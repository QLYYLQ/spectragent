"""Templates remain ordinary configuration with the same effective behavior.

``docs/architecture_design.md`` §4.17: the framework starts the agent, the
agent's wiring picks the environment, each capability's declarations start its
block and give its template flags, and with a benchmark the template leaves out
every capability setting the benchmark supplies, naming each where it would sit.
"""

import copy
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, ClassVar, Protocol, runtime_checkable

import pytest
import yaml
from conscribe import create_registrar
from pydantic import BaseModel, Field, JsonValue, ValidationError

from spectragent.agents.browser_use.dom import BrowserUseDomAgent
from spectragent.agents.m3a.agent import M3AAgent
from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FILE, OPEN_APP_FORMAT
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.benchmarks.androidworld.tasks import instance_seed
from spectragent.benchmarks.selection import BenchmarkSelectionError, read_benchmark, run_root
from spectragent.cli import main
from spectragent.cli.config_templates import TemplateChoices, render_config_template
from spectragent.core.composer import ComposeError, ConfigComposer, UnresolvedWiringError
from spectragent.core.registries import (
    TemplateFlag,
    capability_declarations,
    collect_registrars,
    template_flags,
)
from spectragent.core.runtime import AgentRuntime
from tests._helpers.android_device import fake_service_apk

#: A two-name ``open_app`` table, as AndroidWorld's task list writer would read it.
TABLE = {
    "clock": "com.google.android.deskclock/com.android.deskclock.DeskClock",
    "markor": "net.gsantner.markor/net.gsantner.markor.activity.MainActivity",
}


@pytest.fixture(scope="module")
def composer():
    """The Composer ``config template`` builds: the whole catalog."""
    return ConfigComposer.for_every_package()


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
    assert len(browser["actions"]) == 15
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


def test_unrelated_action_pool_does_not_hide_browser_default_actions():
    from spectragent.core.capabilities.browser.actions.wait import WaitAction

    class ForeignWait(WaitAction):
        __registry_key__ = "other.wait"

    composer = ConfigComposer.for_every_package()
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["action"] = [{"action_id": "other.wait"}]
    before = composer.compose(raw)
    expanded = yaml.safe_load(render_config_template(TemplateChoices(), raw, composer=composer))
    assert len(_browser(expanded)["actions"]) == 15
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
            TemplateChoices(
                model="selected",
                recording=("none",),
                capability_flags={"--replayability": "pinned"},
            ),
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

    composer = ConfigComposer(registrars={"template_probe": registrar})
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
        config_templates,
        "ConfigComposer",
        SimpleNamespace(for_every_package=lambda: pytest.fail("help composed")),
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


