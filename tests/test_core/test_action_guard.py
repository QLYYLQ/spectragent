"""Tests for build_guarded_act — prompt-capability consistency guarantee."""

import asyncio

from spectragent.core.action_guard import build_guarded_act
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

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
        assert len(filtered) == 3

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
        assert {d.name for d in filtered} == {"click", "done"}

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
        assert len(filtered) == 0
