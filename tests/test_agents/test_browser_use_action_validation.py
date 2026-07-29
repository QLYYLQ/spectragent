"""BrowserUseAgent — per-run output validation + action_list rendering.

Covers two pieces added in M14.5:

1. ``_build_per_run_output_model`` builds a Pydantic subclass of
   ``BrowserUseOutput`` whose ``action`` field rejects verbs outside
   the negotiated set, including hallucinated names and multi-key
   single-action items.

2. ``BrowserUseAgent._render_action_list`` formats the descriptors for
   prompt injection — empty list yields empty string, populated list
   yields stable canonical-name bullet output, params hint is preserved
   verbatim from each ``ActionDescriptor``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spectragent.agents._output_models import BrowserUseOutput
from spectragent.agents.browser_use import (
    BrowserUseAgent,
    _build_per_run_output_model,
)
from spectragent.schemas.action import ActionDescriptor


def _descriptors(*pairs: tuple[str, str, dict[str, str]]) -> list[ActionDescriptor]:
    return [
        ActionDescriptor(name=name, description=desc, params=params)
        for name, desc, params in pairs
    ]


# ── Per-run output model — verb validation ────────────────────────────


class TestPerRunOutputValidation:
    def _model_with(self, *names: str):
        descs = _descriptors(*[(n, "x", {}) for n in names])
        return _build_per_run_output_model(descs)

    def test_accepts_canonical_name(self) -> None:
        Model = self._model_with("browser.click", "browser.navigate")
        out = Model.model_validate(
            {
                "memory": "x",
                "action": [{"browser.click": {"index": 1}}],
            }
        )
        # ``action`` stays ``list[dict]`` — wire-identical to the base
        # model, so ``_items_to_actions`` needs no unwrapping.
        assert out.action[0] == {"browser.click": {"index": 1}}

    def test_accepts_short_alias(self) -> None:
        # ``"click"`` short alias is allowed when ``browser.click`` is in the
        # negotiated set — _SHORT_TO_CANONICAL bridges them.
        Model = self._model_with("browser.click", "browser.navigate")
        Model.model_validate(
            {"memory": "", "action": [{"click": {"index": 1}}]}
        )

    def test_accepts_legacy_alias(self) -> None:
        # ``"click_element"`` is the upstream browser-use vocabulary; allowed
        # when ``browser.click`` is in the negotiated set.
        Model = self._model_with("browser.click")
        Model.model_validate(
            {"memory": "", "action": [{"click_element": {"index": 1}}]}
        )

    def test_rejects_unknown_verb(self) -> None:
        Model = self._model_with("browser.click", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            Model.model_validate(
                {"memory": "", "action": [{"refresh_browser": {}}]}
            )
        msg = str(exc.value)
        assert "unknown action 'refresh_browser'" in msg
        # Error message lists the allowed set so the LLM can self-correct.
        assert "browser.click" in msg

    def test_rejects_short_alias_when_canonical_not_in_set(self) -> None:
        # ``browser.click`` not negotiated → short alias ``click`` rejected.
        Model = self._model_with("browser.navigate", "browser.done")
        with pytest.raises(ValidationError) as exc:
            Model.model_validate(
                {"memory": "", "action": [{"click": {"index": 1}}]}
            )
        assert "unknown action 'click'" in str(exc.value)

    def test_rejects_multi_key_action_item(self) -> None:
        Model = self._model_with("browser.click", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            Model.model_validate(
                {
                    "memory": "",
                    "action": [
                        {"browser.click": {}, "browser.navigate": {}},
                    ],
                }
            )
        assert "exactly one key" in str(exc.value)

    def test_rejects_empty_action_item(self) -> None:
        Model = self._model_with("browser.click")
        with pytest.raises(ValidationError):
            Model.model_validate(
                {"memory": "", "action": [{}]}
            )

    def test_empty_descriptors_rejects_everything(self) -> None:
        # Defensive — if prepare() ever runs with no descriptors, NO verb
        # is allowed. (Negotiation should have caught this earlier.)
        Model = _build_per_run_output_model([])
        with pytest.raises(ValidationError) as exc:
            Model.model_validate(
                {"memory": "", "action": [{"click": {}}]}
            )
        assert "unknown action 'click'" in str(exc.value)

    def test_per_run_subclass_inherits_other_fields(self) -> None:
        # ``thinking`` / ``memory`` / ``next_goal`` etc. still on the class.
        Model = self._model_with("browser.done")
        out = Model.model_validate(
            {
                "thinking": "deliberating",
                "memory": "remembering",
                "next_goal": "go forth",
                "action": [{"browser.done": {"extracted_content": "ok"}}],
            }
        )
        assert out.thinking == "deliberating"
        assert out.memory == "remembering"
        assert out.next_goal == "go forth"

    def test_class_name_set_for_debugging(self) -> None:
        Model = self._model_with("browser.done")
        assert Model.__name__ == "BrowserUseOutputForRun"

    def test_subclass_relationship_preserved(self) -> None:
        Model = self._model_with("browser.done")
        assert issubclass(Model, BrowserUseOutput)


# ── _render_action_list ───────────────────────────────────────────────


class TestRenderActionList:
    def test_empty_descriptors_yields_empty_string(self) -> None:
        assert BrowserUseAgent._render_action_list([]) == ""

    def test_single_descriptor_with_params(self) -> None:
        descs = _descriptors(
            ("browser.navigate", "Navigate to a URL.", {"url": "str"}),
        )
        rendered = BrowserUseAgent._render_action_list(descs)
        assert rendered == "- `browser.navigate(url: str)` — Navigate to a URL."

    def test_multiple_descriptors_one_per_line(self) -> None:
        descs = _descriptors(
            ("browser.click", "Click an element.", {"index": "int"}),
            ("browser.wait", "Pause N seconds.", {"seconds": "float"}),
        )
        rendered = BrowserUseAgent._render_action_list(descs)
        lines = rendered.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("- `browser.click(")
        assert lines[1].startswith("- `browser.wait(")

    def test_no_params_renders_placeholder(self) -> None:
        descs = _descriptors(
            ("browser.go_back", "Navigate back.", {}),
        )
        rendered = BrowserUseAgent._render_action_list(descs)
        assert "no params" in rendered
        assert rendered == "- `browser.go_back(no params)` — Navigate back."

    def test_uses_canonical_names_not_short(self) -> None:
        descs = _descriptors(("browser.click", "Click.", {"index": "int"}))
        rendered = BrowserUseAgent._render_action_list(descs)
        assert "browser.click" in rendered
        assert "`click(" not in rendered  # short form should NOT appear

    def test_preserves_param_descriptions_verbatim(self) -> None:
        descs = _descriptors(
            (
                "browser.scroll",
                "Scroll the page.",
                {"direction": "up|down|left|right", "amount": "int (optional)"},
            ),
        )
        rendered = BrowserUseAgent._render_action_list(descs)
        assert "direction: up|down|left|right" in rendered
        assert "amount: int (optional)" in rendered


# ── prepare() integration ─────────────────────────────────────────────


class TestPrepareWiresPerRunModel:
    def test_get_react_output_model_uses_per_run_subclass(self) -> None:
        agent = BrowserUseAgent()
        descs = _descriptors(
            ("browser.click", "Click.", {"index": "int"}),
            ("browser.done", "Finish.", {"extracted_content": "str"}),
        )
        agent.prepare(task="t", action_descriptors=descs)
        model = agent.get_react_output_model()
        assert model is not None
        assert model.__name__ == "BrowserUseOutputForRun"

        # Negotiated verb passes; foreign verb fails.
        model.model_validate(
            {"memory": "", "action": [{"click": {"index": 1}}]}
        )
        with pytest.raises(ValidationError):
            model.model_validate(
                {"memory": "", "action": [{"refresh_browser": {}}]}
            )

    def test_prompt_includes_action_list(self) -> None:
        agent = BrowserUseAgent(flash_mode=True)
        descs = _descriptors(
            ("browser.click", "Click an element.", {"index": "int"}),
            ("browser.wait", "Pause.", {"seconds": "float"}),
        )
        seed = agent.prepare(task="t", action_descriptors=descs)
        text = str(seed[0].content)
        assert "<actions_available>" in text
        assert "browser.click" in text
        assert "browser.wait" in text
        # Action list is the canonical, not short, names.
        assert "- `browser.click(index: int)`" in text

        # The snapshot is a TemplatePrompt, so the advertised action set
        # is a field on the trace rather than something to regex out of
        # 200 lines of prose.
        snapshot = agent.system_prompt
        assert snapshot is not None
        assert snapshot.kind == "template"  # type: ignore[union-attr]
        assert "browser.wait" in snapshot.params["action_list"]  # type: ignore[union-attr]

    def test_get_react_output_model_falls_back_before_prepare(self) -> None:
        # Defensive: if anyone calls before prepare(), they get the static
        # loose model rather than crashing.
        agent = BrowserUseAgent()
        assert agent.get_react_output_model() is BrowserUseOutput