def test_cursor_presentation_knobs_expand_validate_compose_and_explain(
    composer,
    tmp_path,
    capsys,
):
    template = render_config_template(TemplateChoices(model="test-model"), composer=composer)
    raw = yaml.safe_load(template)
    assert _browser(raw)["screenshot_cursor"] is False
    assert _browser(raw)["screenshot_cursor_size"] == 32
    assert "Model image cursor height in CSS pixels" in template
    _browser(raw).update(screenshot_cursor=True, screenshot_cursor_size=40)
    expanded = render_config_template(TemplateChoices(), raw, composer=composer)
    run = composer.compose(yaml.safe_load(expanded))
    capability = run.single("capability")
    assert capability.screenshot_cursor is True and capability.screenshot_cursor_size == 40
    source = tmp_path / "cursor.yaml"
    source.write_text(expanded)
    assert main(["config", "explain", str(source), "--json"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert "screenshot_cursor" in json.dumps(explanation)
    assert "screenshot_cursor_size" in json.dumps(explanation)
    for invalid in (15, 97):
        _browser(raw)["screenshot_cursor_size"] = invalid
        with pytest.raises(ValidationError, match="screenshot_cursor_size"):
            composer.compose(raw)
    _browser(raw)["screenshot_cursor_size"] = 32
    _browser(raw)["screenshot_cursor"] = "typo"
    with pytest.raises(ValidationError, match="screenshot_cursor"):
        composer.compose(raw)


# ── every capability: its own starting block, the agent's environment ──


def test_the_agents_wiring_picks_the_environment_and_its_declared_starting_block(composer):
    template = render_config_template(TemplateChoices(agent="m3a", model="m"), composer=composer)
    raw = yaml.safe_load(template)
    (environment,) = raw["environment"]
    assert (environment["env_type"], environment["id"]) == ("android", "android_env")
    (android,) = environment["capabilities"]
    (declared,) = capability_declarations(["android"])
    for name, start in declared.template_block.items():
        if isinstance(start, dict):
            assert {key: android[name][key] for key in start} == start, name
        else:
            assert android[name] == start, name
    # M3A's wiring takes only sliding_window, not the template's simple memory.
    assert raw["agent"][0]["memory"]["memory_id"] == "sliding_window"
    assert "browser" not in template
    composer.compose(raw)


def test_m3a_starts_from_the_run_settings_its_own_prepare_requires(composer, tmp_path):
    raw = yaml.safe_load(
        render_config_template(TemplateChoices(agent="m3a", model="m"), composer=composer)
    )
    agent = raw["agent"][0]
    for receptor, start in M3AAgent.__template_block__.items():
        assert isinstance(start, dict)
        assert {key: agent[receptor][key] for key in start} == start, receptor
    assert agent["agent_loop"]["cut_off_reply"] == "read_as_is"
    # With the APK placeholder replaced, prepare() passes M3A's own run-settings check.
    raw["environment"][0]["capabilities"][0]["service_apk"] = str(fake_service_apk(tmp_path))
    AgentRuntime.for_agent(composer.compose(raw), "main").prepare()
    # Naming the declared Loop keeps its values; another Loop would reset them.
    again = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="m3a", loop="react", model="m"), composer=composer
        )
    )
    assert again["agent"][0]["agent_loop"] == agent["agent_loop"]


def test_an_agents_declared_loop_stands_where_its_wiring_refuses_the_frameworks():
    from spectragent.agents.ui_tars.agent import UITarsAgent
    from spectragent.core.loops.react import ReactLoop

    class SecondReact(ReactLoop):
        __registry_key__ = "second_react"

    class PinnedLoopAgent(UITarsAgent):
        __registry_key__ = "pinned_loop"
        __wiring__: ClassVar[dict[str, Any]] = {
            "agent_loop": ("agent_loop", ["branch", "second_react"])
        }
        __template_block__: ClassVar[Mapping[str, JsonValue]] = {
            "agent_loop": {"loop_id": "second_react"}
        }

    # Built after the two registrations, without discovery.
    composer = ConfigComposer(registrars=collect_registrars())
    raw = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="pinned_loop", environment="browser", model="m"),
            composer=composer,
        )
    )
    assert raw["agent"][0]["agent_loop"]["loop_id"] == "second_react"


def test_a_receptor_the_agent_declares_nothing_for_starts_from_the_frameworks_choice(composer):
    assert "memory" not in BrowserUseDomAgent.__template_block__
    raw = yaml.safe_load(render_config_template(TemplateChoices(model="m"), composer=composer))
    agent = raw["agent"][0]
    assert agent["agent_loop"]["loop_id"] == "react"
    assert agent["memory"]["memory_id"] == "simple"
    assert (agent["llm"]["model_type"], agent["llm"]["provider"]["name"]) == ("openai", "direct")


# ── every agent: its upstream settings (user ruling 2026-11-04) ──────

