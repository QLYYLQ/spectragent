"""The command boundary must stay scriptable, safe to inspect and side-effect free on help."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from spectragent.cli import main


def test_delegated_help_includes_display_options(capsys):
    assert main(["run", "--help"]) == 0
    text = capsys.readouterr().out
    assert "--task" in text and "--tui" in text and "--json" in text
    assert "cannot be combined" in text


def test_terminal_captured_run_prints_still_leave_a_final_summary(capsys):
    from spectragent.cli.app import Output

    Output().emit(
        {
            "output": [],
            "result": {"agent_done": "done", "env_status": "success", "answer": "answer"},
            "recording": {"status": "incomplete", "complete_through": 2, "reason": "close failed"},
            "verdicts": ["androidworld_0a1b2c3d: score 1.0"],
            "trace_dir": "traces/one",
        }
    )
    text = capsys.readouterr().out
    assert "done" in text and "incomplete" in text and "androidworld_0a1b2c3d: score 1.0" in text
    assert "traces/one" in text and "close failed" in text


def test_json_run_preserves_exit_code_and_separates_stdout(monkeypatch, capsys):
    from spectragent.core import run_cli

    def run(argv, *, progress, report, **kwargs):
        assert argv == ["--config", "selected.yaml", "--task", "read"]
        assert progress is not None
        print("agent_done=error env_status=failed")
        report({"result": {"agent_done": "error", "env_status": "failed"}})
        return 1

    monkeypatch.setattr(run_cli, "main", run)
    assert main(["run", "--config", "selected.yaml", "--task", "read", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 1
    assert result["result"] == {"agent_done": "error", "env_status": "failed"}
    assert result["output"] == ["agent_done=error env_status=failed"]


def test_cancelled_execution_returns_interrupt_status(monkeypatch, capsys):
    from spectragent.core import run_cli

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(run_cli, "main", cancel)
    assert main(["--json", "run", "--config", "ignored.yaml", "--task", "read"]) == 130
    assert json.loads(capsys.readouterr().out)["interrupted"] is True


def test_closing_terminal_before_operation_starts_closes_owned_coroutine(monkeypatch):
    from spectragent import tui
    from spectragent.cli.app import _display_operation
    from spectragent.monitoring import ProgressState

    async def operation():
        pytest.fail("The operation must not have started")

    def close(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(tui, "run_operation", close)
    pending = operation()
    with pytest.raises(asyncio.CancelledError):
        _display_operation(pending, ProgressState(), tui=True)
    assert pending.cr_frame is None


def test_config_explain_uses_prepare_without_launching_or_exposing_secrets(
    tmp_path, monkeypatch, capsys
):
    from spectragent.core.runtime import AgentRuntime

    async def forbidden(*args, **kwargs):
        pytest.fail("Inspection must not execute a run or set up an environment")

    monkeypatch.setattr(AgentRuntime, "execute", forbidden)
    monkeypatch.setattr(AgentRuntime, "setup_environment", forbidden)
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["llm"]["api_key"] = "secret-config-inspection-sentinel"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    before = sorted(tmp_path.iterdir())
    assert main(["config", "explain", str(path), "--json"]) == 0
    output = capsys.readouterr().out
    assert "secret-config-inspection-sentinel" not in output
    result = json.loads(output)
    assert result["checked"] == "configuration_and_prepare"
    assert result["launched"] is False
    assert "browser.click" in result["negotiation"]["active_actions"]
    assert result["resolved"]["agent"][0]["llm"]["api_key"] == "<omitted>"
    assert result["registry_fingerprints"]["agent"]
    provenance = result["provenance"]
    inputs = {entry["path"]: entry["source"] for entry in provenance["inputs"]}
    assert inputs["agent[0].llm.api_key"] == "explicit"
    assert "secret-config-inspection-sentinel" not in json.dumps(provenance)
    agent_declaration = provenance["declarations"]["agent/browser_use.dom"]
    assert agent_declaration["fields"]["id"]["declared_on"].endswith("AgentBase")
    assert agent_declaration["fields"]["use_thinking"]["declared_on"].endswith("BrowserUseAgent")
    assert agent_declaration["mro"][0].endswith("BrowserUseDomAgent")
    loop_wiring = agent_declaration["wiring"]["agent_loop"]
    assert loop_wiring["winner"]["declared_on"].endswith("BrowserUseAgent")
    assert any(d["disposition"] == "overridden" for d in loop_wiring["declarations"])
    edges = {edge["path"]: edge for edge in provenance["wiring"]}
    assert edges["agent[0].agent_loop"]["source"] == "inline"
    assert edges["environment[0].capabilities[0].actions"]["source"] == "owner_default"
    assert all(
        action["instance_id"] is None and action["path"] is None
        for action in edges["environment[0].capabilities[0].actions"]["targets"]
    )
    assert sorted(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("dropped", "checked", "env_intercepted"),
    [
        ((), "configuration_and_prepare", True),
        (("agent",), "configuration", True),
        (("agent", "environment"), "configuration", False),
    ],
    ids=["prepared", "environment-without-agent", "no-environment"],
)
def test_explain_lists_the_interceptions_the_run_would_weave(
    tmp_path, capsys, dropped, checked, env_intercepted
):
    """``env`` interceptions follow whether there is an environment, prepared or not."""
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    for layer in dropped:
        del raw[layer]
    raw["hook"][0]["trace_dir"] = str(tmp_path / "never-written")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["config", "explain", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["checked"] == checked
    (writer,) = [hook for hook in result["hooks"] if hook["hook_id"] == "trace_writer"]
    points = {(item["role"], item["method"]) for item in writer["intercepts"]}
    assert ("loop", "record_step") in points
    assert (("env", "act") in points) is env_intercepted
    assert (("env", "verify_live") in points) is env_intercepted
    assert not (tmp_path / "never-written").exists()


def test_config_validation_error_does_not_include_input_values(tmp_path, capsys):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["llm"]["api_key"] = {"hidden": "secret-invalid-input-sentinel"}
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--json", "config", "check", str(path)]) == 2
    captured = capsys.readouterr()
    assert "secret-invalid-input-sentinel" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["error"]["type"] == "ValidationError"
    assert result["error"]["stage"] == "validation"
    assert any("api_key" in issue["location"] for issue in result["error"]["issues"])


@pytest.mark.parametrize(
    ("change", "code", "location"),
    [
        (
            lambda raw: raw["environment"][0]["capabilities"][0].update(capability_id="browsr"),
            "unknown_capability_package",
            "environment[0].capabilities[0].capability_id",
        ),
        (lambda raw: raw.pop("environment"), "agent_without_capability", "agent[0].agent_id"),
    ],
)
def test_a_name_refusal_is_a_validation_error(tmp_path, capsys, change, code, location):
    """What a configuration names is its own error, refused before the schema is built."""
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    change(raw)
    path = tmp_path / "named.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--json", "config", "check", str(path)]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["stage"] == "validation"
    assert [(issue["code"], issue["path"]) for issue in error["issues"]] == [(code, location)]


def test_a_loop_that_may_run_no_action_per_reply_is_a_validation_error(tmp_path, capsys):
    """``max_actions_per_step: 0`` is a run that can never act, whatever the agent: the ReAct
    Loop's schema refuses it before anything is composed."""
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["agent_loop"]["max_actions_per_step"] = 0
    path = tmp_path / "no_actions.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["--json", "config", "check", str(path)]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["type"], error["stage"]) == ("ValidationError", "validation")
    assert [
        issue["type"]
        for issue in error["issues"]
        if issue["location"][-1] == "max_actions_per_step"
    ] == ["greater_than_equal"]


