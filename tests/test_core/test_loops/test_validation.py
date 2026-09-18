"""Tests for slot-based compatibility validation."""

import pytest

from spectragent.core.loops._registrar import AgentLoopBase, AgentLoopRegistrar
from spectragent.core.loops.validation import IncompatibleError, validate_compatibility


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
    AgentLoopRegistrar.unregister("test_slot_validation")
    AgentLoopRegistrar.unregister("test_no_slot_validation")


class TestValidateSlots:
    """_validate_slots checks required slots on agent."""

    def test_compatible_passes(self) -> None:
        class GoodAgent:
            agent_name = "good"

            def build_react_prompt(self, observation):
                return []

            def parse_react_output(self, llm_output):
                return []

        _SlotLoop()._validate_slots(GoodAgent())  # Should not raise

    def test_incompatible_raises(self) -> None:
        class BadAgent:
            agent_name = "bad"

        with pytest.raises(IncompatibleError) as exc_info:
            _SlotLoop()._validate_slots(BadAgent())

        assert "bad" in str(exc_info.value)
        assert "test_slot_validation" in str(exc_info.value)
        assert len(exc_info.value.missing_slots) > 0

    def test_no_required_slots_always_passes(self) -> None:
        class AnyAgent:
            agent_name = "any"

        _NoSlotLoop()._validate_slots(AnyAgent())  # Should not raise

    def test_error_lists_missing_slots(self) -> None:
        class PartialAgent:
            agent_name = "partial"

            def build_react_prompt(self, observation):
                return []

        with pytest.raises(IncompatibleError) as exc_info:
            _SlotLoop()._validate_slots(PartialAgent())

        assert "parse_react_output" in exc_info.value.missing_slots

    def test_optional_slot_missing_does_not_raise(self) -> None:
        class MinimalAgent:
            agent_name = "minimal"

            def build_react_prompt(self, observation):
                return []

            def parse_react_output(self, llm_output):
                return []

            # get_react_output_model is optional — not implemented

        _SlotLoop()._validate_slots(MinimalAgent())  # Should not raise


class TestValidateCompatibilityWrapper:
    """validate_compatibility() delegates to loop._validate_slots()."""

    def test_delegates_to_loop(self) -> None:
        class GoodAgent:
            def build_react_prompt(self, observation):
                return []

            def parse_react_output(self, llm_output):
                return []

        validate_compatibility(GoodAgent(), _SlotLoop())  # Should not raise

    def test_raises_on_missing(self) -> None:
        class BadAgent:
            agent_name = "bad"

        with pytest.raises(IncompatibleError):
            validate_compatibility(BadAgent(), _SlotLoop())