#: Each agent's official starting values, as its upstream project sets them;
#: the citations are on each ``__template_block__``.
OFFICIAL = [
    # browser-use 6e2add1c: ChatOpenAI 0.2 / 0.3 (browser_use/llm/openai/chat.py:37-38),
    # AgentSettings.max_actions_per_step 5 (agent/views.py:70), Agent.run max_steps 500
    # (agent/service.py:2468).
    (
        "browser_use.dom",
        None,
        {"temperature": 0.2, "frequency_penalty": 0.3},
        {"max_actions_per_step": 5, "max_steps": 500},
    ),
    (
        "browser_use.dom_vision",
        None,
        {"temperature": 0.2, "frequency_penalty": 0.3},
        {"max_actions_per_step": 5, "max_steps": 500},
    ),
    # Skyvern bd68e1af: LLM_CONFIG_TEMPERATURE 0 (skyvern/config.py:196), no penalty;
    # MAX_STEPS_PER_RUN 10 (skyvern/config.py:58).
    ("skyvern.dom", None, {"temperature": 0.0, "frequency_penalty": None}, {"max_steps": 10}),
    (
        "skyvern.dom_vision",
        None,
        {"temperature": 0.0, "frequency_penalty": None},
        {"max_steps": 10},
    ),
    # UI-TARS 582f3a7e: temperature 0.0, frequency_penalty None (README_deploy.md:99-105);
    # one action a step (codes/ui_tars/prompt.py:3-9).
    (
        "ui_tars",
        "browser",
        {"temperature": 0.0, "frequency_penalty": None},
        {"max_actions_per_step": 1},
    ),
    # AndroidWorld 55ddaf5e: Gpt4Wrapper temperature 0.0, no penalty
    # (android_world/agents/infer.py:261, 290-300).
    (
        "m3a",
        None,
        {"temperature": 0.0, "frequency_penalty": None},
        {"max_actions_per_step": 2, "parse_retries_per_step": 0},
    ),
]


@pytest.mark.parametrize(("agent_id", "environment", "llm", "loop"), OFFICIAL, ids=lambda v: str(v))
def test_each_agent_starts_from_its_upstream_settings(composer, agent_id, environment, llm, loop):
    raw = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent=agent_id, environment=environment, model="m"),
            composer=composer,
        )
    )
    agent = raw["agent"][0]
    assert {name: agent["llm"][name] for name in llm} == llm
    assert {name: agent["agent_loop"][name] for name in loop} == loop
    composer.compose(raw)


@pytest.mark.parametrize(
    ("llm", "written"),
    [
        ("openai.direct", {"temperature": 0.2, "frequency_penalty": 0.3}),
        ("openai.deepseek", {"temperature": 0.2, "frequency_penalty": 0.3}),
        # Anthropic and Google have no frequency_penalty; Ollama takes neither.
        ("anthropic.direct", {"temperature": 0.2}),
        ("google.default", {"temperature": 0.2}),
        ("ollama.default", {}),
    ],
)
def test_an_agents_sampling_values_go_into_the_provider_chosen_where_it_has_the_field(
    composer, llm, written
):
    """The values name no provider: each lands only where the chosen provider has the field."""
    raw = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="browser_use.dom", llm=llm, model="m"), composer=composer
        )
    )
    block = raw["agent"][0]["llm"]
    fields = set(composer.catalog.components["llm"][llm].config_model.model_fields)
    for name in {"temperature", "frequency_penalty"} & fields - set(written):
        assert block[name] is None, name
    assert {name: block[name] for name in written} == written
    composer.compose(raw)


def test_changing_the_model_provider_of_a_skeleton_keeps_the_agents_sampling_values(composer):
    source = yaml.safe_load(
        render_config_template(TemplateChoices(agent="m3a", model="m"), composer=composer)
    )
    expanded = yaml.safe_load(
        render_config_template(
            TemplateChoices(llm="anthropic.direct", model="claude"), source, composer=composer
        )
    )
    llm = expanded["agent"][0]["llm"]
    assert (llm["model_type"], llm["temperature"]) == ("anthropic", 0.0)


