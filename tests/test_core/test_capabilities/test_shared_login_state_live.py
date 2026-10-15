"""Shared Chrome keeps seeded authentication and committed run evidence separate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import (
    NetworkArchiver,
    ReplayLogArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.backends.base import SharedBrowserProvider
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.replay.archive_source import ArchiveNetworkSource
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.capabilities.browser.replay.pinned import PinnedReplayability
from spectragent.core.capabilities.browser.storage_seed import StorageSeed, read_initial_seed
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.experiment import ExperimentScope
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action
from spectragent.schemas.browser_storage import (
    BrowserStorageState,
    SessionStorageArea,
    StorageArea,
    StorageTab,
)
from spectragent.schemas.task import BrowserSetup
from tests._helpers.assembly import AGENT_ID, make_run
from tests.test_core.test_capabilities.test_browser_storage_seed_live import (
    auth_site as auth_site,
)
from tests.test_core.test_capabilities.test_browser_storage_seed_live import (
    evaluate,
)

pytestmark = pytest.mark.requires_chrome


class FixtureAgent:
    agent_id = "seeded_shared_fixture"
    __execution_limit_fields__ = frozenset()
    __wiring__ = {
        "action": ("action", ["browser.done"]),
        "observation": ("observation", ["browser.browser_meta"]),
    }

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list[UserMessage]:
        return [UserMessage(content="Inspect the authenticated local fixture.")]

    def get_react_output_model(self) -> None:
        return None

    def parse_react_output(self, output: Any) -> list[Action]:
        return [Action(name="browser.done", params={"extracted_content": "authenticated"})]


def seed_for(origin: str, label: str) -> BrowserStorageState:
    return BrowserStorageState(
        cookies=[
            {"name": "sid", "value": "authenticated", "domain": "one.test", "path": "/"},
            {"name": "owner", "value": label, "domain": "one.test", "path": "/"},
        ],
        local=[
            StorageArea(
                storage_key=origin + "/",
                origin=origin,
                items={"token": f"initial-{label}", f"only-{label}": label},
            )
        ],
        tabs=[StorageTab(tab=label)],
        session=[
            SessionStorageArea(
                tab=label,
                storage_key=origin + "/",
                origin=origin,
                items={"tab": label},
            )
        ],
    )


async def assert_first_script(cap: BrowserCapability, label: str) -> None:
    first = await evaluate(cap, "window.first")
    assert first["local"] == f"initial-{label}"
    assert first["session"] == label
    assert set(first["cookie"].split("; ")) == {"sid=authenticated", f"owner={label}"}
    other = "B" if label == "A" else "A"
    assert await evaluate(cap, f"localStorage.getItem('only-{other}')") is None


def network_urls(trace: RunTrace) -> list[str]:
    return [
        row.get("url", "")
        for _, entry in trace.iter_archive("browser.network", include_trailing=True)
        if entry.name == "index.jsonl"
        for line in trace.read_extra(entry.path).splitlines()
        if (row := json.loads(line)).get("kind") == "exchange"
    ]


async def test_shared_seeded_runtimes_commit_isolated_state_and_replay_after_one_closes(
    auth_site: tuple[str, list[tuple[str, str]]], tmp_path: Path
) -> None:
    origin, requests = auth_site
    seeds = {label: seed_for(origin, label) for label in ("A", "B")}
    seed_path = tmp_path / "A-seed.json"
    seed_path.write_text(seeds["A"].model_dump_json())
    ready = {label: asyncio.Event() for label in seeds}
    a_closed = asyncio.Event()
    caps: dict[str, BrowserCapability] = {}
    context_ids: dict[str, str] = {}

    class Model:
        model = name = "scripted_seeded_fixture"

        def __init__(self, label: str) -> None:
            self.label = label

        async def ainvoke(self, messages: Any, **kwargs: Any) -> ChatInvokeCompletion:
            label = self.label
            cap = caps[label]
            await assert_first_script(cap, label)
            assert cap.browser_context_id
            context_ids[label] = cap.browser_context_id
            ready[label].set()
            await ready["B" if label == "A" else "A"].wait()
            assert context_ids["A"] != context_ids["B"]
            a, b = caps["A"].backend, caps["B"].backend
            assert isinstance(a, SharedBrowserProvider) and isinstance(b, SharedBrowserProvider)
            assert a.managed_browser is b.managed_browser
            if label == "B":
                await a_closed.wait()
                # A's Runtime has torn down its context; B's context and recorder remain usable.
                await assert_first_script(cap, "B")
                assert await evaluate(
                    cap, "[localStorage.token, sessionStorage.tab, localStorage.getItem('only-B')]"
                ) == ["after-first-script", "B", "B"]
                assert set((await evaluate(cap, "document.cookie")).split("; ")) == {
                    "sid=authenticated",
                    "owner=B",
                }
                reply = await cap.cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": """(async () => {
                            localStorage.setItem('token', 'B-after-A');
                            sessionStorage.setItem('tab', 'B-after-A');
                            document.cookie = 'owner=B-after-A;path=/';
                            await fetch('/after-A?run=B');
                            return [localStorage.token, sessionStorage.tab, document.cookie];
                        })()""",
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    cap.default_session_id,
                )
                assert "exceptionDetails" not in reply
                assert reply["result"]["value"][:2] == ["B-after-A", "B-after-A"]
                assert "owner=B-after-A" in reply["result"]["value"][2]
            return ChatInvokeCompletion(completion="done", usage=None)

        async def aclose(self, *, timeout: float = 5) -> tuple[()]:
            return ()

    async with ExperimentScope() as scope:
        runtimes = {}
        for label in seeds:
            cap = BrowserCapability(profile="vision_only", settle_quiet_ms=30)
            provider = LocalChromeProvider(
                headless=True,
                lifetime="experiment",
                extra_args=["--no-proxy-server", "--host-resolver-rules=MAP *.test 127.0.0.1"],
            )
            cap.backend = provider
            cap.replayability = PinnedReplayability(random_seed=7)
            caps[label] = cap
            run = make_run(
                agent=FixtureAgent(),
                loop=ReactLoop(),
                env=BrowserEnvironment(capabilities={"browser": cap}),
                llm=Model(label),
                memory=SimpleMemory(),
                hooks=[
                    TraceWriter(
                        trace_dir=str(tmp_path), archive=[NetworkArchiver(), StorageArchiver()]
                    )
                ],
            )
            run.by_layer["browser_provider"] = {"shared": provider}
            scope.bind(run)
            runtimes[label] = AgentRuntime.for_agent(run, AGENT_ID)

        async def execute(label: str) -> Any:
            result = await runtimes[label].execute(
                "Inspect the authenticated local fixture.",
                task_id=label,
                run_id=f"shared-seed-{label}",
                env_context=BrowserSetup(
                    start_url=f"{origin}/auth?run={label}",
                    storage_state=str(seed_path) if label == "A" else seeds[label],
                ).to_context(),
            )
            assert result.agent_done.value == "done", result
            if label == "A":
                assert caps[label].browser_context_id is None
                a_closed.set()
            return result

        tasks = [asyncio.create_task(execute(label)) for label in seeds]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=90)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    traces = {label: RunTrace(tmp_path / f"shared-seed-{label}") for label in seeds}
    for label, trace in traces.items():
        assert trace.manifest.status == "complete"
        assert read_initial_seed(trace).data == StorageSeed.freeze(seeds[label]).data
        assert trace.config.task_setup is not None
        assert trace.config.task_setup.context == {"start_url": f"{origin}/auth?run={label}"}
        urls = network_urls(trace)
        assert f"{origin}/auth?run={label}" in urls
        other = "B" if label == "A" else "A"
        assert not any(f"run={other}" in url for url in urls), urls
        snapshots = [
            json.loads(trace.read_extra(entry.path))
            for _, entry in trace.iter_archive("browser.storage", include_trailing=True)
            if entry.name == "storage.json"
        ]
        assert snapshots
        for state in snapshots:
            assert not state["unavailable"]
            assert all(area["tab"] == label for area in state["session"])
            assert all(f"only-{other}" not in area["items"] for area in state["local"])
        last = snapshots[-1]
        expected = "B-after-A" if label == "B" else "after-first-script"
        assert any(area["items"].get("token") == expected for area in last["local"])
        assert any(
            area["items"].get("tab") == ("B-after-A" if label == "B" else "A")
            for area in last["session"]
        )
        assert any(
            cookie.get("value") == ("B-after-A" if label == "B" else "A")
            for cookie in last["cookies"]
            if cookie.get("name") == "owner"
        )
    assert f"{origin}/after-A?run=B" in network_urls(traces["B"])
    assert not any("after-A" in url for url in network_urls(traces["A"]))
    for label in seeds:
        first_request = next(cookie for path, cookie in requests if path == f"/auth?run={label}")
        assert set(first_request.split("; ")) == {"sid=authenticated", f"owner={label}"}
    assert all("__af_storage_seed__" not in path for path, _ in requests)

    # Restore A from committed evidence in a fresh, exclusive browser, not B's later state.
    seed_path.unlink()
    request_count = len(requests)
    replay = BrowserCapability(profile="vision_only", settle_quiet_ms=30)
    replay.backend = LocalChromeProvider(headless=True)
    replay.replayability = PinnedReplayability(random_seed=7, base_time="from_archive")
    replay.network_source = ArchiveNetworkSource(
        matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
    )
    replay.bind_archivers([ReplayLogArchiver()])
    try:
        await replay.setup(
            negotiation={"browser.browser_meta"},
            start_url=f"{origin}/auth?run=A",
            replay_archive=str(tmp_path / "shared-seed-A"),
        )
        await assert_first_script(replay, "A")
        assert len(requests) == request_count, "offline replay reached the live auth fixture"
    finally:
        await replay.teardown()
