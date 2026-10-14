"""A replaying BrowserCapability against the fake browser: isolation, setup, serving (M30).

§4.14 "回放": the isolation switches derive from the source and reach only a
provider declaring ``NETWORK_ISOLATION``; ``none_acknowledged`` launches
without them and says so in the replay fingerprint; a replay without the
replay-log archiver is refused naming both; ``base_time: from_archive`` pins
the recording's T0; and once set up, the capability's replay server answers
the recorder's paused requests from the source run, ``browser.replay_log``
writes the decision into the step's ``replay.jsonl`` and the fingerprint
carries the replay block. The real Chrome counterpart is
``test_browser_replay_live.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import ReplayLogArchiver, StorageArchiver
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    NETWORK_ISOLATION_ARGS,
)
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.replay import (
    ArchiveNetworkSource,
    PinnedReplayability,
)
from tests._helpers.browser_archive import IsolatingBrowser, OwnedBrowser, settle
from tests._helpers.fake_cdp import FakeBrowser, FakeClient
from tests._helpers.replay_archive import RECORDED_T0_MS, RecordingScript

PINNED_ARGS = tuple(PinnedReplayability(random_seed=7).launch_args())
PINNED_ENV = dict(PinnedReplayability(random_seed=7).launch_env())


@pytest.fixture(scope="module")
def source_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    async def _record() -> Path:
        script = RecordingScript(
            tmp_path_factory.mktemp("source"),
            run_id="run-src",
            launch_flags=OwnedBrowser().planned_launch_flags(PINNED_ARGS, PINNED_ENV),
        )
        await script.exchange("http://shop.test/api/data.json", body=b'{"n":1}')
        await script.commit(1)
        return script.finish()

    return asyncio.run(_record())


def _replaying(
    backend: Any,
    *,
    source: ArchiveNetworkSource | None = None,
    archivers: tuple[Any, ...] = (),
    tier: Any = None,
) -> tuple[BrowserCapability, FakeBrowser]:
    """A replaying capability holding *archivers* as its binding, unchecked.

    Bypassing ``bind_archivers`` leaves the refusals to setup, which is what
    these tests exercise (``test_browser_replay_cooperation.py`` covers the
    binding-time ones).
    """
    browser = FakeBrowser()
    cap = BrowserCapability()
    cap.backend = backend  # type: ignore[assignment]
    cap.replayability = tier if tier is not None else PinnedReplayability(random_seed=7)
    cap.network_source = source if source is not None else ArchiveNetworkSource()
    cap._archivers = list(archivers or (ReplayLogArchiver(),))
    cap._inject_cdp_factory(browser.client)
    return cap, browser


class TestIsolation:
    async def test_args_reach_a_supporting_provider(self, source_run: Path) -> None:
        backend = IsolatingBrowser()
        cap, _browser = _replaying(backend)
        await cap.setup(replay_archive=str(source_run))
        try:
            assert backend.open_calls == [
                {"launch_args": (*PINNED_ARGS, *NETWORK_ISOLATION_ARGS), "launch_env": PINNED_ENV}
            ]
        finally:
            await cap.teardown()

    async def test_none_acknowledged_launches_without_isolation_and_records_it(
        self, source_run: Path
    ) -> None:
        backend = OwnedBrowser()
        cap, _browser = _replaying(
            backend, source=ArchiveNetworkSource(isolation="none_acknowledged")
        )
        await cap.setup(replay_archive=str(source_run))
        try:
            assert backend.open_calls == [{"launch_args": PINNED_ARGS, "launch_env": PINNED_ENV}]
            batch = await cap.collect_archive()
            fingerprint = next(f for f in batch.files if f.scope == "run")
            block = json.loads(fingerprint.data)["network_source"]
            assert block["source_id"] == "archive" and block["isolation"] == "none_acknowledged"
        finally:
            await cap.teardown()

    async def test_replay_without_the_replay_log_archiver_raises(self, source_run: Path) -> None:
        backend = IsolatingBrowser()
        cap, browser = _replaying(backend, archivers=(StorageArchiver(),))
        with pytest.raises(BrowserCooperationError) as info:
            await cap.setup(replay_archive=str(source_run))
        (problem,) = info.value.problems
        assert problem.startswith("network_source: archive ↔ bound archivers ['browser.storage']")
        assert "bind browser.replay_log" in problem
        assert backend.open_calls == [] and browser.clients == []


class TestBrowserDefaults:
    """A replay's browser must fall back to the recording's time zone and locale (§4.14)."""

    async def test_a_matching_browser_replays_and_records_its_defaults(
        self, source_run: Path
    ) -> None:
        cap, _browser = _replaying(IsolatingBrowser())
        await cap.setup(replay_archive=str(source_run))
        try:
            batch = await cap.collect_archive()
            fingerprint = next(f for f in batch.files if f.scope == "run")
            assert json.loads(fingerprint.data)["browser_defaults"] == {
                "timezone": "UTC",
                "locale": "en-US",
            }
        finally:
            await cap.teardown()

    async def test_a_default_the_os_could_not_say_is_refused_after_launch(
        self, source_run: Path
    ) -> None:
        """No locale read before launch (a Linux host): Chrome's own answer decides, strictly."""

        class _NoLocaleBeforeLaunch(IsolatingBrowser):
            LOCALE = None

        cap, browser = _replaying(_NoLocaleBeforeLaunch())
        browser.default_locale = "fr-FR"
        try:
            with pytest.raises(BrowserCooperationError) as info:
                await cap.setup(replay_archive=str(source_run))
            (problem,) = info.value.problems
            assert problem.startswith("source run 'run-src' browser default locale='en-US' ↔")
            assert "locale='fr-FR' (measured after launch)" in problem
            # Refused before the recorder prepared any target or opened its auto-attach.
            assert "Target.setAutoAttach" not in browser.recorder.methods()
        finally:
            await cap.teardown()

    async def test_a_failed_measurement_refuses_the_replay(
        self, source_run: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap, browser = _replaying(IsolatingBrowser())
        handle = browser.handle

        async def fail_default_measurement(
            client: FakeClient, method: str, params: dict[str, Any], session_id: str | None
        ) -> Any:
            # Fail the recorder's defaults measurement, after the control-side OS probe.
            if method == "Page.getFrameTree" and client is not browser.control:
                raise RuntimeError("CDP error: no frame")
            return await handle(client, method, params, session_id)

        monkeypatch.setattr(browser, "handle", fail_default_measurement)
        try:
            with pytest.raises(BrowserCooperationError, match="could not be measured after launch"):
                await cap.setup(replay_archive=str(source_run))
            assert cap._input_platform == "macos"
        finally:
            await cap.teardown()


class TestServing:
    async def test_a_replaying_capability_serves_and_logs(self, source_run: Path) -> None:
        cap, browser = _replaying(IsolatingBrowser())
        await cap.setup(replay_archive=str(source_run))
        try:
            recorder = cap._recorder
            assert recorder is not None and cap._replay_server is not None
            sid = recorder.session_for_target("page-1")
            assert sid is not None
            browser.recorder.fire(
                "Fetch.requestPaused",
                {
                    "requestId": "q1",
                    "networkId": "n1",
                    "request": {
                        "url": "http://shop.test/api/data.json",
                        "method": "GET",
                        "headers": {},
                    },
                    "frameId": "page-1",
                    "resourceType": "XHR",
                },
                sid,
            )
            await settle()
            fulfil = next(s for s in browser.recorder.sent if s.method == "Fetch.fulfillRequest")
            assert (
                fulfil.session_id == sid and base64.b64decode(fulfil.params["body"]) == b'{"n":1}'
            )
            assert not [s for s in browser.recorder.sent if s.method.startswith("Fetch.continue")]

            batch = await cap.collect_archive()
            files = {(f.archiver_id, f.name): f for f in batch.files}
            log = files[("browser.replay_log", "replay.jsonl")]
            (entry,) = [json.loads(line) for line in log.data.splitlines()]
            assert entry["kind"] == "hit" and entry["run_id"] == "run-src"
            assert set(files) == {
                ("browser.replay_log", "replay.jsonl"),
                ("browser", "fingerprint.json"),
            }, "a replay records its log, no traffic of its own"
            block = json.loads(files[("browser", "fingerprint.json")].data)["network_source"]
            assert block["source_run_id"] == "run-src" and block["isolation"] == "required"
            assert block["source_fingerprint_sha256"]
            # The fake browser is not the recording's Chrome: tolerated, and recorded.
            assert any(w.startswith("Chrome product differs") for w in block["warnings"])
        finally:
            await cap.teardown()
        assert cap._replay_server is None

    async def test_base_time_from_archive_pins_the_recorded_t0(self, source_run: Path) -> None:
        tier = PinnedReplayability(random_seed=7, base_time="from_archive")
        cap, browser = _replaying(IsolatingBrowser(), tier=tier)
        await cap.setup(replay_archive=str(source_run))
        try:
            assert cap._replay_plan is not None and cap._replay_plan.t0_ms == RECORDED_T0_MS
            shim = next(
                s.params["source"]
                for s in browser.recorder.sent
                if s.method == "Page.addScriptToEvaluateOnNewDocument"
                and str(RECORDED_T0_MS) in s.params["source"]
            )
            assert shim
        finally:
            await cap.teardown()

    async def test_the_participants_are_plan_server_sessions(self, source_run: Path) -> None:
        cap, _browser = _replaying(IsolatingBrowser())
        await cap.setup(replay_archive=str(source_run))
        try:
            kinds = [type(p).__name__ for p in cap._recorder_participants()]
            assert kinds == ["ReplayabilityPlan", "ReplayServer", "ReplayLogSession"]
        finally:
            await cap.teardown()
