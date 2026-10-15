"""Tests for build_guarded_act — prompt-capability consistency guarantee."""

import asyncio

import pytest

from spectragent.core.action_guard import build_guarded_act, resolve_action_name
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace

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

    def test_valid_action_passes(self) -> None:
        _, guard = build_guarded_act(_mock_act, _DESCRIPTORS)
        result = asyncio.run(guard(Action(name="click")))
        assert result.success
        assert result.extracted_content == "click"

    def test_invalid_action_rejected(self) -> None:
        _, guard = build_guarded_act(_mock_act, _DESCRIPTORS)
        result = asyncio.run(guard(Action(name="invalid")))
        assert not result.success
        assert "invalid" in result.error

    def test_filter_narrows_set(self) -> None:
        filtered, _ = build_guarded_act(
            _mock_act,
            _DESCRIPTORS,
            action_filter=["click", "done"],
        )
        assert {d.name for d in filtered.descriptors} == {"click", "done"}

    def test_filter_rejects_excluded(self) -> None:
        _, guard = build_guarded_act(
            _mock_act,
            _DESCRIPTORS,
            action_filter=["click"],
        )
        result = asyncio.run(guard(Action(name="type")))
        assert not result.success

    def test_empty_filter_returns_empty(self) -> None:
        filtered, _ = build_guarded_act(_mock_act, _DESCRIPTORS, action_filter=[])
        assert len(filtered.descriptors) == 0

    @pytest.mark.parametrize("selection", [None, [], ["click"]])
    async def test_recorded_space_matches_prompt_and_execution(self, selection) -> None:
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
