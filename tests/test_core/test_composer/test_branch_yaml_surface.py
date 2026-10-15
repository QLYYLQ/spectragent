"""The branch loop's YAML surface: ``agent_loop: {loop_id: branch, restorer: …}`` (§4.1, §6).

``BranchLoop.restorer`` is a single-instance receptor wired from the restorer
registry, held annotation-only on a plain mixin (the ``AgentReceptors``
shape) with a hand-built default. Pinned here, against the real Composer and
the real registrars:

- the generated union nests agent → ``agent_loop`` → ``restorer``, every knob
  typed, and the restorer is required in a ``branch`` block;
- the configured restorer and its knobs reach the instance, replacing the
  default a hand-built loop gets;
- ``history_source`` reaches the loop independently of environment verification;
- prompt contributions configure an existing Hook and an independent wired helper;
- a mistyped ``restorer_id`` / ``loop_id``, an unknown knob and a value outside
  a closed set are refused by validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.prompt_contribution import PromptContributionHook
from spectragent.core.loops import BranchLoop
from spectragent.core.loops.restore import PrefixReplayRestorer
from spectragent.core.registries import collect_registrars
from spectragent.core.trace.redaction import redact_config
from spectragent.schemas.prompt import PromptContribution, PromptRequest

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "ui_tars_local.yaml"
SHIPPED_BRANCH = CONFIG.parent / "flywheel" / "branch_ui_tars_archive.yaml"


def configured_prompt_hint(*, request: PromptRequest) -> PromptContribution:
    """Stable reference used by the configuration-only prompt hook tests."""
    return PromptContribution(text=str(request.parameters["instruction"]), placement="system")


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars=collect_registrars())


def _branch(**loop: Any) -> dict[str, Any]:
    raw = yaml.safe_load(CONFIG.read_text("utf-8"))
    raw["agent"][0]["agent_loop"] = {"loop_id": "branch", **loop}
    return raw


def _loop(composer: ConfigComposer, raw: dict[str, Any]) -> Any:
    run = composer.compose(raw)
    return run.single("agent").agent_loop


class TestNestedUnions:
    def test_the_configured_restorer_reaches_the_loop(self, composer: ConfigComposer) -> None:
        loop = _loop(
            composer,
            _branch(
                continue_steps=2,
                max_steps=7,
                restorer={
                    "restorer_id": "prefix_replay",
                    "verify": "warn",
                    "screenshot_max_distance": 12,
                    "pace": "recorded_llm_latency",
                },
            ),
        )
        assert isinstance(loop, BranchLoop)
        assert (loop.continue_steps, loop.max_steps, loop.terminal_observation) == (2, 7, True)
        assert isinstance(loop.restorer, PrefixReplayRestorer)
        assert (
            loop.restorer.verify,
            loop.restorer.screenshot_max_distance,
            loop.restorer.pace,
        ) == ("warn", 12, "recorded_llm_latency")

    def test_a_hand_built_loop_gets_the_default_restorer(self) -> None:
        loop = BranchLoop()
        assert isinstance(loop.restorer, PrefixReplayRestorer)
        assert loop.restorer.verify == "strict"

    def test_the_restorer_is_required_in_a_branch_block(self, composer: ConfigComposer) -> None:
        with pytest.raises(ValidationError, match="restorer"):
            composer.top_level_type.model_validate(_branch())

    def test_the_restorer_block_defaults_its_knobs(self, composer: ConfigComposer) -> None:
        loop = _loop(composer, _branch(restorer={"restorer_id": "prefix_replay"}))
        assert (loop.restorer.verify, loop.restorer.screenshot_max_distance) == ("strict", 5)
        assert loop.continue_steps == 0
        assert loop.history_source == "source"

    @pytest.mark.parametrize("history_source", ["source", "restored"])
    @pytest.mark.parametrize("verify", ["strict", "warn"])
    def test_history_source_reaches_the_loop_and_is_recorded_independently(
        self, composer: ConfigComposer, history_source: str, verify: str
    ) -> None:
        """History selection and environment verification remain separate experiment inputs."""
        run = composer.compose(
            _branch(
                restorer={"restorer_id": "prefix_replay", "verify": verify},
                history_source=history_source,
            )
        )
        loop = run.single("agent").agent_loop
        assert loop.history_source == history_source
        assert loop.restorer.verify == verify
        recorded = redact_config(run.top_level_cfg)["agent"][0]["agent_loop"]
        assert recorded["history_source"] == history_source
        assert recorded["restorer"]["verify"] == verify

    def test_the_shipped_branch_config_declares_a_supported_history_source(
        self, composer: ConfigComposer
    ) -> None:
        raw = yaml.safe_load(SHIPPED_BRANCH.read_text("utf-8"))
        loop = raw["agent"][0]["agent_loop"]
        assert loop["history_source"] == "source"
        assert "branch_instruction" not in loop
        validated = composer.top_level_type.model_validate(raw).model_dump()
        assert validated["agent"][0]["agent_loop"]["history_source"] == "source"

    def test_prompt_contribution_configuration_reaches_the_hook_and_helper(
        self, composer: ConfigComposer
    ) -> None:
        raw = _branch(restorer={"restorer_id": "prefix_replay"}, history_source="restored")
        reference = f"{__name__}:configured_prompt_hint"
        raw["hook"].append(
            {
                "hook_id": "prompt_contribution",
                "function": reference,
                "parameters": {"instruction": "Use the actual restored result."},
                "scope": "each_policy_step",
                "helper_llms": [
                    {
                        "model_type": "openai",
                        "model": "prompt-helper",
                        "provider": {"name": "direct"},
                        "temperature": 0.3,
                    }
                ],
            }
        )
        run = composer.compose(raw)
        hook = next(item for item in run.hooks if isinstance(item, PromptContributionHook))
        assert hook.function == reference and hook.scope == "each_policy_step"
        assert hook.parameters == {"instruction": "Use the actual restored result."}
        assert len(hook.code_digest) == 64
        assert len(hook.helper_llms) == 1
        helper = hook.helper_llms[0]
        assert helper.model == "prompt-helper"
        assert helper is not run.single("agent").llm
        recorded = next(
            item
            for item in redact_config(run.top_level_cfg)["hook"]
            if item["hook_id"] == "prompt_contribution"
        )
        assert recorded["function"] == reference and recorded["scope"] == "each_policy_step"
        assert recorded["helper_llms"][0]["temperature"] == 0.3

    def test_react_stays_the_default_shape(self, composer: ConfigComposer) -> None:
        raw = yaml.safe_load(CONFIG.read_text("utf-8"))
        loop = _loop(composer, raw)
        assert loop.loop_id == "react"
        assert loop.terminal_observation is False


class TestTyposRejected:
    @pytest.mark.parametrize(
        "loop",
        [
            pytest.param({"restorer": {"restorer_id": "prefix_replays"}}, id="restorer_id"),
            pytest.param(
                {"restorer": {"restorer_id": "prefix_replay", "verfy": "warn"}},
                id="restorer-knob",
            ),
            pytest.param(
                {"restorer": {"restorer_id": "prefix_replay", "verify": "loose"}},
                id="verify-value",
            ),
            pytest.param(
                {"restorer": {"restorer_id": "prefix_replay", "screenshot_max_distance": 65}},
                id="distance-range",
            ),
            pytest.param(
                {"restorer": {"restorer_id": "prefix_replay"}, "continue_steps": -1},
                id="continue_steps-range",
            ),
            pytest.param(
                {"restorer": {"restorer_id": "prefix_replay"}, "continue_step": 1},
                id="loop-knob",
            ),
            pytest.param(
                {
                    "restorer": {"restorer_id": "prefix_replay"},
                    "history_source": "mixed",
                },
                id="history_source-value",
            ),
            pytest.param(
                {
                    "restorer": {"restorer_id": "prefix_replay"},
                    "branch_instruction": "prefer_different_action",
                },
                id="removed-branch_instruction",
            ),
        ],
    )
    def test_refused_by_validation(self, composer: ConfigComposer, loop: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            composer.top_level_type.model_validate(_branch(**loop))

    def test_an_unsupported_prompt_contribution_scope_is_refused(
        self, composer: ConfigComposer
    ) -> None:
        raw = _branch(restorer={"restorer_id": "prefix_replay"})
        raw["hook"].append(
            {
                "hook_id": "prompt_contribution",
                "function": f"{__name__}:configured_prompt_hint",
                "scope": "every_builder_call",
            }
        )
        with pytest.raises(ValidationError, match="scope"):
            composer.top_level_type.model_validate(raw)

    def test_a_mistyped_loop_id_is_refused(self, composer: ConfigComposer) -> None:
        raw = _branch(restorer={"restorer_id": "prefix_replay"})
        raw["agent"][0]["agent_loop"]["loop_id"] = "brnach"
        with pytest.raises(ValidationError):
            composer.top_level_type.model_validate(raw)


# ── the task loader that feeds a branch sweep: trace_branches → step_selector ──


def _sweep(**loader: Any) -> dict[str, Any]:
    raw = _branch(restorer={"restorer_id": "prefix_replay"})
    raw["task_loader"] = [{"benchmark_name": "trace_branches", **loader}]
    return raw


class TestTheTaskLoaderSurface:
    def test_the_configured_selector_reaches_the_loader(self, composer: ConfigComposer) -> None:
        from spectragent.core.data_loader.task import TraceBranchesTaskLoader
        from spectragent.core.data_loader.task.require_eval import RequireEval
        from spectragent.core.data_loader.task.step_selector import EveryKSteps

        run = composer.compose(
            _sweep(
                data_dir="/recorded",
                branches_per_step=3,
                agent_outcome="done",
                require_eval={"evaluator_id": "online_mind2web", "slug": "online_mind2web-ab"},
                require_archive=[],
                on_unbranchable="skip",
                step_selector={"selector_id": "every_k", "k": 2, "offset": 1},
            )
        )
        loader = run.single("task_loader")
        assert isinstance(loader, TraceBranchesTaskLoader)
        assert isinstance(loader.step_selector, EveryKSteps)
        assert (loader.step_selector.k, loader.step_selector.offset) == (2, 1)
        assert (loader.data_dir, loader.branches_per_step, loader.agent_outcome) == (
            "/recorded",
            3,
            "done",
        )
        assert loader.require_eval == RequireEval(
            evaluator_id="online_mind2web", label=1, slug="online_mind2web-ab"
        )
        assert (loader.require_archive, loader.on_unbranchable) == ((), "skip")

    def test_the_selector_is_required_in_the_block(self, composer: ConfigComposer) -> None:
        with pytest.raises(ValidationError, match="step_selector"):
            composer.top_level_type.model_validate(_sweep())

    @pytest.mark.parametrize(
        "loader",
        [
            pytest.param({"step_selector": {"selector_id": "everyk"}}, id="selector_id"),
            pytest.param({"step_selector": {"selector_id": "every_k", "k": 0}}, id="k-range"),
            pytest.param(
                {"step_selector": {"selector_id": "explicit", "step": [1]}}, id="selector-knob"
            ),
            pytest.param(
                {"step_selector": {"selector_id": "all"}, "require_eval": {"label": 1}},
                id="require_eval-shape",
            ),
            pytest.param(
                {"step_selector": {"selector_id": "all"}, "agent_outcome": "completed"},
                id="agent_outcome-value",
            ),
            pytest.param(
                {"step_selector": {"selector_id": "all"}, "require_status": "any"},
                id="the-removed-require_status",
            ),
            pytest.param(
                {"step_selector": {"selector_id": "all"}, "branch_per_step": 2}, id="loader-knob"
            ),
        ],
    )
    def test_refused_by_validation(self, composer: ConfigComposer, loader: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            composer.top_level_type.model_validate(_sweep(**loader))
