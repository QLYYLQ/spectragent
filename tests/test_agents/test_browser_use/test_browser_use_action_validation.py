"""BrowserUseAgent — the closed per-run action schema.

What used to live here: an ``AfterValidator`` over ``list[dict[str, Any]]``
that knew the negotiated verb *names* and accepted three spellings of each
(canonical, local id, upstream alias). It contributed nothing to the JSON
schema, so a provider saw an open map and Gemini answered ``{}``.

What lives here now: the union built by ``core/actions/action_union.py``. One
single-key model per negotiated verb, dispatched by a ``BeforeValidator``,
so the schema *is* the vocabulary — one spelling, the fully-qualified key,
the same string the prompt advertises and ``env.act`` routes on.

The other half of the file is the error surface: one typed error per
mistake, naming the verb, never one complaint per union branch. The model is
not shown these messages; ``core/action_guard.py::reply_refusal`` words the
refusal from the errors' types and context (``tests/test_core/test_action_guard.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
from spectragent.agents.browser_use import BrowserUseAgent, BrowserUseDomAgent
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.action_union import (
    ACTION_PARAMS,
    ACTION_SHAPE,
    ACTION_UNKNOWN,
    UnsupportedParamsSchema,
    action_union,
)
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import ActionDescriptor
from tests._helpers.action_spaces import space_of
from tests._helpers.fragment_formats import dom_formats


def _descriptors(*keys: str) -> list[ActionDescriptor]:
    """Real descriptors, because the union is built from ``params_schema``."""
    return [ActionRegistrar.get(key)().descriptor() for key in keys]


def _errors(exc: ValidationError) -> list[str]:
    return [f"{e['loc']}: {e['msg']}" for e in exc.errors()]


# ── The union ─────────────────────────────────────────────────────────


class TestClosedUnion:
    def _model(self, *keys: str) -> Any:
        agent = BrowserUseDomAgent(flash_mode=True)
        agent.prepare(
            task="t",
            action_space=space_of(_descriptors(*keys)),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        return agent.get_react_output_model()

    def test_accepts_the_fully_qualified_key(self) -> None:
        model = self._model("browser.click", "browser.navigate")
        out = model.model_validate({"memory": "x", "action": [{"browser.click": {"index": 1}}]})
        assert out.action[0].model_dump() == {"browser.click": {"index": 1}}

    def test_refuses_the_local_id(self) -> None:
        """No short form. ``build_guarded_act`` used to bind one late.

        The prompt advertises ``browser.click`` and the schema permits
        ``browser.click``; accepting ``click`` as well meant the run had
        two names for one action and the trace could record either.
        """
        model = self._model("browser.click", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            model.model_validate({"memory": "", "action": [{"click": {"index": 1}}]})
        assert "unknown action 'click'" in str(exc.value)

    def test_refuses_the_upstream_alias(self) -> None:
        """``click_element`` / ``input_text`` / ``go_to_url`` are gone.

        No template ever taught them, so the table was pure accommodation.
        """
        model = self._model("browser.click")
        with pytest.raises(ValidationError) as exc:
            model.model_validate({"memory": "", "action": [{"click_element": {"index": 1}}]})
        assert "unknown action 'click_element'" in str(exc.value)

    def test_unknown_verb_is_one_error_that_lists_the_allowed_set(self) -> None:
        model = self._model("browser.click", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            model.model_validate({"memory": "", "action": [{"refresh": {}}]})
        assert len(exc.value.errors()) == 1, _errors(exc.value)
        (error,) = exc.value.errors()
        assert error["type"] == ACTION_UNKNOWN
        assert error.get("ctx", {})["verb"] == "refresh"
        message = str(exc.value)
        assert "unknown action 'refresh'" in message
        assert "browser.click" in message and "browser.navigate" in message

    def test_empty_action_item_is_one_error(self) -> None:
        """The failure this whole design exists to make impossible.

        A property-less ``{"type": "object"}`` schema let a constrained
        decoder emit ``[{}]``; a plain union then reported it once per
        branch, so eleven actions meant eleven lines of feedback.
        """
        model = self._model("browser.click", "browser.navigate", "browser.done")
        with pytest.raises(ValidationError) as exc:
            model.model_validate({"memory": "", "action": [{}]})
        assert len(exc.value.errors()) == 1, _errors(exc.value)
        (error,) = exc.value.errors()
        assert error["type"] == ACTION_SHAPE
        assert error.get("ctx", {})["written"] == "an empty object"
        assert "exactly one key" in error["msg"]

    def test_multi_key_item_is_one_error(self) -> None:
        model = self._model("browser.click", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            model.model_validate(
                {
                    "memory": "",
                    "action": [{"browser.click": {"index": 1}, "browser.navigate": {}}],
                }
            )
        assert len(exc.value.errors()) == 1, _errors(exc.value)
        (error,) = exc.value.errors()
        assert error["type"] == ACTION_SHAPE
        assert error.get("ctx", {})["written"] == (
            "an object naming 'browser.click', 'browser.navigate'"
        )

    def test_bad_param_type_is_one_error_located_on_the_verb(self) -> None:
        model = self._model("browser.click", "browser.scroll", "browser.navigate")
        with pytest.raises(ValidationError) as exc:
            model.model_validate(
                {"memory": "", "action": [{"browser.scroll": {"direction": "sideways"}}]}
            )
        assert len(exc.value.errors()) == 1, _errors(exc.value)
        (error,) = exc.value.errors()
        assert error["type"] == ACTION_PARAMS
        ctx = error.get("ctx", {})
        assert ctx["verb"] == "browser.scroll"
        assert [problem["param"] for problem in ctx["problems"]] == ["direction"]
        assert "browser.scroll: direction" in str(exc.value)

    def test_every_bad_param_of_one_action_is_in_its_one_error(self) -> None:
        """One error per action, listing each parameter at fault, as ``params_refusal`` does."""
        model = self._model("browser.click")
        with pytest.raises(ValidationError) as exc:
            model.model_validate(
                {"memory": "", "action": [{"browser.click": {"index": "x", "y": 2}}]}
            )
        (error,) = exc.value.errors()
        assert [problem["param"] for problem in error.get("ctx", {})["problems"]] == [
            "index",
            "y",
        ]

    def test_unexpected_param_is_refused(self) -> None:
        model = self._model("browser.click")
        with pytest.raises(ValidationError) as exc:
            model.model_validate(
                {"memory": "", "action": [{"browser.click": {"index": 1, "x": 2}}]}
            )
        assert len(exc.value.errors()) == 1, _errors(exc.value)
        assert "browser.click:" in str(exc.value)

    def test_an_empty_action_list_is_refused(self) -> None:
        model = self._model("browser.done")
        with pytest.raises(ValidationError):
            model.model_validate({"memory": "", "action": []})

    def test_the_dotted_key_survives_a_json_round_trip(self) -> None:
        """The replay contract: a recorded reply re-reads as itself.

        Pydantic takes ``browser.click`` as a field name verbatim, so no
        alias is involved — which is what would otherwise have put ``v0``
        in the trace where the verb belongs.
        """
        model = self._model("browser.click", "browser.navigate")
        out = model.model_validate({"memory": "m", "action": [{"browser.click": {"index": 3}}]})
        text = out.model_dump_json()
        assert json.loads(text)["action"] == [{"browser.click": {"index": 3}}]
        assert model.model_validate_json(text).action[0].model_dump() == {
            "browser.click": {"index": 3}
        }

    def test_a_run_with_no_actions_is_refused_at_prepare(self) -> None:
        agent = BrowserUseDomAgent()
        with pytest.raises(ValueError, match="at least one negotiated action"):
            agent.prepare(
                task="t",
                action_space=space_of([]),
                execution_limits=ExecutionLimits(max_actions_per_step=1),
                fragment_formats=dom_formats(),
            )

    def test_a_params_schema_outside_the_subset_fails_at_prepare(self) -> None:
        """Loud at ``prepare()``, not at the first LLM call."""
        nested = ActionDescriptor(
            name="browser.weird",
            description="x",
            params={"payload": "object"},
            params_schema={
                "type": "object",
                "properties": {"payload": {"type": "object"}},
                "required": ["payload"],
            },
        )
        with pytest.raises(UnsupportedParamsSchema, match="payload"):
            action_union([nested])


# ── Reasoning fields follow the template ──────────────────────────────


class TestReasoningFieldsFollowTheVariant:
    def test_flash_asks_only_for_memory_and_action(self) -> None:
        agent = BrowserUseDomAgent(flash_mode=True)
        agent.prepare(
            task="t",
            action_space=space_of(_descriptors("browser.done")),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert list(agent.get_react_output_model().model_fields) == ["memory", "action"]

    def test_thinking_variant_asks_for_the_full_set(self) -> None:
        agent = BrowserUseDomAgent(flash_mode=False, use_thinking=True)
        agent.prepare(
            task="t",
            action_space=space_of(_descriptors("browser.done")),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert list(agent.get_react_output_model().model_fields) == [
            "thinking",
            "evaluation_previous_goal",
            "memory",
            "next_goal",
            "current_plan_item",
            "plan_update",
            "action",
        ]

    def test_no_thinking_variant_drops_thinking(self) -> None:
        agent = BrowserUseDomAgent(flash_mode=False, use_thinking=False)
        agent.prepare(
            task="t",
            action_space=space_of(_descriptors("browser.done")),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert "thinking" not in agent.get_react_output_model().model_fields


# ── The action list in the prompt ─────────────────────────────────────


class TestRenderActionList:
    def _render(self, agent: BrowserUseAgent, *keys: str) -> str:
        seed = agent.prepare(
            task="t",
            action_space=space_of(_descriptors(*keys)),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        return str(seed[0].content)

    def test_advertises_the_fully_qualified_name_and_the_descriptor_params(self) -> None:
        text = self._render(BrowserUseDomAgent(flash_mode=True), "browser.navigate")
        assert "- `browser.navigate(url: str)` — Navigate to a URL." in text
        assert "`navigate(" not in text

    def test_a_parameterless_action_says_so(self) -> None:
        text = self._render(BrowserUseDomAgent(flash_mode=True), "browser.go_back")
        assert "- `browser.go_back(no params)` — Navigate back in browser history." in text

    def test_param_text_is_the_descriptor_s_own(self) -> None:
        text = self._render(BrowserUseDomAgent(flash_mode=True), "browser.scroll")
        assert "direction: up|down|left|right" in text
        assert "amount: int (optional)" in text

    def test_the_advertised_set_is_a_field_on_the_snapshot(self) -> None:
        """Not something to regex out of 200 lines of prose."""
        agent = BrowserUseDomAgent(flash_mode=True)
        self._render(agent, "browser.wait", "browser.done")
        snapshot = agent.system_prompt
        assert snapshot is not None
        assert snapshot.kind == "template"  # type: ignore[union-attr]
        assert "browser.wait" in snapshot.params["action_list"]  # type: ignore[union-attr]
        assert snapshot.params["surface"]  # type: ignore[union-attr]


# ── Before prepare() ──────────────────────────────────────────────────


def test_get_react_output_model_refuses_to_answer_before_prepare() -> None:
    """Louder than a loose fallback, which is how the open schema shipped."""
    agent = BrowserUseDomAgent()
    with pytest.raises(RuntimeError, match="before"):
        agent.get_react_output_model()
