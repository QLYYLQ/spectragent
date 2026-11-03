"""``spectragent run``: a benchmark's task (``--benchmark``, ``--task-id``) or one written here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core import run_cli

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
BROWSER_CONFIG = CONFIGS / "browser_use_local.yaml"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cli, "load_dotenv", lambda: None)  # never read the repo's .env
    monkeypatch.setattr(run_cli.logging, "basicConfig", lambda **_kw: None)


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


def test_the_oracle_timeout_flag_reaches_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    async def _fake_run(**kwargs: Any) -> int:
        seen.append(kwargs["oracle_timeout"])
        return 0

    monkeypatch.setattr(run_cli, "_run", _fake_run)
    monkeypatch.setattr(run_cli, "load_dotenv", lambda: None)  # never read the repo's .env
    monkeypatch.setattr(run_cli.logging, "basicConfig", lambda **_kw: None)
    argv = ["--config", "run.yaml", "--task", "score it"]
    assert run_cli.main(argv) == 0
    assert run_cli.main([*argv, "--oracle-timeout", "9"]) == 0
    assert seen == [run_cli.ORACLE_TIMEOUT_S, 9.0]


def test_a_dry_run_checks_what_setup_would_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` prepares with the flags' task context: the setup check sees it too."""
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

    monkeypatch.setattr(run_cli, "load_dotenv", lambda: None)  # never read the repo's .env
    monkeypatch.setattr(run_cli.logging, "basicConfig", lambda **_kw: None)
    config = str(Path(__file__).resolve().parents[2] / "configs" / "browser_use_local.yaml")
    assert run_cli.main(["--config", config, "--dry-run"]) == 0
    with pytest.raises(BrowserCooperationError, match="network_source: live ↔ replay_archive"):
        run_cli.main(["--config", config, "--dry-run", "--replay-archive", "/runs/src"])


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--benchmark", "online_mind2web"], "--benchmark and --task-id go together"),
        (["--task-id", "t1"], "--benchmark and --task-id go together"),
        (
            ["--benchmark", "online_mind2web", "--task-id", "t1", "--task", "other"],
            "--task: a benchmark task's text and start come from its benchmark",
        ),
        (
            ["--benchmark", "online_mind2web", "--task-id", "t1", "--start-url", "https://a.b"],
            "--start-url: a benchmark task's text and start come from its benchmark",
        ),
        ([], "--task is required unless --dry-run or --task-id is set"),
    ],
)
def test_a_task_comes_from_one_place(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        run_cli.main(["--config", "run.yaml", *argv])
    assert refused.value.code == 2
    assert message in capsys.readouterr().err


def _benchmark(tmp_path: Path, start_url: str) -> Path:
    """An ``online_mind2web`` benchmark YAML over one task, in a data directory of its own."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "data_info.json").write_text(json.dumps({"split": {"All": "tasks.json"}}))
    task = {"task_id": "t1", "task_text": "find the kettle", "start_url": start_url}
    (data / "tasks.json").write_text(json.dumps([task]))
    path = tmp_path / "benchmark.yaml"
    block = {
        "benchmark_id": "online_mind2web",
        "task_loader": {"benchmark_name": "online_mind2web", "data_dir": str(data)},
    }
    path.write_text(yaml.safe_dump({"benchmark": [block]}))
    return path


def test_a_benchmark_task_prepares_with_its_own_start(tmp_path: Path) -> None:
    """The task's context is the benchmark's: a start page setup refuses is refused at the dry run."""
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

    argv = ["--config", str(BROWSER_CONFIG), "--task-id", "t1", "--dry-run"]
    good = _benchmark(tmp_path / "good", "https://shop.example/kettles")
    assert run_cli.main([*argv, "--benchmark", str(good)]) == 0
    bare = _benchmark(tmp_path / "bare", "shop.example")
    with pytest.raises(BrowserCooperationError, match="start_url 'shop.example'"):
        run_cli.main([*argv, "--benchmark", str(bare)])


def test_an_unknown_task_id_is_refused_by_name(tmp_path: Path) -> None:
    benchmark = _benchmark(tmp_path, "https://shop.example")
    argv = ["--config", str(BROWSER_CONFIG), "--benchmark", str(benchmark), "--dry-run"]
    with pytest.raises(ValueError, match="--task-id t9: benchmark .* has no such task"):
        run_cli.main([*argv, "--task-id", "t9"])


async def test_a_benchmark_task_runs_with_its_text_context_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectragent.core.runtime import AgentRuntime
    from tests._helpers.trace_builder import agent_result

    seen: dict[str, Any] = {}

    async def execute(self: AgentRuntime, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return agent_result()

    monkeypatch.setattr(AgentRuntime, "execute", execute)
    benchmark = _benchmark(tmp_path, "https://shop.example/kettles")
    code = await run_cli._run(
        config_path=BROWSER_CONFIG,
        task="",
        benchmark=str(benchmark),
        task_id="t1",
        timeout=1,
        dry_run=False,
    )
    assert code == 0
    assert seen["task"] == "find the kettle"
    assert seen["env_context"] == {"start_url": "https://shop.example/kettles"}
    assert (seen["task_id"], seen["benchmark"], seen["subset"]) == (
        "t1",
        "online_mind2web",
        "All",
    )
    assert seen["run_id"].startswith("t1_")
    assert seen["config"]["benchmark"] == "online_mind2web"
