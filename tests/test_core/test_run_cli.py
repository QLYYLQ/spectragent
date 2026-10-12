"""``spectragent-run``'s task-context flags (``--start-url``, ``--replay-archive``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spectragent.core import run_cli


def test_task_context_carries_only_what_was_given() -> None:
    assert run_cli.task_context() is None
    assert run_cli.task_context(start_url="https://a.example") == {"start_url": "https://a.example"}
    assert run_cli.task_context(replay_archive="/runs/src") == {"replay_archive": "/runs/src"}


def test_a_relative_replay_archive_is_passed_as_given() -> None:
    """``AgentRuntime.execute`` resolves it, for the CLI and every other caller alike.

    ``tests/test_core/test_runtime/test_env_context_paths.py`` pins that the
    trace and setup both see the absolute directory.
    """
    assert run_cli.task_context(replay_archive="runs/src") == {"replay_archive": "runs/src"}


def test_replay_archive_flag_reaches_env_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(run_cli, "_run", _fake_run)
    monkeypatch.setattr(run_cli, "load_dotenv", lambda: None)  # never read the repo's .env
    monkeypatch.setattr(run_cli.logging, "basicConfig", lambda **_kw: None)
    code = run_cli.main(
        ["--config", "run.yaml", "--task", "replay it", "--replay-archive", "/runs/src"]
    )
    assert code == 0
    assert seen["replay_archive"] == "/runs/src" and seen["config_path"] == Path("run.yaml")
    assert run_cli.task_context(
        start_url=seen["start_url"], replay_archive=seen["replay_archive"]
    ) == {"replay_archive": "/runs/src"}
