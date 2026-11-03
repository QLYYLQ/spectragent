"""``spectragent run``: a benchmark's task (``--benchmark``, ``--task-id``) or one written here.

A written task starts where the start flags of the run's capabilities say
(``docs/architecture_design.md`` §4.9 "起始参数"): every capability package
declares its own in ``declarations.py``, ``--help`` lists them by capability,
and a flag of a capability the configuration does not name is refused before
anything starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core import run_cli
from tests._helpers.benchmark_data import online_mind2web_benchmark

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
BROWSER_CONFIG = CONFIGS / "browser_use_local.yaml"
_ON_ANDROID = {
    "environment": [{"env_type": "android", "capabilities": [{"capability_id": "android"}]}]
}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cli, "load_dotenv", lambda: None)  # never read the repo's .env
    monkeypatch.setattr(run_cli.logging, "basicConfig", lambda **_kw: None)


def _main_kwargs(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, Any]:
    """What ``main`` hands the run, without running it."""
    seen: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(run_cli, "_run", _fake_run)
    assert run_cli.main(argv) == 0
    return seen


def test_the_start_context_carries_only_what_was_given() -> None:
    raw = yaml.safe_load(BROWSER_CONFIG.read_text())
    assert run_cli.start_context({}, raw) is None
    assert run_cli.start_context({"--start-url": "https://a.example"}, raw) == {
        "start_url": "https://a.example"
    }
    assert run_cli.start_context({"--replay-archive": "/runs/src"}, raw) == {
        "replay_archive": "/runs/src"
    }


def test_a_relative_replay_archive_is_passed_as_given() -> None:
    """``AgentRuntime.execute`` resolves it, for the CLI and every other caller alike.

    ``tests/test_core/test_runtime/test_env_context_paths.py`` pins that the
    trace and setup both see the absolute directory.
    """
    raw = yaml.safe_load(BROWSER_CONFIG.read_text())
    assert run_cli.start_context({"--replay-archive": "runs/src"}, raw) == {
        "replay_archive": "runs/src"
    }


def test_a_flag_of_a_capability_the_run_does_not_name_is_refused() -> None:
    with pytest.raises(run_cli.StartFlagError) as refused:
        run_cli.start_context({"--start-url": "https://a.example"}, _ON_ANDROID)
    assert str(refused.value) == (
        "--start-url is a start flag of the browser capability, and the configuration names "
        "no browser capability (it names: android)"
    )


def test_the_help_lists_every_start_flag_by_capability(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_cli.main(["--help"])
    help_text = capsys.readouterr().out
    group = help_text[help_text.index("browser start flags:") :]
    for flag in ("--start-url", "--storage-state", "--replay-archive"):
        assert flag in group, flag
    assert "(browser) Page to open before the agent starts" in group


def test_a_start_flag_reaches_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _main_kwargs(
        monkeypatch,
        ["--config", "run.yaml", "--task", "replay it", "--replay-archive", "/runs/src"],
    )
    assert seen["start"] == {"--replay-archive": "/runs/src"}
    assert seen["config_path"] == Path("run.yaml") and seen["task_id"] is None


def test_the_oracle_timeout_flag_reaches_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["--config", "run.yaml", "--task", "score it"]
    assert _main_kwargs(monkeypatch, argv)["oracle_timeout"] == run_cli.ORACLE_TIMEOUT_S
    assert _main_kwargs(monkeypatch, [*argv, "--oracle-timeout", "9"])["oracle_timeout"] == 9.0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--benchmark", "online_mind2web"], "--benchmark and --task-id go together"),
        (["--task-id", "t1"], "--benchmark and --task-id go together"),
        (["--task", "x", "--subset", "Hard"], "--subset selects a benchmark's tasks"),
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


def test_a_dry_run_checks_what_setup_would_refuse() -> None:
    """``--dry-run`` prepares with the flags' task context: the setup check sees it too."""
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

    config = str(BROWSER_CONFIG)
    assert run_cli.main(["--config", config, "--dry-run"]) == 0
    with pytest.raises(BrowserCooperationError, match="network_source: live ↔ replay_archive"):
        run_cli.main(["--config", config, "--dry-run", "--replay-archive", "/runs/src"])


def test_a_start_flag_of_another_capability_launches_nothing(tmp_path: Path) -> None:
    config = tmp_path / "android.yaml"
    config.write_text(yaml.safe_dump(_ON_ANDROID))
    with pytest.raises(run_cli.StartFlagError, match="names no browser capability"):
        run_cli.main(["--config", str(config), "--dry-run", "--start-url", "https://a.example"])


def _benchmark(tmp_path: Path, start_url: str) -> Path:
    """An ``online_mind2web`` benchmark YAML over one task; its ``Hard`` subset has harder text."""
    task = {"task_id": "t1", "task_text": "find the kettle", "start_url": start_url}
    hard = {**task, "task_text": "find the cheapest kettle"}
    return online_mind2web_benchmark(tmp_path, {"All": [task], "Hard": [hard]})


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
    with pytest.raises(ValueError, match=r"subset 'Hard'.*--subset Hard --list lists them"):
        run_cli.main([*argv, "--task-id", "t9", "--subset", "Hard"])


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


async def test_a_subset_selects_the_task_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--subset`` is written into the benchmark's task loader: the run reads and records it."""
    from spectragent.core.composer import ComposedRun, ConfigComposer
    from spectragent.core.runtime import AgentRuntime
    from tests._helpers.trace_builder import agent_result

    seen: dict[str, Any] = {}

    async def execute(self: AgentRuntime, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return agent_result()

    compose = ConfigComposer.compose

    def resolved(self: ConfigComposer, raw: Any) -> ComposedRun:
        run = compose(self, raw)
        assert run.top_level_cfg is not None
        seen["resolved_config"] = run.top_level_cfg.model_dump()
        return run

    monkeypatch.setattr(AgentRuntime, "execute", execute)
    monkeypatch.setattr(ConfigComposer, "compose", resolved)
    code = await run_cli._run(
        config_path=BROWSER_CONFIG,
        task="",
        benchmark=str(_benchmark(tmp_path, "https://shop.example/kettles")),
        subset="Hard",
        task_id="t1",
        timeout=1,
        dry_run=False,
    )
    assert code == 0
    assert seen["task"] == "find the cheapest kettle"
    assert (seen["benchmark"], seen["subset"]) == ("online_mind2web", "Hard")
    assert seen["config"]["subset"] == "Hard"
    (block,) = seen["resolved_config"]["benchmark"]
    assert block["task_loader"]["subset"] == "Hard"
