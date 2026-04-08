"""Tests for compatibility validation logic."""

import pytest

from spectragent.core.loops.compatibility import ReactCompatible
from spectragent.core.loops.validation import IncompatibleError, validate_compatibility


class _MockLoop:
    """Mock loop for testing."""

    def __init__(self, requires_protocol=None):
        self.__requires_protocol__ = requires_protocol
        self.loop_id = "mock_loop"

    async def run(self, agent, env, task, **kwargs):
        return {}


class TestValidateCompatibility:
    """validate_compatibility reads __requires_protocol__ from loop."""

    def test_compatible_passes(self) -> None:
        class GoodAgent:
            agent_name = "good"
            def build_react_prompt(self, observation):
                return []
            def parse_react_output(self, llm_output):
                return []
            def get_react_output_model(self):
                return None

        loop = _MockLoop(requires_protocol=ReactCompatible)
        validate_compatibility(GoodAgent(), loop)  # Should not raise

    def test_incompatible_raises(self) -> None:
        class BadAgent:
            agent_name = "bad"

        loop = _MockLoop(requires_protocol=ReactCompatible)
        with pytest.raises(IncompatibleError) as exc_info:
            validate_compatibility(BadAgent(), loop)

        assert "bad" in str(exc_info.value)
        assert "ReactCompatible" in str(exc_info.value)
        assert "mock_loop" in str(exc_info.value)
        assert len(exc_info.value.missing_methods) > 0

    def test_no_protocol_always_passes(self) -> None:
        class AnyAgent:
            agent_name = "any"

        loop = _MockLoop(requires_protocol=None)
        validate_compatibility(AnyAgent(), loop)  # Should not raise

    def test_error_lists_missing_methods(self) -> None:
        class PartialAgent:
            agent_name = "partial"
            def build_react_prompt(self, observation):
                return []

        loop = _MockLoop(requires_protocol=ReactCompatible)
        with pytest.raises(IncompatibleError) as exc_info:
            validate_compatibility(PartialAgent(), loop)

        missing = exc_info.value.missing_methods
        assert "parse_react_output" in missing or "get_react_output_model" in missing
