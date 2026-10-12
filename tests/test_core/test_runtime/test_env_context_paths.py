"""A relative ``replay_archive`` is resolved against the launch directory, for every caller.

``AgentRuntime.execute`` makes it absolute before the context is recorded as
``TraceConfig.task_setup`` and before setup opens it
(``docs/architecture_design.md`` §4.10), so a Python caller that passes
``env_context`` itself gets what the CLI gets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.task import REPLAY_ARCHIVE_KEY, resolve_env_context_paths
from tests._helpers.assembly import AGENT_ID
from tests._helpers.branch_fakes import TASK, BranchFakeAgent, build_run, make_cap


class TestResolveEnvContextPaths:
    def test_a_relative_directory_becomes_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        context = {"start_url": "https://a.example/", REPLAY_ARCHIVE_KEY: "runs/src"}
        resolved = resolve_env_context_paths(context)
        assert resolved == {
            "start_url": "https://a.example/",
            REPLAY_ARCHIVE_KEY: str((tmp_path / "runs" / "src").resolve()),
        }
        assert context[REPLAY_ARCHIVE_KEY] == "runs/src"  # never mutated

    def test_a_capability_scoped_block_is_resolved_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        resolved = resolve_env_context_paths({"browser": {REPLAY_ARCHIVE_KEY: Path("src")}})
        assert resolved == {"browser": {REPLAY_ARCHIVE_KEY: str((tmp_path / "src").resolve())}}

    def test_a_home_relative_directory_is_expanded(self) -> None:
        resolved = resolve_env_context_paths({REPLAY_ARCHIVE_KEY: "~/runs/src"})
        assert resolved == {REPLAY_ARCHIVE_KEY: str((Path.home() / "runs" / "src").resolve())}

    @pytest.mark.parametrize("value", ["", None, 3])
    def test_a_value_that_is_no_path_is_left_for_setup_to_refuse(self, value: object) -> None:
        assert resolve_env_context_paths({REPLAY_ARCHIVE_KEY: value}) == {REPLAY_ARCHIVE_KEY: value}

    def test_no_context_stays_none(self) -> None:
        assert resolve_env_context_paths(None) is None
        assert resolve_env_context_paths({}) == {}


async def test_the_trace_and_setup_see_the_absolute_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Python caller's relative path: recorded and opened as the same absolute directory."""
    source = tmp_path / "recorded" / "src"
    source.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    cap = make_cap()
    run, writer, _llm = build_run(
        tmp_path / "traces",
        loop=ReactLoop(max_steps=2),
        replies=["done ok"],
        agent=BranchFakeAgent(),
        cap=cap,
    )

    await AgentRuntime.for_agent(run, AGENT_ID).execute(
        TASK, timeout=30, env_context={REPLAY_ARCHIVE_KEY: "recorded/src"}
    )

    expected = str(source.resolve())
    assert cap.replay_archive == expected
    assert writer.run_dir is not None
    task_setup = RunTrace(writer.run_dir).config.task_setup
    assert task_setup is not None
    assert task_setup.context == {REPLAY_ARCHIVE_KEY: expected}
