"""Tests for compatibility Protocol isinstance checks."""

from spectragent.core.loops.compatibility import (
    CodeActCompatible,
    FuncCallCompatible,
    PlanExecCompatible,
    ReactCompatible,
)


class TestReactCompatible:
    """ReactCompatible Protocol structural subtyping."""

    def test_satisfies_protocol(self) -> None:
        class GoodAgent:
            def build_react_prompt(self, observation):
                return []
            def parse_react_output(self, llm_output):
                return []
            def get_react_output_model(self):
                return None

        assert isinstance(GoodAgent(), ReactCompatible)

    def test_missing_method_fails(self) -> None:
        class BadAgent:
            def build_react_prompt(self, observation):
                return []
            # Missing parse_react_output and get_react_output_model

        assert not isinstance(BadAgent(), ReactCompatible)


class TestCodeActCompatible:
    def test_satisfies(self) -> None:
        class GoodAgent:
            def build_code_prompt(self, observation):
                return []
            def parse_code_output(self, code_result):
                return []

        assert isinstance(GoodAgent(), CodeActCompatible)

    def test_missing(self) -> None:
        class BadAgent:
            def build_code_prompt(self, observation):
                return []

        assert not isinstance(BadAgent(), CodeActCompatible)


class TestFuncCallCompatible:
    def test_satisfies(self) -> None:
        class GoodAgent:
            def build_funccall_prompt(self, observation):
                return []
            def get_tool_schemas(self):
                return []

        assert isinstance(GoodAgent(), FuncCallCompatible)


class TestPlanExecCompatible:
    def test_satisfies(self) -> None:
        class GoodAgent:
            def build_plan_prompt(self, observation):
                return []
            def build_exec_prompt(self, observation, plan):
                return []
            def parse_plan(self, llm_output):
                return {}

        assert isinstance(GoodAgent(), PlanExecCompatible)


class TestMultiProtocol:
    """One agent can satisfy multiple Protocols."""

    def test_react_and_codeact(self) -> None:
        class DualAgent:
            def build_react_prompt(self, observation):
                return []
            def parse_react_output(self, llm_output):
                return []
            def get_react_output_model(self):
                return None
            def build_code_prompt(self, observation):
                return []
            def parse_code_output(self, code_result):
                return []

        agent = DualAgent()
        assert isinstance(agent, ReactCompatible)
        assert isinstance(agent, CodeActCompatible)