def test_every_environment_starts_from_its_capabilities_declared_blocks(composer):
    for environment in composer.catalog.components["environment"]:
        raw = yaml.safe_load(
            render_config_template(
                TemplateChoices(environment=environment, model="m"), composer=composer
            )
        )
        blocks = raw["environment"][0]["capabilities"]
        for block, declared in zip(
            blocks, capability_declarations(b["capability_id"] for b in blocks), strict=True
        ):
            assert set(declared.template_block) <= set(block), environment


def test_an_agent_whose_wiring_runs_in_several_environments_needs_one_named(composer):
    with pytest.raises(
        ValueError, match="can run in android, browser; choose one with --environment"
    ):
        render_config_template(TemplateChoices(agent="ui_tars", model="m"), composer=composer)
    raw = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="ui_tars", environment="android", model="m"), composer=composer
        )
    )
    assert raw["environment"][0]["capabilities"][0]["capability_id"] == "android"


def test_a_template_flag_needs_its_capabilitys_block(composer):
    with pytest.raises(ValueError, match="--replayability is a template flag of the browser"):
        render_config_template(
            TemplateChoices(agent="m3a", model="m", capability_flags={"--replayability": "pinned"}),
            composer=composer,
        )


def test_changing_environment_and_agent_restarts_what_the_new_wiring_does_not_take(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["llm"]["model"] = "chosen-model"
    expanded = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="m3a", environment="android"), raw, composer=composer
        )
    )
    (environment,) = expanded["environment"]
    assert environment["env_type"] == "android"
    assert [block["capability_id"] for block in environment["capabilities"]] == ["android"]
    agent = expanded["agent"][0]
    # Simple memory is not M3A's: it restarts from M3A's own starting values.
    start = M3AAgent.__template_block__["memory"]
    assert isinstance(start, dict)
    assert {key: agent["memory"][key] for key in start} == start
    assert agent["llm"]["model"] == "chosen-model", "a receptor the new agent takes is kept"
    # The skeleton's ReAct Loop is M3A's too: its written values win.
    assert agent["agent_loop"]["max_actions_per_step"] == 4


def test_a_loop_written_without_its_omittable_receptor_is_kept_when_the_agent_changes(composer):
    raw = yaml.safe_load(Path("configs/browser_use_local.yaml").read_text())
    raw["agent"][0]["agent_loop"] = {"loop_id": "branch"}
    expanded = yaml.safe_load(
        render_config_template(TemplateChoices(agent="ui_tars"), raw, composer=composer)
    )
    assert expanded["agent"][0]["agent_loop"]["loop_id"] == "branch"


