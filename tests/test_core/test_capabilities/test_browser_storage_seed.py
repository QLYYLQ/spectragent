"""Initial state is immutable evidence, never an after-action approximation."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.storage_seed import StorageSeed, read_initial_seed
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