def test_yaml_syntax_error_never_echoes_a_credential_line(tmp_path, capsys):
    path = tmp_path / "invalid.yaml"
    path.write_text("llm:\n  api_key: [secret-malformed-yaml-sentinel\n")
    assert main(["--json", "config", "check", str(path)]) == 2
    captured = capsys.readouterr()
    assert "secret-malformed-yaml-sentinel" not in captured.out + captured.err
    error = json.loads(captured.out)["error"]
    assert error["message"] == "Invalid YAML syntax"
    assert error["stage"] == "yaml"
    assert isinstance(error["line"], int) and isinstance(error["column"], int)


def test_view_rejects_a_missing_root_without_a_traceback(tmp_path, capsys):
    assert main(["--json", "view", "--root", str(tmp_path / "absent")]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert json.loads(captured.out)["error"]["type"] == "FileNotFoundError"


def test_generate_help_never_discovers_or_writes(tmp_path, monkeypatch, capsys):
    from spectragent.scripts import generate_all

    def forbidden(*args, **kwargs):
        pytest.fail("Help must not discover or generate")

    monkeypatch.setattr(generate_all, "discover_every_implementation", forbidden)
    monkeypatch.setattr(generate_all, "GENERATED_DIR", tmp_path)
    assert main(["config", "generate", "--help"]) == 0
    with pytest.raises(SystemExit) as exc:
        generate_all.main(["--help"])
    assert exc.value.code == 0
    assert list(tmp_path.iterdir()) == []


def test_missing_input_is_a_clean_json_usage_error(capsys):
    assert main(["--json", "config", "check", "/path/that/does/not/exist"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert json.loads(captured.out)["exit_code"] == 2


def test_components_are_read_from_the_live_registries(capsys):
    assert main(["components", "list", "--layer", "agent", "--json"]) == 0
    agents = json.loads(capsys.readouterr().out)["components"]["agent"]
    assert {"browser_use.dom", "browser_use.dom_vision"} <= set(agents)


def test_tui_and_json_are_refused_before_execution(monkeypatch, capsys):
    from spectragent.core import run_cli

    monkeypatch.setattr(run_cli, "main", lambda *args, **kwargs: pytest.fail("Must not execute"))
    assert main(["--json", "run", "--tui"]) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2


def test_missing_generated_files_are_reported_without_regeneration(tmp_path, monkeypatch, capsys):
    from spectragent.cli import configuration

    generated = tmp_path / "missing-generated"
    monkeypatch.setattr(configuration, "GENERATED_DIR", generated)
    assert main(["--json", "config", "check", "configs/browser_use_local.yaml"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["generated"]["status"] == "missing"
    assert data["generated"]["remedy"] == "spectragent config generate"
    assert not generated.exists()


def test_config_commands_compose_a_benchmark_with_or_without_one_of_its_tasks(tmp_path, capsys):
    """``--benchmark`` alone checks the combined configuration with no task context.

    ``--task-id`` needs ``--benchmark`` and prepares with that task's context,
    so a start setup refuses is refused here; start flags start a task written
    on the command line, never a benchmark's.
    """
    from tests._helpers.benchmark_data import online_mind2web_benchmark

    def benchmark(name: str, start_url: str) -> str:
        task = {"task_id": "t1", "task_text": "find the kettle", "start_url": start_url}
        return str(online_mind2web_benchmark(tmp_path / name, {"All": [task]}))

    argv = ["--json", "config", "explain", "configs/browser_use_local.yaml"]
    good = benchmark("good", "https://shop.example/kettles")
    assert main([*argv, "--benchmark", good]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["checked"] == "configuration_and_prepare"
    (block,) = data["resolved"]["benchmark"]
    assert block["task_loader"]["benchmark_name"] == "online_mind2web"
    assert main([*argv, "--benchmark", good, "--task-id", "t1"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == "configuration_and_prepare"

    bare = benchmark("bare", "shop.example")
    assert main([*argv, "--benchmark", bare]) == 0, "no task, so no start to refuse"
    capsys.readouterr()
    assert main([*argv, "--benchmark", bare, "--task-id", "t1"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["stage"] == "prepare" and "start_url 'shop.example'" in error["message"]

    for given, refusal in (
        (["--task-id", "t1"], "give --benchmark with it"),
        (["--benchmark", good, "--start-url", "https://a.example"], "leave the start flags out"),
    ):
        assert main([*argv, *given]) == 2
        assert refusal in json.loads(capsys.readouterr().out)["error"]["message"]


def test_config_explain_hashes_what_a_run_records_with_the_verifiers_left_out(tmp_path, capsys):
    """Verifiers are no part of a run's recorded configuration, so neither of what explain hashes."""
    import yaml

    from tests._helpers.benchmark_data import online_mind2web_benchmark

    task = {"task_id": "t1", "task_text": "find the kettle", "start_url": "https://shop.example"}
    path = online_mind2web_benchmark(tmp_path, {"All": [task]})
    unjudged = path.read_text()
    raw = yaml.safe_load(unjudged)
    raw["benchmark"][0]["verifiers"] = [
        {
            "verifier_id": "online_mind2web",
            "llm": {
                "model_type": "openai",
                "provider": {"name": "direct"},
                "model": "judge-model",
                "api_key": "test",
            },
        }
    ]
    judged = tmp_path / "judged.yaml"
    judged.write_text(yaml.safe_dump(raw))
    argv = ["--json", "config", "explain", "configs/browser_use_local.yaml", "--benchmark"]

    assert main([*argv, str(judged)]) == 0
    with_verifier = json.loads(capsys.readouterr().out)
    assert main([*argv, str(path)]) == 0
    without = json.loads(capsys.readouterr().out)

    assert with_verifier["resolved_config_sha256"] == without["resolved_config_sha256"]
    (block,) = with_verifier["resolved"]["benchmark"]
    assert block["verifiers"] is None
    assert with_verifier["components"]["verifier"][0]["implementation"] == "online_mind2web"


@pytest.mark.parametrize("command", ["check", "explain"])
def test_config_commands_take_the_subset_of_the_benchmark(tmp_path, capsys, command):
    """``--subset`` is written into the benchmark's task loader before composing, as bench does."""
    from tests._helpers.benchmark_data import online_mind2web_benchmark

    task = {"task_text": "find the kettle", "start_url": "https://shop.example/kettles"}
    benchmark = str(
        online_mind2web_benchmark(
            tmp_path, {"All": [{**task, "task_id": "easy"}], "Hard": [{**task, "task_id": "hard"}]}
        )
    )
    argv = ["--json", "config", command, "configs/browser_use_local.yaml", "--benchmark", benchmark]

    assert main([*argv, "--subset", "Hard", "--task-id", "hard"]) == 0
    hard = json.loads(capsys.readouterr().out)
    assert hard["checked"] == "configuration_and_prepare"
    assert main([*argv, "--task-id", "hard"]) == 2, "the task is in Hard, not the YAML's All"
    assert (
        "(subset 'All') has no such task" in json.loads(capsys.readouterr().out)["error"]["message"]
    )
    assert main(argv) == 0
    all_tasks = json.loads(capsys.readouterr().out)
    # Recorded in the resolved configuration, so its hash, as if the YAML said it.
    assert hard["resolved_config_sha256"] != all_tasks["resolved_config_sha256"]
    if command == "explain":
        (block,) = hard["resolved"]["benchmark"]
        assert block["task_loader"]["subset"] == "Hard"

    without = ["--json", "config", command, "configs/browser_use_local.yaml", "--subset", "Hard"]
    assert main(without) == 2
    assert "give --benchmark with it" in json.loads(capsys.readouterr().out)["error"]["message"]


def _androidworld_m3a_config(tmp_path, monkeypatch, **loop) -> Path:
    """An M3A run on Android for ``--benchmark androidworld``, its Loop AndroidWorld's but *loop*,
    with a fake AndroidWorld installation and data under *tmp_path*."""
    import sys

    from tests._helpers.android_device import fake_service_apk
    from tests._helpers.benchmark_data import androidworld_data
    from tests._helpers.fake_androidworld import fake_installation

    data = androidworld_data(tmp_path / "data" / "android" / "androidworld")
    fake_installation(data / "installation", monkeypatch)
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "data"))
    # The emulator's gRPC token was measured on macOS only (``local_emulator``).
    monkeypatch.setattr(sys, "platform", "darwin")
    android = {
        "capability_id": "android",
        "service_apk": str(fake_service_apk(tmp_path)),
        "tap_injector": {"injector_id": "shell_injection"},
        # No typing_backend: the benchmark supplies AndroidWorld's (key_events).
        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
        "ui_tree_serializer": {"serializer_id": "m3a"},
    }
    androidworld_loop = {
        "loop_id": "react",
        "max_steps": 120,
        "max_actions_per_step": 2,
        "max_consecutive_parse_errors": None,
    }
    agent = {
        "agent_id": "m3a",
        "agent_loop": {**androidworld_loop, **loop},
        "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
        "memory": {"memory_id": "sliding_window", "window_size": 1},
    }
    config = tmp_path / "android.yaml"
    config.write_text(
        yaml.safe_dump(
            {"agent": [agent], "environment": [{"env_type": "android", "capabilities": [android]}]}
        )
    )
    return config


def test_config_check_with_androidworld_alone_merges_its_part_and_checks_without_a_task(
    tmp_path, capsys, monkeypatch
):
    """The shipped ``androidworld`` alias, with no task: its setup checks the installation and
    what it needs of the device, its part of the Android block (the AVD, the ``open_app`` table) is
    merged in, and the task is setup's to refuse (``docs/architecture_design.md`` §4.10.1)."""
    from tests._helpers.benchmark_data import ANDROIDWORLD_APPS

    config = _androidworld_m3a_config(tmp_path, monkeypatch)

    assert main(["--json", "config", "check", str(config), "--benchmark", "androidworld"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == "configuration_and_prepare"
    task = ["--task-id", "SystemBrightnessMax/42/0"]
    assert (
        main(["--json", "config", "check", str(config), "--benchmark", "androidworld", *task]) == 0
    )
    capsys.readouterr()

    assert main(["--json", "config", "explain", str(config), "--benchmark", "androidworld"]) == 0
    explained = json.loads(capsys.readouterr().out)
    (block,) = explained["resolved"]["environment"][0]["capabilities"]
    assert block["backend"]["avd"] == "AndroidWorldAvd"
    assert (block["typing_backend"]["typing_id"], block["settle_max_wait_s"]) == ("key_events", 6.0)
    (open_app,) = [a for a in block["actions"] if a["action_id"] == "android.open_app"]
    assert open_app["apps"] == ANDROIDWORLD_APPS


@pytest.mark.parametrize("command", [["config", "check"], ["run", "--dry-run", "--config"]])
def test_a_loaders_refusal_reaches_the_user_in_its_own_short_words(
    tmp_path, capsys, monkeypatch, command
):
    """A task list of an older format is refused by the loader's constructor; ``config check`` and
    ``run --dry-run`` both show that refusal itself, in one line naming the file and the command
    that writes it again, not a placeholder or pydantic's dump of every task."""
    config = _androidworld_m3a_config(tmp_path, monkeypatch)
    listing = tmp_path / "data" / "android" / "androidworld" / "All.json"
    old = json.loads(listing.read_text())
    old["format"] = "androidworld-tasks-1"
    old["tasks"] = old["tasks"] * 40
    listing.write_text(json.dumps(old))
    task = ["--task-id", "SystemBrightnessMax/42/0"]

    argv = ["--json", *command, str(config), "--benchmark", "androidworld", *task]
    assert main(argv) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    message = error["message"]
    assert f"{listing} is not an AndroidWorld task list of format androidworld-tasks-2" in message
    assert "spectragent benchmark fetch androidworld" in message
    assert "\n" not in message and len(message) < 500
    if command[0] == "config":
        assert error["issues"][0]["code"] == "component_refused"


def test_config_check_refuses_a_loop_m3a_cannot_run_on(tmp_path, capsys, monkeypatch):
    """M3A's ``input_text`` runs as two actions: one per reply is refused by ``prepare()``, so the
    check reports it rather than every task refusing at its start (``RunSettingsCheck``)."""
    config = _androidworld_m3a_config(tmp_path, monkeypatch, max_actions_per_step=1)
    task = ["--task-id", "SystemBrightnessMax/42/0"]

    argv = ["--json", "config", "check", str(config), "--benchmark", "androidworld", *task]
    assert main(argv) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["type"], error["stage"]) == ("ValueError", "prepare")
    assert error["message"].endswith("needs the Loop's max_actions_per_step at 2 or more; it is 1")


def test_human_explain_shows_negotiation_without_dumping_resolved_config(capsys):
    assert main(["config", "explain", "configs/browser_use_local.yaml"]) == 0
    text = capsys.readouterr().out
    assert "Guarded actions:" in text and "browser.click" in text
    assert "trace_writer" in text and "record_step" in text
    assert "Field inputs:" in text and "owner_default" in text
    assert '"resolved"' not in text


def test_config_prepare_failure_exposes_the_existing_negotiation_contract(tmp_path, capsys):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["environment"][0]["capabilities"][0]["actions"] = []
    path = tmp_path / "missing_actions.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["config", "explain", str(path), "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["stage"] == "prepare"
    assert error["contract"] == "actions"
    assert error["missing"] and error["provided"] == []


def test_a_refused_loop_input_is_named_in_the_contract():
    from spectragent.cli.configuration_errors import contract_details
    from spectragent.core.loops import ReactLoop
    from spectragent.core.loops.validation import IncompatibleLoopInputError

    with pytest.raises(IncompatibleLoopInputError) as refused:
        ReactLoop().check_loop_inputs({"max_steps": 0})
    assert contract_details(refused.value) == {
        "contract": "loop_inputs",
        "loop": "react",
        "missing": [],
        "unknown": [],
        "mistyped": {},
        "refused": {"max_steps": "must be a positive integer, got 0"},
    }


def test_composition_diagnosis_does_not_echo_constructor_exception_values():
    from spectragent.cli.app import _validation_error
    from spectragent.cli.configuration_errors import ConfigInspectionError
    from spectragent.core.composer import InstantiationError
    from spectragent.core.composition_evidence import CompositionIssue

    cause = InstantiationError(
        "secret-constructor-failure-sentinel",
        issue=CompositionIssue(
            "constructor_failed", "agent[0]", "Constructor refused config", "Check its contract"
        ),
    )
    report = _validation_error(ConfigInspectionError("composition", cause))
    assert report["stage"] == "composition"
    assert report["issues"][0]["path"] == "agent[0]"
    assert report["issues"][0]["code"] == "constructor_failed"
    assert "secret-constructor-failure-sentinel" not in json.dumps(report)


async def test_agent_done_and_incomplete_recording_remain_separate(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from spectragent.core import run_cli
    from spectragent.core.trace.models import TraceConfig, TraceStatus
    from spectragent.core.trace.wal import WALWriter
    from tests._helpers.trace_builder import agent_result

    wal = WALWriter(tmp_path, "incomplete")
    wal.init_run(TraceConfig())
    wal.finalize(
        TraceStatus.INCOMPLETE, incomplete_reason="terminal observation could not be recorded"
    )
    result = agent_result(agent_done="done", error=None)

    class Runtime:
        agent = SimpleNamespace(
            agent_id="fake",
            agent_loop=SimpleNamespace(loop_id="react"),
            llm=SimpleNamespace(model="fake"),
        )
        env = None

        async def execute(self, **kwargs):
            return result

    run = SimpleNamespace(
        ids=lambda layer: ["fake"],
        instances=lambda layer: [],
        by_layer={},
        hooks=[SimpleNamespace(run_dir=wal.run_dir)],
    )
    monkeypatch.setattr(
        run_cli,
        "ConfigComposer",
        SimpleNamespace(for_config=lambda raw: SimpleNamespace(compose=lambda raw: run)),
    )
    monkeypatch.setattr(run_cli.AgentRuntime, "for_agent", lambda *args: Runtime())
    path = tmp_path / "run.yaml"
    path.write_text("{}")
    report = {}
    code = await run_cli._run(
        config_path=path,
        task="task",
        timeout=1,
        dry_run=False,
        report=report.update,
    )
    assert code == 1
    assert report["result"]["agent_done"] == "done"
    assert report["recording"]["status"] == "incomplete"
    assert report["recording"]["complete_through"] == -1
    assert report["verdicts"] == []


def test_benchmark_fetch_writes_the_pinned_data_and_says_which_version(
    tmp_path, capsys, monkeypatch
):
    """``spectragent benchmark fetch`` (``docs/architecture_design.md`` §4.10.1 "数据"):
    BrowseComp's file, stood in for, into the shared cache."""
    from spectragent.benchmarks.browsecomp import data as browsecomp_data

    def download(url, sha256, target, say):
        say(f"downloading {url}")
        target.write_text("problem,answer,problem_topic,canary\nq,a,Art,c\n")
        return target

    monkeypatch.setattr(browsecomp_data, "download_file", download)
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))

    assert main(["--json", "benchmark", "fetch", "browsecomp"]) == 0
    captured = capsys.readouterr()
    fetched = json.loads(captured.out)
    assert fetched["directory"] == str(tmp_path / "browser" / "browsecomp")
    assert fetched["data_version"] == browsecomp_data.DATA.model_dump(mode="json")
    assert "downloading https://openaipublic" in captured.err

    # Now there, the run commands read it.
    argv = ["--json", "config", "check", "configs/browser_use_local.yaml"]
    assert main([*argv, "--benchmark", "browsecomp"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "check", "configs/browser_use_local.yaml", "--benchmark", "browsecomp"],
        ["bench", "--config", "configs/browser_use_local.yaml", "--benchmark", "browsecomp"],
        ["run", "--config", "configs/browser_use_local.yaml", "--benchmark", "browsecomp"]
        + ["--task-id", "browsecomp_000", "--dry-run"],
    ],
)
def test_missing_benchmark_data_is_refused_naming_the_fetch_command(
    tmp_path, capsys, monkeypatch, argv
):
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))
    assert main(["--json", *argv]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == (
        f"there is no browsecomp data at {tmp_path / 'browser' / 'browsecomp'}: fetch it with "
        f"`spectragent benchmark fetch browsecomp` (run and bench fetch it first when given "
        f"--fetch)"
    )
    assert not (tmp_path / "browser" / "browsecomp").exists(), "nothing was downloaded"


def test_fetch_goes_with_a_benchmark(capsys):
    assert main(["run", "--config", "configs/browser_use_local.yaml", "--fetch", "--dry-run"]) == 2
    assert "--fetch fetches a benchmark's data" in capsys.readouterr().err
