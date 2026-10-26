"""Tests for build_guarded_act — prompt-capability consistency guarantee — and the refusals beside it."""

import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from spectragent.agents.skyvern.reply import reply_model
from spectragent.core.action_guard import (
    build_guarded_act,
    executed_descriptor,
    executed_name,
    name_refusal,
    params_refusal,
    reply_refusal,
    resolve_action_name,
)
from spectragent.core.actions.action_union import (
    ACTION_PARAMS,
    ACTION_SHAPE,
    ACTION_UNKNOWN,
    action_union,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace
from tests._helpers.action_spaces import browser_descriptors, space_of

_DESCRIPTORS = [
    ActionDescriptor(name="click", description="Click"),
    ActionDescriptor(name="type", description="Type"),
    ActionDescriptor(name="done", description="Done"),
]


async def _mock_act(action: Action, **kwargs) -> ActionResult:
    return ActionResult(success=True, extracted_content=action.name)


class TestGuardedAct:
    def test_no_filter_returns_all(self) -> None:
        filtered, _ = build_guarded_act(_mock_act, _DESCRIPTORS)
        assert len(filtered.descriptors) == 3

    async def test_valid_action_passes(self) -> None:
        _, guard = build_guarded_act(_mock_act, _DESCRIPTORS)
        result = await guard(Action(name="click"))
        assert result.success
        assert result.extracted_content == "click"

    async def test_invalid_action_rejected(self) -> None:
        _, guard = build_guarded_act(_mock_act, _DESCRIPTORS)
        result = await guard(Action(name="invalid"))
        assert not result.success
        assert result.error is not None and "invalid" in result.error

    def test_filter_narrows_set(self) -> None:
        filtered, _ = build_guarded_act(
            _mock_act,
            _DESCRIPTORS,
            action_filter=["click", "done"],
        )
        assert {d.name for d in filtered.descriptors} == {"click", "done"}

    async def test_filter_rejects_excluded(self) -> None:
        _, guard = build_guarded_act(
            _mock_act,
            _DESCRIPTORS,
            action_filter=["click"],
        )
        result = await guard(Action(name="type"))
        assert not result.success

    def test_empty_filter_returns_empty(self) -> None:
        filtered, _ = build_guarded_act(_mock_act, _DESCRIPTORS, action_filter=[])
        assert len(filtered.descriptors) == 0

    @pytest.mark.parametrize("selection", [None, [], ["click"]])
    async def test_recorded_space_matches_prompt_and_execution(
        self, selection: list[str] | None
    ) -> None:
        space, guard = build_guarded_act(_mock_act, _DESCRIPTORS, action_filter=selection)
        restored = ActionSpace.model_validate_json(space.model_dump_json())
        assert restored.action_filter == selection
        assert restored.allowed_actions == {d.name for d in restored.descriptors}
        for descriptor in _DESCRIPTORS:
            result = await guard(Action(name=descriptor.name))
            assert result.success == (descriptor.name in restored.allowed_actions)

    def test_original_filter_and_bindings_are_owned_snapshots(self) -> None:
        selection, bindings = ["click"], {"alias": "click"}
        space, _ = build_guarded_act(_mock_act, _DESCRIPTORS, selection, bindings=bindings)
        selection.clear()
        bindings.clear()
        assert space.action_filter == ["click"]
        assert space.action_bindings == {"alias": "click"}

    async def test_public_snapshot_mutation_cannot_change_guard_execution(self) -> None:
        space, guard = build_guarded_act(
            _mock_act, _DESCRIPTORS, ["click"], bindings={"alias": "click"}
        )
        space.action_bindings["alias"] = "done"
        space.descriptors.clear()
        assert (await guard(Action(name="alias"))).extracted_content == "click"
        assert not (await guard(Action(name="done"))).success

    def test_serialized_allowed_names_have_stable_order(self) -> None:
        space, _ = build_guarded_act(_mock_act, list(reversed(_DESCRIPTORS)))
        assert space.model_dump(mode="json")["allowed_actions"] == ["click", "done", "type"]

    def test_mismatched_recorded_prompt_and_guard_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must agree"):
            ActionSpace(descriptors=_DESCRIPTORS, allowed_actions=frozenset())


class TestResolveActionName:
    """The qualification rule ``guarded_act`` applies — its one copy."""

    _ACTIVE = frozenset({"browser.click", "browser.done"})
    _BINDINGS = {"click": "browser.click", "done": "browser.done", "scroll": "browser.scroll"}

    def test_an_active_name_is_itself(self) -> None:
        assert resolve_action_name("browser.click", self._ACTIVE, self._BINDINGS) == (
            "browser.click"
        )

    def test_a_short_name_resolves_through_its_binding(self) -> None:
        assert resolve_action_name("click", self._ACTIVE, self._BINDINGS) == "browser.click"

    def test_a_binding_whose_target_is_not_active_is_refused(self) -> None:
        """An ablation that dropped ``browser.scroll`` revokes ``scroll`` too."""
        assert resolve_action_name("scroll", self._ACTIVE, self._BINDINGS) is None

    def test_an_unknown_name_is_refused(self) -> None:
        assert resolve_action_name("hover", self._ACTIVE, self._BINDINGS) is None

    def test_without_bindings_only_full_names_pass(self) -> None:
        assert resolve_action_name("click", self._ACTIVE) is None
        assert resolve_action_name("browser.done", self._ACTIVE) == "browser.done"

    async def test_the_guard_applies_it(self) -> None:
        descriptors = [
            ActionDescriptor(name="browser.click", description="Click"),
            ActionDescriptor(name="browser.scroll", description="Scroll"),
        ]
        _, guard = build_guarded_act(
            _mock_act, descriptors, ["browser.click"], bindings=self._BINDINGS
        )
        resolved, refused = Action(name="click"), Action(name="scroll")
        assert (await guard(resolved)).success
        assert not (await guard(refused)).success
        # Resolved in place; a refused action keeps the name the agent wrote.
        assert (resolved.name, refused.name) == ("browser.click", "scroll")


class TestTheAgentsNamingHelper:
    """What an Agent asks instead of matching a name's suffix: the run's own space."""

    _DESCRIPTORS = [
        ActionDescriptor(name="browser.click", description="Click"),
        ActionDescriptor(name="browser.scroll", description="Scroll"),
    ]

    def _space(self) -> ActionSpace:
        space, _ = build_guarded_act(
            _mock_act,
            self._DESCRIPTORS,
            ["browser.click"],
            bindings={"click": "browser.click", "scroll": "browser.scroll"},
        )
        return space

    def test_a_name_means_what_the_guard_executes_it_as(self) -> None:
        space = self._space()
        assert executed_name(space, "click") == "browser.click"
        assert executed_name(space, "browser.click") == "browser.click"
        descriptor = executed_descriptor(space, "click")
        assert descriptor is not None and descriptor.name == "browser.click"

    def test_a_name_the_guard_refuses_means_no_action_of_the_run(self) -> None:
        space = self._space()
        # The filter dropped scroll, so its alias is revoked with it.
        assert executed_name(space, "scroll") is None
        assert executed_descriptor(space, "scroll") is None
        # Another capability's click is not this run's, whatever its suffix.
        assert executed_name(space, "desktop.click") is None


class TestReplyRefusal:
    """A structured reply that does not fit its output model, refused as a params misfit is.

    ``reply_refusal`` is a function of the reply's text, the output model and
    the run's action space, so a branch rereading a recorded reply words it
    as the live step did. Its action items read like ``check_action_params``
    (``params_refusal``, ``name_refusal``), so a structured agent is told what
    a text-protocol one is for the same mistake.
    """

    _SPACE = space_of(browser_descriptors("click", "type", "done"))

    def _flat(self) -> type[BaseModel]:
        """BrowserUse's shape: reasoning fields beside an action list."""
        union, _ = action_union(self._SPACE.descriptors)
        return create_model(
            "Flat",
            __config__=ConfigDict(extra="forbid"),
            next_goal=(str, ...),
            action=(list[union], ...),
        )

    def _refuse(self, model: type[BaseModel], reply: object) -> str:
        refusal = reply_refusal(self._SPACE, model, json.dumps(reply))
        assert refusal is not None
        # Worded for the model: no pydantic links, no echo of the input.
        assert "pydantic" not in refusal and "input_value" not in refusal
        return refusal

    def test_text_that_is_not_json_is_one_line_with_the_position(self) -> None:
        refusal = reply_refusal(self._SPACE, self._flat(), '{"next_goal": "x", "action": [')
        assert (
            refusal == "the reply is not valid JSON: EOF while parsing a list at line 1 column 30"
        )

    def test_a_reply_that_fits_is_not_refused(self) -> None:
        reply = {"next_goal": "x", "action": [{"browser.click": {"index": 1}}]}
        assert reply_refusal(self._SPACE, self._flat(), json.dumps(reply)) is None

    def test_a_missing_field_outside_the_actions_is_named_by_its_path(self) -> None:
        refusal = self._refuse(self._flat(), {"action": [{"browser.click": {"index": 1}}]})
        assert refusal == "the reply does not fit the output schema: next_goal: Field required"

    def test_a_misfit_action_reads_as_the_params_check_words_it(self) -> None:
        reply = {
            "next_goal": "x",
            "action": [{"browser.click": {"index": 1}}, {"browser.type": {"index": 2}}],
        }
        refusal = self._refuse(self._flat(), reply)
        # The same clause ReAct's params check gives the same action.
        same = params_refusal(self._SPACE, Action(name="browser.type", params={"index": 2}))
        assert refusal == f"the reply does not fit the output schema: action 2, {same}"
        assert "action 2, browser.type: text: Field required (browser.type takes index: " in refusal

    def test_an_unknown_verb_reads_as_the_name_check_words_it(self) -> None:
        refusal = self._refuse(self._flat(), {"next_goal": "x", "action": [{"browser.fly": {}}]})
        same = name_refusal(self._SPACE, "browser.fly")
        assert refusal == f"the reply does not fit the output schema: action 1, {same}"
        assert "(its actions: browser.click, browser.done, browser.type)" in refusal

    @pytest.mark.parametrize(
        ("item", "written"),
        [
            ("click 3", "a string"),
            ({}, "an empty object"),
            (
                {"browser.click": {"index": 1}, "browser.done": {}},
                "an object naming 'browser.click', 'browser.done'",
            ),
            ({"browser.click": 3}, "'browser.click' given a number for its parameters"),
        ],
    )
    def test_an_item_of_the_wrong_shape_says_what_it_is(self, item: object, written: str) -> None:
        refusal = self._refuse(self._flat(), {"next_goal": "x", "action": [item]})
        assert refusal == (
            f"the reply does not fit the output schema: action 1, {written}: each action is an "
            "object with exactly one key, the action's name, whose value is the object of its "
            "parameters (its actions: browser.click, browser.done, browser.type)"
        )

    def test_every_mistake_is_located_in_reply_order(self) -> None:
        reply = {
            "action": [
                {"browser.click": {"index": "the cart", "x": 1}},
                {"browser.done": {"extracted_content": None}},
                {"browser.fly": {}},
            ]
        }
        refusal = self._refuse(self._flat(), reply)
        assert refusal == (
            "the reply does not fit the output schema: next_goal: Field required; "
            f"action 1, {params_refusal(self._SPACE, Action(name='browser.click', params={'index': 'the cart', 'x': 1}))}; "
            f"action 3, {name_refusal(self._SPACE, 'browser.fly')}"
        )

    def test_an_action_nested_in_an_item_is_numbered_by_its_item(self) -> None:
        """Skyvern's shape, ``{"actions": [{"reasoning": ..., "action": {...}}]}``: the action
        is the item's, so it is numbered by the item's place in the list."""
        model = reply_model(self._SPACE.descriptors)
        reply = {
            "actions": [
                {"reasoning": 3, "action": {"browser.click": {"index": 1}}},
                {"reasoning": "then type", "action": {"browser.type": {"index": 2}}},
            ]
        }
        refusal = self._refuse(model, reply)
        same = params_refusal(self._SPACE, Action(name="browser.type", params={"index": 2}))
        assert refusal == (
            "the reply does not fit the output schema: actions[0].reasoning: Input should be a "
            f"valid string; action 2, {same}"
        )

    def test_a_reply_that_is_not_an_object_has_no_path(self) -> None:
        refusal = self._refuse(self._flat(), [1])
        assert refusal == "the reply does not fit the output schema: Input should be an object"

    def test_the_union_reports_its_mistakes_as_typed_errors(self) -> None:
        """The refusal reads the union's error types and context, never its message."""
        reply = {
            "next_goal": "x",
            "action": [{"browser.fly": {}}, [], {"browser.type": {"index": 2}}],
        }
        with pytest.raises(ValidationError) as caught:
            self._flat().model_validate(reply)
        errors = caught.value.errors()
        assert [(error["type"], error["loc"]) for error in errors] == [
            (ACTION_UNKNOWN, ("action", 0)),
            (ACTION_SHAPE, ("action", 1)),
            (ACTION_PARAMS, ("action", 2)),
        ]
        assert errors[0].get("ctx", {})["verb"] == "browser.fly"
        assert errors[1].get("ctx", {})["written"] == "an array"
        params = errors[2].get("ctx", {})
        assert params["verb"] == "browser.type"
        assert params["problems"] == ({"param": "text", "msg": "Field required"},)
