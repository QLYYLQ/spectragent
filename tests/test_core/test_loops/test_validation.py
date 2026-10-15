"""Tests for slot-based compatibility validation."""

import pytest

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.validation import IncompatibleError


class _SlotLoop(AgentLoopBase):
    __registry_key__ = "test_slot_validation"
    __required_slots__ = {"build_react_prompt", "parse_react_output"}
    __optional_slots__ = {"get_react_output_model"}

    async def _execute(self, agent, task, **kwargs):
        return {}


class _NoSlotLoop(AgentLoopBase):
    __registry_key__ = "test_no_slot_validation"
    __required_slots__ = set()

    async def _execute(self, agent, task, **kwargs):
        return {}


def teardown_module():
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    for key in ("test_slot_validation", "test_no_slot_validation"):
        if AgentLoopRegistrar.get_or_none(key) is not None:
            AgentLoopRegistrar.unregister(key)


class TestValidateSlots:
    """validate_slots checks required slots on agent."""

    def test_compatible_passes(self) -> None:
        class GoodAgent:
            agent_name = "good"

            __execution_limit_fields__ = frozenset()

            async def build_react_prompt(self, observation, *, context):
                return []

            def parse_react_output(self, llm_output):
                return []

        _SlotLoop().validate_slots(GoodAgent())  # Should not raise

    def test_incompatible_raises(self) -> None:
        class BadAgent:
            agent_name = "bad"

        with pytest.raises(IncompatibleError) as exc_info:
            _SlotLoop().validate_slots(BadAgent())

        assert "bad" in str(exc_info.value)
        assert "test_slot_validation" in str(exc_info.value)
        assert len(exc_info.value.missing_slots) > 0

    def test_no_required_slots_always_passes(self) -> None:
        class AnyAgent:
            agent_name = "any"

        _NoSlotLoop().validate_slots(AnyAgent())  # Should not raise

    def test_error_lists_missing_slots(self) -> None:
        class PartialAgent:
            agent_name = "partial"

            __execution_limit_fields__ = frozenset()

            async def build_react_prompt(self, observation, *, context):
                return []

        with pytest.raises(IncompatibleError) as exc_info:
            _SlotLoop().validate_slots(PartialAgent())

        assert "parse_react_output" in exc_info.value.missing_slots

    def test_optional_slot_missing_does_not_raise(self) -> None:
        class MinimalAgent:
            agent_name = "minimal"

            __execution_limit_fields__ = frozenset()

            async def build_react_prompt(self, observation, *, context):
                return []

            def parse_react_output(self, llm_output):
                return []

            # get_react_output_model is optional — not implemented

        _SlotLoop().validate_slots(MinimalAgent())  # Should not raise


class TestWording:
    """Who needs the slots is part of the message (arch §4.1 "可选 slot", M32)."""

    def test_assembly_wording_is_unchanged(self) -> None:
        class BadAgent:
            agent_name = "bad"

        with pytest.raises(IncompatibleError) as exc_info:
            _SlotLoop().validate_slots(BadAgent())
        assert str(exc_info.value) == (
            "Agent 'bad' is missing slots required by loop 'test_slot_validation': "
            "build_react_prompt, parse_react_output"
        )
        assert exc_info.value.needed_for == "required by loop 'test_slot_validation'"


def test_export_wording() -> None:
    """An optional slot the export needs never reads like a Loop's required one."""
    error = IncompatibleError(
        "ui_tars",
        "react",
        ["format_react_output"],
        needed_for="needed for training export (optional slots of loop 'react')",
    )
    assert str(error) == (
        "Agent 'ui_tars' is missing slots needed for training export "
        "(optional slots of loop 'react'): format_react_output"
    )
    assert "required by" not in str(error)
    assert (error.agent_name, error.loop_id, error.missing_slots) == (
        "ui_tars",
        "react",
        ["format_react_output"],
    )
