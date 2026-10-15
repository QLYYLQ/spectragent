"""Setup input transformation is owned by the capability that receives the key."""

from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.browser_storage import BrowserStorageState
from tests._helpers.assembly import AGENT_ID, ENV_ID, make_env
from tests._helpers.branch_fakes import TASK, BranchFakeAgent, build_run, make_cap


class OpaqueCapability(CapabilityBase):
    __abstract__ = True
    __registry_key__ = "opaque"

    async def setup(self, *, negotiation: Any = None, storage_state: Any = None) -> None:
        self.seen = storage_state


@pytest.mark.parametrize("namespace", [None, "opaque#0", "opaque"])
async def test_runtime_keeps_other_domain_storage_value_and_records_it(
    tmp_path: Path, namespace: str | None
) -> None:
    cap = OpaqueCapability()
    run, writer, _ = build_run(
        tmp_path / "traces",
        loop=ReactLoop(max_steps=1),
        replies=["done ok"],
        agent=BranchFakeAgent(),
        cap=make_cap(),
    )
    env = run.instance("environment", ENV_ID)
    env.capabilities["opaque#0"] = cap
    context = {"storage_state": "opaque-cache-id"}
    if namespace is not None:
        context = {namespace: context}
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(TASK, env_context=context)
    assert result.answer == "ok"
    assert cap.seen == "opaque-cache-id"
    assert writer.run_dir is not None
    setup = RunTrace(writer.run_dir).config.task_setup
    assert setup is not None and setup.context == context


@pytest.mark.parametrize("namespace", ["browser", "browser#0", "actual-browser"])
def test_real_browser_alias_freezes_source_once_and_omits_only_owned_seed(
    tmp_path: Path, namespace: str
) -> None:
    path = tmp_path / "state.json"
    state = BrowserStorageState()
    path.write_text(state.model_dump_json())
    instance = "browser#0" if namespace == "browser" else namespace
    env = make_env(EnvironmentBase, {instance: BrowserCapability(), "opaque#0": OpaqueCapability()})
    original = {
        namespace: {"storage_state": str(path), "start_url": "https://site.test/"},
        "opaque#0": {"storage_state": "opaque-cache-id"},
    }
    prepared = env.prepare_setup_context(original)
    path.write_text("not JSON")
    path.unlink()
    routed = env.route_setup_context(prepared.execution)
    assert routed[instance]["storage_state"] == state
    assert prepared.recordable == {
        namespace: {"start_url": "https://site.test/"},
        "opaque#0": {"storage_state": "opaque-cache-id"},
    }
    assert original[namespace]["storage_state"].endswith("state.json")


def test_flat_shared_key_is_broadcast_before_each_owner_prepares_it(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(BrowserStorageState().model_dump_json())
    env = make_env(
        EnvironmentBase, {"browser#0": BrowserCapability(), "opaque#0": OpaqueCapability()}
    )
    prepared = env.prepare_setup_context({"storage_state": str(path)})
    routed = env.route_setup_context(prepared.execution)
    assert isinstance(routed["browser#0"]["storage_state"], BrowserStorageState)
    assert routed["opaque#0"]["storage_state"] == str(path)
    recorded = env.route_setup_context(prepared.recordable)
    assert recorded["browser#0"] == {}
    assert recorded["opaque#0"]["storage_state"] == str(path)


@pytest.mark.parametrize("flat_last", [False, True])
def test_namespace_and_flat_override_order_matches_existing_router(
    tmp_path: Path, flat_last: bool
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(BrowserStorageState().model_dump_json())
    second.write_text(
        BrowserStorageState(
            cookies=[{"name": "sid", "value": "second", "domain": "site.test", "path": "/"}]
        ).model_dump_json()
    )
    env = make_env(
        EnvironmentBase, {"browser#0": BrowserCapability(), "opaque#0": OpaqueCapability()}
    )
    pairs = [("storage_state", str(first)), ("browser#0", {"storage_state": str(second)})]
    if flat_last:
        pairs.reverse()
    context = dict(pairs)
    before = env.route_setup_context(context)
    prepared = env.prepare_setup_context(context)
    execution = env.route_setup_context(prepared.execution)
    expected = BrowserStorageState.model_validate_json(
        Path(before["browser#0"]["storage_state"]).read_bytes()
    )
    assert execution["browser#0"]["storage_state"] == expected
    assert execution["opaque#0"] == before["opaque#0"]
    assert env.route_setup_context(prepared.recordable)["opaque#0"] == before["opaque#0"]
    assert env.route_setup_context(prepared.recordable)["browser#0"] == {}


def test_unknown_namespace_is_refused_before_any_browser_file_read(tmp_path: Path) -> None:
    env = make_env(EnvironmentBase, {"browser#0": BrowserCapability()})
    with pytest.raises(ValueError, match="accepted by none"):
        env.prepare_setup_context({"unknown": {"storage_state": str(tmp_path / "missing.json")}})


def test_namespaced_browser_override_prevents_reading_other_domains_flat_cache_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(BrowserStorageState().model_dump_json())
    env = make_env(
        EnvironmentBase, {"browser#0": BrowserCapability(), "opaque#0": OpaqueCapability()}
    )
    prepared = env.prepare_setup_context(
        {"storage_state": "opaque-cache-id", "browser#0": {"storage_state": str(path)}}
    )
    execution = env.route_setup_context(prepared.execution)
    recorded = env.route_setup_context(prepared.recordable)
    assert isinstance(execution["browser#0"]["storage_state"], BrowserStorageState)
    assert execution["opaque#0"]["storage_state"] == "opaque-cache-id"
    assert recorded == {"browser#0": {}, "opaque#0": {"storage_state": "opaque-cache-id"}}


async def test_runtime_prepares_browser_path_once_before_lifecycle_and_reuses_frozen_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "seed.json"
    state = BrowserStorageState(
        cookies=[{"name": "sid", "value": "frozen", "domain": "site.test", "path": "/"}]
    )
    path.write_text(state.model_dump_json())
    reads = 0
    original_read = Path.read_bytes

    def read_bytes(source: Path) -> bytes:
        nonlocal reads
        if source == path:
            reads += 1
        return original_read(source)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    browser = BrowserCapability(actions=[])
    received = []

    async def setup(*, negotiation: Any = None, storage_state: Any = None) -> None:
        assert not path.exists()
        received.append(storage_state)
        browser._alive = True

    monkeypatch.setattr(browser, "setup", setup)
    run, writer, _ = build_run(
        tmp_path / "traces",
        loop=ReactLoop(max_steps=1),
        replies=["done ok"],
        agent=BranchFakeAgent(),
        cap=make_cap(),
    )
    env = run.instance("environment", ENV_ID)
    env.capabilities.update({"browser#0": browser, "opaque#0": OpaqueCapability()})
    write_start = writer.on_run_start

    async def remove_source(**context: Any) -> None:
        path.write_text("changed after preparation")
        path.unlink()
        await write_start(**context)

    monkeypatch.setattr(writer, "on_run_start", remove_source)
    context = {
        "browser#0": {"storage_state": str(path)},
        "opaque#0": {"storage_state": "opaque-cache-id"},
    }
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(TASK, env_context=context)
    assert result.answer == "ok"
    assert reads == 1
    assert received == [state]
    assert writer.run_dir is not None
    record = RunTrace(writer.run_dir).config.task_setup
    assert record is not None
    assert record.context == {"browser#0": {}, "opaque#0": {"storage_state": "opaque-cache-id"}}
    assert not record.redactions.entries
