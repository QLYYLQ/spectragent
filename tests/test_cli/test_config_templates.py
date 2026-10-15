"""Templates remain ordinary configuration with the same effective behavior."""

import copy
import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Annotated, Protocol, runtime_checkable

import pytest
import yaml
from conscribe import create_registrar
from pydantic import BaseModel, Field, ValidationError

from spectragent.cli import main
from spectragent.cli.config_templates import TemplateChoices, render_config_template
from spectragent.core.composer import ConfigComposer, UnresolvedWiringError
from spectragent.core.runtime import AgentRuntime
from spectragent.scripts.generate_all import _collect_registrars


@pytest.fixture(scope="module")
def composer():
    return ConfigComposer(registrars=_collect_registrars())


def _browser(raw):
    return raw["environment"][0]["capabilities"][0]


def _action_state(run):
    browser = run.single("capability")
    return {
        action.action_id: {
            field.name: getattr(action, field.name) for field in fields(action) if field.init
        }
        if is_dataclass(action)
        else {}
        for action in browser.actions
    }


def test_new_template_is_explicit_and_prepares_without_launch(composer, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Authoring and preparation must not start an environment or a model")

    monkeypatch.setattr(AgentRuntime, "execute", forbidden)
    monkeypatch.setattr(AgentRuntime, "setup_environment", forbidden)
    template = render_config_template(TemplateChoices(model="test-model"), composer=composer)
    raw = yaml.safe_load(template)
    assert list(raw) == ["agent", "environment", "hook"]
    assert raw["agent"][0]["llm"]["model"] == "test-model"
    assert raw["agent"][0]["memory"]["memory_id"] == "simple"
    assert "action" not in raw["agent"][0] and "observation" not in raw["agent"][0]
    browser = _browser(raw)
    assert browser["network_source"] == {"source_id": "live"}
    assert browser["replayability"] == {"replayability_id": "none"}
    assert "archiver" not in browser and "archiver" not in raw
    assert len(browser["actions"]) == 16
    writer = raw["hook"][0]
    assert writer["recording"] == {
        "metrics": True,
        "errors": False,
        "trajectory": False,
        "observations": False,
    }
    assert writer["archive"] == []
    assert "Source: AF template default" in template
    assert "Source: --model" in template
    assert "Required by the selected component" in template
    assert "Maximum sealed segments awaiting persistence" in template
    assert "'minimum': 1" in template
    run = composer.compose(raw)
    runtime = AgentRuntime.for_agent(run, "main")
    runtime.prepare()
    assert runtime.active_descriptors


def test_default_action_expansion_preserves_every_effective_parameter(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    assert "actions" not in _browser(raw)
    before = composer.compose(raw)
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    after = composer.compose(expanded)
    assert _action_state(before) == _action_state(after)
    assert (
        before.single("capability").provided_actions == after.single("capability").provided_actions
    )
    assert before.single("hook").recording == after.single("hook").recording
    assert expanded["hook"][0]["recording"] == {
        "metrics": True,
        "errors": True,
        "trajectory": True,
        "observations": True,
    }


@pytest.mark.parametrize(
    "actions", [None, [], [{"action_id": "browser.scroll", "default_amount": 777}]]
)
def test_null_empty_and_explicit_action_subset_keep_their_meanings(composer, actions):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    _browser(raw)["actions"] = actions
    before = composer.compose(raw)
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    after = composer.compose(expanded)
    assert _action_state(before) == _action_state(after)
    if actions == []:
        assert _browser(expanded)["actions"] == []


def test_top_level_action_pool_is_not_duplicated_by_default_expansion(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["action"] = [{"action_id": "browser.scroll", "default_amount": 999}]
    before = composer.compose(raw)
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    assert _browser(expanded)["actions"] is None
    assert expanded["action"][0]["default_amount"] == 999
    assert _action_state(before) == _action_state(composer.compose(expanded))


def test_source_fields_nested_discriminators_and_multiline_values_round_trip(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    agent = raw["agent"][0]
    message = "第一行: yes\n  # quoted text\n'null' and false\n"
    agent["extend_system_message"] = message
    agent["llm"]["provider"]["organization"] = "yes"
    agent["llm"]["provider"]["project"] = "null"
    original = copy.deepcopy(raw)
    template = render_config_template(TemplateChoices(llm="openai.direct"), raw, composer=composer)
    assert raw == original
    expanded = yaml.safe_load(template)
    assert expanded["agent"][0]["extend_system_message"] == message
    assert expanded["agent"][0]["llm"]["model"] == agent["llm"]["model"]
    assert expanded["agent"][0]["llm"]["provider"] == agent["llm"]["provider"]
    before = composer.compose(raw).single("agent").llm
    after = composer.compose(expanded).single("agent").llm
    assert before.provider == after.provider
    assert before.model == after.model


def test_changing_agent_keeps_selected_children_and_resets_agent_knobs(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["llm"]["model"] = "chosen-model"
    raw["agent"][0]["agent_loop"]["max_steps"] = 7
    expanded = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="ui_tars"),
            raw,
            composer=composer,
        )
    )
    agent = expanded["agent"][0]
    assert agent["agent_id"] == "ui_tars"
    assert "flash_mode" not in agent
    assert agent["llm"]["model"] == "chosen-model"
    assert agent["agent_loop"]["max_steps"] == 7
    AgentRuntime.for_agent(composer.compose(expanded), "main").prepare()


def test_replayability_does_not_change_network_or_archive(composer):
    raw = yaml.safe_load(Path("configs/flywheel/replay_ui_tars_archive.yaml").read_text())
    expanded = yaml.safe_load(
        render_config_template(
            TemplateChoices(recording=("metrics", "errors")),
            raw,
            composer=composer,
        )
    )
    assert _browser(expanded)["network_source"]["source_id"] == "archive"
    assert _browser(expanded)["network_source"]["miss_policy"]["miss_policy_id"] == "strict"
    assert _browser(expanded)["replayability"]["replayability_id"] == "pinned"
    assert [item["archiver_id"] for item in expanded["hook"][0]["archive"]] == [
        item["archiver_id"] for item in raw["hook"][0]["archive"]
    ]
    assert "archiver" not in expanded
    assert expanded["hook"][0]["recording"]["errors"]
    assert not expanded["hook"][0]["recording"]["trajectory"]


def test_pinned_new_template_keeps_live_network_without_archive(composer):
    expanded = yaml.safe_load(
        render_config_template(
            TemplateChoices(model="selected", replayability="pinned", recording=("none",)),
            composer=composer,
        )
    )
    assert _browser(expanded)["replayability"]["random_seed"] is not None
    assert _browser(expanded)["network_source"]["source_id"] == "live"
    assert expanded["hook"][0]["archive"] == []
    assert not any(expanded["hook"][0]["recording"].values())


def test_unselected_optional_components_are_not_expanded(composer):
    raw = {"hook": [{"hook_id": "trace_writer", "archive": None}]}
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    assert list(expanded) == ["hook"]
    assert expanded["hook"][0]["archive"] is None
    assert "llm" not in expanded["hook"][0]


def test_populated_declaration_is_refused_instead_of_silently_dropped(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["action"] = "browser.click"
    with pytest.raises(UnresolvedWiringError, match="negotiation-only"):
        render_config_template(TemplateChoices(), raw, composer=composer)


def test_required_model_is_never_invented(composer):
    with pytest.raises(ValidationError) as error:
        render_config_template(TemplateChoices(), composer=composer)
    assert any(issue["loc"][-1] == "model" for issue in error.value.errors())


def test_invalid_component_and_inline_only_root_are_rejected(composer):
    with pytest.raises(ValueError, match="Unknown agent implementation"):
        render_config_template(
            TemplateChoices(agent="does_not_exist", model="test"), composer=composer
        )
    with pytest.raises(ValidationError, match="inline"):
        render_config_template(
            TemplateChoices(),
            {"archiver": [{"archiver_id": "browser.network"}]},
            composer=composer,
        )


def test_multi_agent_skeleton_expands_but_flags_do_not_pick_an_arbitrary_agent(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    second = copy.deepcopy(raw["agent"][0])
    second["id"] = "second"
    raw["agent"].append(second)
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    assert [agent["id"] for agent in expanded["agent"]] == ["main", "second"]
    with pytest.raises(ValueError, match="exactly one agent"):
        render_config_template(TemplateChoices(model="different"), raw, composer=composer)


def test_schema_drives_new_component_fields_aliases_and_comments():
    @runtime_checkable
    class Surface(Protocol):
        pass

    registrar = create_registrar("template_probe", Surface, discriminator_field="component")

    class Settings(BaseModel):
        threshold: int = Field(default=4, ge=1, description="New nested research threshold")

    class ProbeBase(metaclass=registrar.Meta):
        __abstract__ = True

    class Probe(ProbeBase):
        __registry_key__ = "probe"

        def __init__(
            self,
            limit: Annotated[int, Field(alias="sample_limit", ge=1, description="A new knob")] = 3,
            settings: Settings = Settings(),  # noqa: B008 -- exercise a declared nested default
        ):
            self.limit = limit
            self.settings = settings

    composer = ConfigComposer(registrars={"template_probe": registrar}, discover_roots=())
    text = render_config_template(
        TemplateChoices(),
        {"template_probe": [{"component": "probe"}]},
        composer=composer,
    )
    expanded = yaml.safe_load(text)
    assert expanded == {
        "template_probe": [
            {
                "component": "probe",
                "sample_limit": 3,
                "settings": {"threshold": 4},
            }
        ]
    }
    assert "A new knob" in text and "New nested research threshold" in text
    assert "'minimum': 1" in text


def test_cli_stdout_is_yaml_and_output_file_is_not_overwritten(tmp_path, capsys):
    assert main(["config", "template", "--model", "test-model"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    raw = yaml.safe_load(captured.out)
    assert raw["agent"][0]["llm"]["model"] == "test-model"
    output = tmp_path / "experiment.yaml"
    assert main(["config", "template", "--model", "test-model", "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "Wrote" in captured.err
    original = output.read_bytes()
    assert main(["--json", "config", "template", "--model", "other", "--output", str(output)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "FileExistsError"
    assert output.read_bytes() == original


def test_cli_missing_field_and_invalid_recording_are_actionable(tmp_path, capsys):
    output = tmp_path / "missing.yaml"
    assert main(["config", "template", "--json", "--output", str(output)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert any(issue["location"][-1] == "model" for issue in result["error"]["issues"])
    assert not output.exists()
    assert main(["config", "template", "--model", "test", "--record", "tokens", "--json"]) == 2
    assert "Unknown recording channels" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert (
        main(
            [
                "config",
                "template",
                "--model",
                "test",
                "--record",
                "none",
                "--record",
                "metrics",
                "--json",
            ]
        )
        == 2
    )
    assert "cannot be combined" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_cli_from_preserves_source_and_json_can_carry_yaml(tmp_path, capsys):
    source = tmp_path / "source.yaml"
    source.write_text("hook:\n  - hook_id: trace_writer\n    archive: []\n")
    original = source.read_bytes()
    assert (
        main(
            [
                "config",
                "template",
                "--from",
                str(source),
                "--record",
                "metrics",
                "--record",
                "errors",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    raw = yaml.safe_load(result["yaml"])
    assert raw["hook"][0]["recording"] == {
        "metrics": True,
        "errors": True,
        "trajectory": False,
        "observations": False,
    }
    assert source.read_bytes() == original


def test_help_does_not_discover_or_construct(monkeypatch, capsys):
    from spectragent.cli import config_templates

    monkeypatch.setattr(
        config_templates, "ConfigComposer", lambda **kwargs: pytest.fail("help composed")
    )
    assert main(["config", "template", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--from" in help_text and "--replayability" in help_text
    assert "--model" in help_text


def test_output_is_deterministic_for_same_selections(composer):
    choices = TemplateChoices(model="test-model")
    assert render_config_template(choices, composer=composer) == render_config_template(
        choices, composer=composer
    )
