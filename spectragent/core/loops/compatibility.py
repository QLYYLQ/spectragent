"""Compatibility Protocols — what an Agent must implement for each Loop.

These Protocols use target architecture method signatures. Existing agents
(BrowserUseAgent, etc.) will be adapted in M12 (Agent base class rework).

One Agent can implement multiple Protocols (one codebase supports multiple
Loops), declared via ``__wiring__`` on the Agent class.

See docs/architecture_design.md §4.1 "Loop 协议体系" for the full specification.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReactCompatible(Protocol):
    """Agent must satisfy this to be driven by ReactLoop.

    ReactLoop cycle: observe → build_react_prompt → LLM → parse_react_output → act
    """

    def build_react_prompt(self, observation: Any) -> list[Any]:
        """Build LLM message list from the current observation.

        Returns a list of BaseMessage (or compatible) objects.
        """
        ...

    def parse_react_output(self, llm_output: Any) -> list[Any]:
        """Parse LLM output into a list of Action objects."""
        ...

    def get_react_output_model(self) -> type | None:
        """Return the Pydantic model for structured LLM output, or None."""
        ...


@runtime_checkable
class CodeActCompatible(Protocol):
    """Agent must satisfy this to be driven by CodeActLoop.

    CodeActLoop cycle: observe → build_code_prompt → LLM → sandbox execute → verify
    """

    def build_code_prompt(self, observation: Any) -> list[Any]:
        """Build LLM message list for code generation."""
        ...

    def parse_code_output(self, code_result: str) -> list[Any]:
        """Parse code execution result into actions."""
        ...


@runtime_checkable
class FuncCallCompatible(Protocol):
    """Agent must satisfy this to be driven by FunctionCallingLoop.

    FunctionCallingLoop: LLM native tool_call → Action → ActionResult → tool_call_result
    """

    def build_funccall_prompt(self, observation: Any) -> list[Any]:
        """Build LLM message list for function calling."""
        ...

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas in OpenAI function calling format."""
        ...


@runtime_checkable
class PlanExecCompatible(Protocol):
    """Agent must satisfy this to be driven by PlanExecuteLoop.

    PlanExecuteLoop: plan() → execute_steps() → optional replan()
    """

    def build_plan_prompt(self, observation: Any) -> list[Any]:
        """Build LLM message list for plan generation."""
        ...

    def build_exec_prompt(self, observation: Any, plan: Any) -> list[Any]:
        """Build LLM message list for executing a plan step."""
        ...

    def parse_plan(self, llm_output: Any) -> Any:
        """Parse LLM output into a structured plan."""
        ...
