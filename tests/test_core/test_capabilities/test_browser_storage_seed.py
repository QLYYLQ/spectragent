"""Initial state is immutable evidence, never an after-action approximation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.commands import BrowserConnection
from spectragent.core.capabilities.browser.storage_seed import (
    StorageSeed,
    _Hydration,
    read_initial_seed,
)
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.browser_storage import (
    BrowserStorageState,
    SessionStorageArea,
    StorageArea,
    StorageTab,
)
from spectragent.schemas.task import BrowserSetup
from tests._helpers.assembly import make_env


def state(token: str = "initial") -> BrowserStorageState:
    return BrowserStorageState(
        local=[
            StorageArea(
                storage_key="https://site.test/",
                origin="https://site.test",
                items={"auth_token": token},
            )
        ]
    )


def test_freeze_has_no_mutable_caller_or_source_file_reference(tmp_path: Path) -> None:
    original = state()
    seed = StorageSeed.freeze(original)
    original.local[0].items["auth_token"] = "mutated"
    seed.state.local[0].items["auth_token"] = "also-mutated"
    assert seed.state.local[0].items["auth_token"] == "initial"
    path = tmp_path / "seed.json"
    path.write_bytes(seed.data)
    env = make_env(EnvironmentBase, {"browser#0": BrowserCapability()})
    context = env.prepare_setup_context({"browser": {"storage_state": str(path)}}).execution
    path.unlink()
    assert context is not None
    assert StorageSeed.freeze(context["browser"]["storage_state"]).data == seed.data


def test_task_setup_records_provenance_without_seed_values_or_path() -> None:
    context = {"browser": {"start_url": "https://site.test/", "storage_state": state()}}
    env = make_env(EnvironmentBase, {"browser#0": BrowserCapability()})
    assert env.prepare_setup_context(context).recordable == {
        "browser": {"start_url": "https://site.test/"}
    }
    assert "storage_state" in context["browser"], "recording must not mutate setup input"
    assert BrowserSetup(
        storage_state=state(), login_required=True, login_type="password"
    ).to_context() == {"storage_state": state()}
    with pytest.raises(ValueError, match="credentials_ref is unsupported"):
        BrowserSetup.model_validate({"credentials_ref": "ignored-store"})


def reader(seed: StorageSeed, *, initial: bool = True, snapshot: bool = True) -> Any:
    documents = {"fingerprint": canonical_json({"initial_storage": seed.fingerprint()})}
    entries = []
    if snapshot:
        documents["snapshot"] = StorageSeed.freeze(state("after-action")).data
        entries.append((0, SimpleNamespace(name="storage.json", path="snapshot")))
    if initial:
        documents["initial"] = seed.data
        entries.append((0, SimpleNamespace(name="initial_state.json", path="initial")))
    return SimpleNamespace(
        archive_run_file=lambda _: SimpleNamespace(path="fingerprint"),
        read_extra=documents.__getitem__,
        iter_archive=lambda *args, **kwargs: iter(entries),
    )


def test_replay_reads_initial_evidence_and_refuses_missing_seed() -> None:
    seed = StorageSeed.freeze(state())
    assert read_initial_seed(reader(seed)).data == seed.data
    with pytest.raises(ValueError, match="does not contain its initial_state.json"):
        read_initial_seed(reader(seed, initial=False))
    incomplete = StorageSeed(seed.data, "observed_frames")
    with pytest.raises(ValueError, match="persistent-profile origins are unknown"):
        read_initial_seed(reader(incomplete))


def test_seed_and_fingerprint_must_agree() -> None:
    seed = StorageSeed.freeze(state())
    source = reader(seed)
    read = source.read_extra
    source.read_extra = lambda path: (
        StorageSeed.freeze(state("wrong")).data if path == "initial" else read(path)
    )
    with pytest.raises(ValueError, match="disagrees with browser fingerprint"):
        read_initial_seed(source)
    assert seed.fingerprint()["nonempty"] is True
    assert json.loads(StorageSeed.freeze(BrowserStorageState()).data)["cookies"] == []


def test_session_state_requires_unique_tabs_and_storage_keys() -> None:
    with pytest.raises(ValueError, match="absent from tabs"):
        BrowserStorageState(
            session=[
                SessionStorageArea(
                    tab="missing", storage_key="https://site.test/", origin="https://site.test"
                )
            ]
        )
    with pytest.raises(ValueError, match="duplicate tab"):
        BrowserStorageState(tabs=[StorageTab(tab="a"), StorageTab(tab="a")])
    with pytest.raises(ValueError, match="duplicate storage"):
        BrowserStorageState(local=[state().local[0], state().local[0]])


def test_explicit_seed_refuses_external_or_persistent_state_before_launch(tmp_path: Path) -> None:
    from spectragent.core.capabilities.browser import BrowserCapability
    from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

    external = BrowserCapability(cdp_url="ws://localhost:9222/devtools/browser/external")
    with pytest.raises(BrowserCooperationError, match="owned fresh browser context"):
        external.check_setup({"storage_state": state()})
    persistent = BrowserCapability()
    persistent.backend = LocalChromeProvider(user_data_dir=str(tmp_path / "profile"))
    with pytest.raises(
        BrowserCooperationError, match="persistent-profile state cannot be replaced completely"
    ):
        persistent.check_setup({"storage_state": state()})
    assert not (tmp_path / "profile").exists()


def attached(sid: str, frame: str = "frame") -> dict[str, Any]:
    return {"sessionId": sid, "targetInfo": {"targetId": frame}}


async def test_hydration_detach_before_prepare_and_late_events_cannot_revive_session() -> None:
    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace()))
    hydration.sessions.add("root")
    hydration.attached(attached("child"), "root")
    hydration.attached(attached("nested", "nested-frame"), "child")
    tasks = list(hydration.tasks)
    hydration.detached({"sessionId": "child"}, "root")
    hydration.attached(attached("child"), "root")
    hydration.paused({"request": {"url": "late"}}, "child")
    await asyncio.gather(*tasks, return_exceptions=True)
    await hydration.prepare("child")
    assert hydration.sessions == {"root"}
    assert hydration.retired == {"child", "nested"}
    assert hydration.parents == hydration.frame_sessions == {}
    assert not hydration.tasks
    assert not hydration.errors
    hydration.closing = True
    hydration.attached(attached("late"), "root")
    hydration.paused({"request": {"url": "late"}}, "root")
    await hydration.fulfill({}, "root")
    assert hydration.sessions == {"root"}
    assert not hydration.tasks


async def test_hydration_detach_keeps_new_owner_of_same_frame() -> None:
    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace()))
    hydration.sessions.add("root")
    hydration.attached(attached("old"), "root")
    hydration.attached(attached("new"), "root")
    hydration.detached({"sessionId": "old"}, "root")
    assert hydration.frame_sessions == {"frame": "new"}
    hydration.detached({"sessionId": "root"})
    await asyncio.gather(*hydration.tasks, return_exceptions=True)
    assert not hydration.sessions
    assert not hydration.roots
    assert not hydration.frame_sessions


async def test_hydration_detach_before_done_callback_retrieves_and_ignores_late_failure() -> None:
    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace()))
    hydration.sessions.add("child")

    async def rejected() -> None:
        raise PermissionError("session retired while command was pending")

    hydration.spawn(rejected(), "child")
    tasks = list(hydration.tasks)
    await asyncio.sleep(0)
    assert tasks[0].done()
    hydration.detached({"sessionId": "child"})
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not hydration.errors


@pytest.mark.parametrize("phase", ["Fetch.enable", "Target.setAutoAttach", "resume", "reply"])
async def test_hydration_detach_during_prepare_abandons_all_pending_commands(phase: str) -> None:
    entered = asyncio.Event()
    replies = []
    calls = []

    class Reply:
        abandoned = False

        async def wait(self, timeout: float | None = None) -> None:
            if phase == "reply":
                entered.set()
                await asyncio.Event().wait()

        def abandon(self) -> None:
            self.abandoned = True

    async def post(method: str, params: Any, sid: str) -> Reply:
        calls.append((method, sid))
        reply = Reply()
        replies.append(reply)
        if method == phase or (phase == "resume" and method == "Runtime.runIfWaitingForDebugger"):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                reply.abandon()  # the transport owns cleanup until post returns its handle
                raise
        return reply

    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace(post=post)))
    hydration.sessions.add("root")
    hydration.attached(attached("child"), "root")
    tasks = list(hydration.tasks)
    await asyncio.wait_for(entered.wait(), 1)
    hydration.detached({"sessionId": "child"}, "root")
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
    assert replies and all(reply.abandoned for reply in replies)
    assert (
        calls[-1][0]
        == {
            "Fetch.enable": "Fetch.enable",
            "Target.setAutoAttach": "Target.setAutoAttach",
            "resume": "Runtime.runIfWaitingForDebugger",
            "reply": "Runtime.runIfWaitingForDebugger",
        }[phase]
    )
    assert not hydration.errors


async def test_hydration_partial_prepare_failure_still_abandons_earlier_replies() -> None:
    abandoned = []

    async def post(method: str, params: Any, sid: str) -> Any:
        if method == "Target.setAutoAttach":
            raise RuntimeError("injected write failure")
        return SimpleNamespace(abandon=lambda: abandoned.append(method))

    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace(post=post)))
    with pytest.raises(RuntimeError, match="injected write failure"):
        await hydration.prepare("child", resume=True)
    assert abandoned == ["Fetch.enable", "Runtime.runIfWaitingForDebugger"]


@pytest.mark.parametrize("retire_during_read", [False, True])
async def test_hydration_frame_only_tolerates_permission_error_for_its_detached_session(
    retire_during_read: bool,
) -> None:
    calls = []
    area = state().local[0]

    async def send(method: str, params: Any, sid: str, **kwargs: Any) -> Any:
        calls.append((method, sid))
        if sid == "old":
            if retire_during_read:
                hydration.detached({"sessionId": "old"})
            raise PermissionError("session is unavailable")
        if method == "Storage.getStorageKey":
            return {"storageKey": area.storage_key}
        return {
            "frameTree": {
                "frame": {"id": "root-frame", "url": "seed", "securityOrigin": area.origin}
            }
        }

    hydration = _Hydration(cast(BrowserConnection, SimpleNamespace(send=send)))
    hydration.sessions.update({"root", "old"})
    if retire_during_read:
        assert await hydration.frame("root", area, "seed") == ("root", "root-frame")
    else:
        with pytest.raises(PermissionError, match="session is unavailable"):
            await hydration.frame("root", area, "seed")


async def test_hydration_close_skips_retired_root_and_reports_unfinished_tasks() -> None:
    methods = []

    async def send(method: str, params: Any, sid: str, **kwargs: Any) -> None:
        methods.append(method)
        hydration.detached({"sessionId": sid})
        raise PermissionError("session retired during send")

    hydration = _Hydration(
        cast(BrowserConnection, SimpleNamespace(send=send, off_event=lambda *args: None))
    )
    hydration.sessions.add("root")
    hydration.roots.add("root")
    await hydration.close()
    assert methods == ["Fetch.disable"]
    assert not hydration.sessions
    assert not hydration.roots

    release = asyncio.Event()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    hydration.spawn(slow(), "slow")
    await started.wait()
    tasks = list(hydration.tasks)
    try:
        with pytest.raises(ExceptionGroup, match="cleanup could not be confirmed") as caught:
            await asyncio.wait_for(hydration.close(), 3)
        assert "remain unfinished" in str(caught.value.exceptions[0])
        assert tasks[0] in hydration.tasks
    finally:
        release.set()
        await asyncio.gather(*tasks)
