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
            "evaluation": "not_evaluated",
            "trace_dir": "traces/one",
        }
    )
    text = capsys.readouterr().out
    assert "done" in text and "incomplete" in text and "not_evaluated" in text
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

    monkeypatch.setattr(generate_all, "discover", forbidden)
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
        run_cli, "ConfigComposer", lambda **kwargs: SimpleNamespace(compose=lambda raw: run)
    )
    monkeypatch.setattr(run_cli.AgentRuntime, "for_agent", lambda *args: Runtime())
    path = tmp_path / "run.yaml"
    path.write_text("{}")
    report = {}
    code = await run_cli._run(
        config_path=path,
        task="task",
        start_url=None,
        timeout=1,
        dry_run=False,
        report=report.update,
    )
    assert code == 1
    assert report["result"]["agent_done"] == "done"
    assert report["recording"]["status"] == "incomplete"
    assert report["recording"]["complete_through"] == -1
    assert report["evaluation"] == "not_evaluated"