def test_cli_lists_template_flags_by_capability_and_refuses_one_without_its_block(capsys):
    assert main(["config", "template", "--help"]) == 0
    text = capsys.readouterr().out
    assert "browser template flags" in text
    for flag in template_flags()["browser"]:
        assert flag.flag in text
    argv = ["config", "template", "--agent", "m3a", "--model", "m", "--json"]
    assert main([*argv, "--replayability", "pinned"]) == 2
    assert "browser capability" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_a_capability_flag_that_shadows_a_framework_flag_is_refused(monkeypatch, capsys):
    from spectragent.core.capabilities.android import declarations

    monkeypatch.setattr(declarations, "TEMPLATE_FLAGS", (TemplateFlag("--model", "backend", "x"),))
    assert main(["config", "template", "--json", "--help"]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "android capability declares --model" in message
    assert "the template command already takes it" in message


# ── with a benchmark: what it supplies stays out of the run configuration ──


def _androidworld_data(root: Path) -> Path:
    """A small AndroidWorld data directory: one task, a two-name table."""
    data = root / "data"
    data.mkdir(parents=True)
    (data / OPEN_APP_FILE).write_text(
        json.dumps({"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT, "apps": TABLE})
    )
    (data / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    task = {
        "commit": PINNED_COMMIT,
        "family": "android",
        "task": "SystemBrightnessMax",
        "index": 0,
        "suite_seed": 42,
        "params": {"max_or_min": "max", "seed": instance_seed(42, "SystemBrightnessMax", 0)},
        "goal": "Turn brightness to the max value.",
        "goal_at_initialize": "unchanged",
        "complexity": 1.0,
        "start_on_home_screen": True,
    }
    (data / "All.json").write_text(json.dumps({"format": "androidworld-tasks-2", "tasks": [task]}))
    return data


def _androidworld(root: Path) -> Path:
    """An AndroidWorld benchmark YAML over a small data directory: one task, a two-name table."""
    data = _androidworld_data(root)
    benchmark = root / "androidworld.yaml"
    block = {
        "benchmark_id": "androidworld",
        "task_loader": {"benchmark_name": "androidworld", "data_dir": str(data)},
        "setup": {"setup_id": "androidworld"},
        "capability": [
            {
                "capability_id": "android",
                "backend": {"provider_id": "local_emulator", "avd": "AW"},
                "ui_tree_cleaner": {"cleaner_id": "androidworld"},
            }
        ],
    }
    benchmark.write_text(yaml.safe_dump({"benchmark": [block]}))
    return benchmark


def _shipped_androidworld(root: Path) -> Path:
    """The shipped ``configs/benchmarks/androidworld.yaml``, its task loader over a small data directory."""
    shipped = yaml.safe_load(Path("configs/benchmarks/androidworld.yaml").read_text())
    shipped["benchmark"][0]["task_loader"]["data_dir"] = str(_androidworld_data(root))
    benchmark = root / "androidworld.yaml"
    benchmark.write_text(yaml.safe_dump(shipped))
    return benchmark


def _android(raw):
    return raw["environment"][0]["capabilities"][0]


def _open_app(raw):
    return next(a for a in _android(raw)["actions"] if a["action_id"] == "android.open_app")


def test_what_the_benchmark_supplies_is_left_out_and_named_where_it_would_sit(composer, tmp_path):
    benchmark = str(_androidworld(tmp_path))
    text = render_config_template(
        TemplateChoices(agent="m3a", model="m", benchmark=benchmark), composer=composer
    )
    raw = yaml.safe_load(text)
    assert "benchmark" not in raw
    assert not {"provider_id", "avd"} & set(_android(raw)["backend"]), "the placeholder gave way"
    assert "apps" not in _open_app(raw)
    # A block every setting of which the benchmark supplies is left out whole.
    assert "ui_tree_cleaner" not in _android(raw)
    assert "# ui_tree_cleaner.cleaner_id: androidworld, from the benchmark androidworld" in text
    assert "# backend.avd: AW, from the benchmark androidworld; left out here." in text
    assert "# Source: the benchmark androidworld." in text, "the backend block it fills"
    assert "# backend.provider_id: local_emulator, from the benchmark androidworld" in text
    assert (
        "# actions[android.open_app].apps: 2 entries, from the benchmark androidworld, "
        "computed in code; left out here."
    ) in text
    assert f"--benchmark {benchmark}" in text
    run = composer.compose(run_root(raw, read_benchmark(benchmark)))
    capability = run.single("capability")
    assert capability.backend.avd == "AW"
    open_app = next(a for a in capability.actions if a.action_id == "android.open_app")
    assert open_app.apps == TABLE


def test_m3a_on_androidworld_writes_its_official_values_and_none_the_benchmark_supplies(
    composer, tmp_path
):
    """AndroidWorld's runner settings come from the benchmark; M3A's own from its start."""
    benchmark = str(_shipped_androidworld(tmp_path))
    text = render_config_template(
        TemplateChoices(agent="m3a", model="m", benchmark=benchmark), composer=composer
    )
    raw = yaml.safe_load(text)
    block = read_benchmark(benchmark)
    supplied = composer.merge_capability_parts(run_root(raw, block)).supplied
    # The benchmark's part: AndroidWorld's AVD, its runner's waits, typing and
    # scroll (configs/benchmarks/androidworld.yaml), and the open_app table.
    scroll_entry = ("actions", ("action_id", "android.scroll"))
    assert {setting.path for setting in supplied} >= {
        ("settle_max_wait_s",),
        ("settle_quiet_ms",),
        ("typing_backend", "typing_id"),
        ("typing_backend", "fold_accents"),
        (*scroll_entry, "swipe_to"),
        (*scroll_entry, "duration_ms"),
        ("backend", "avd"),
    }
    android = _android(raw)
    assert not {"settle_max_wait_s", "settle_quiet_ms", "typing_backend"} & set(android)
    scroll = next(a for a in android["actions"] if a["action_id"] == "android.scroll")
    assert not {"swipe_to", "duration_ms"} & set(scroll)
    assert "# settle_max_wait_s: 6.0, from the benchmark androidworld; left out here." in text
    assert (
        "# typing_backend.fold_accents: true, from the benchmark androidworld; left out here."
    ) in text
    # M3A's official llm and Loop values (AndroidWorld's Gpt4Wrapper: temperature 0.0).
    agent = raw["agent"][0]
    assert (agent["llm"]["temperature"], agent["llm"]["frequency_penalty"]) == (0.0, None)
    loop = M3AAgent.__template_block__["agent_loop"]
    assert isinstance(loop, dict)
    assert {key: agent["agent_loop"][key] for key in loop} == loop
    # Composed with the benchmark, the run gets AndroidWorld's settings.
    capability = composer.compose(run_root(raw, block)).single("capability")
    assert (capability.settle_max_wait_s, capability.settle_quiet_ms) == (6.0, 1000)
    assert capability.typing_backend.fold_accents is True
    scroll = next(a for a in capability.actions if a.action_id == "android.scroll")
    assert (scroll.swipe_to, scroll.duration_ms) == ("target_edge", 300)


def test_a_run_that_writes_a_benchmark_setting_differently_is_refused(composer, tmp_path):
    benchmark = str(_shipped_androidworld(tmp_path))
    raw = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="m3a", model="m", benchmark=benchmark), composer=composer
        )
    )
    _android(raw)["settle_max_wait_s"] = 2.0
    with pytest.raises(ComposeError, match="settle_max_wait_s"):
        composer.compose(run_root(raw, read_benchmark(benchmark)))


def test_from_with_a_benchmark_keeps_the_run_and_the_values_its_author_wrote(composer, tmp_path):
    benchmark = str(_androidworld(tmp_path))
    source = yaml.safe_load(
        render_config_template(
            TemplateChoices(agent="m3a", model="m", benchmark=benchmark), composer=composer
        )
    )
    del _android(source)["actions"]
    _android(source)["backend"] = {"provider_id": "local_emulator"}
    text = render_config_template(TemplateChoices(benchmark=benchmark), source, composer=composer)
    expanded = yaml.safe_load(text)
    assert _android(expanded)["backend"]["provider_id"] == "local_emulator", "written: kept"
    assert "avd" not in _android(expanded)["backend"]
    assert "apps" not in _open_app(expanded)
    block = read_benchmark(benchmark)

    def resolved(raw):
        config = composer.compose(run_root(raw, block)).top_level_cfg
        assert config is not None
        return config.model_dump(mode="json")

    assert resolved(source) == resolved(expanded)


def test_a_skeleton_holding_a_benchmark_is_refused_with_one_selected(composer, tmp_path):
    benchmark = _androidworld(tmp_path)
    source = yaml.safe_load(benchmark.read_text())
    with pytest.raises(BenchmarkSelectionError, match="never holds a benchmark"):
        render_config_template(TemplateChoices(benchmark=str(benchmark)), source, composer=composer)
