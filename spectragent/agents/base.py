"""BaseAgent Protocol — the public contract for all agent implementations.

M12 refactoring: Agent is now a passive domain-knowledge provider.
Execution is driven by AgentRuntime + Loop, not by the Agent itself.

See docs/architecture_design.md §4.1 and docs/execution_dag.md M12.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.prompt import PromptSnapshot


@runtime_checkable
class BaseAgent(Protocol):
    """Protocol that every agent must satisfy.

    Agents are passive domain-knowledge providers. They declare their
    capabilities (action space, observation needs) and provide domain
    logic (prompt building, output parsing). Execution is driven by
    AgentRuntime + Loop.

    Loop-specific behaviour is declared via callable slots that each
    Loop checks at weave time — see core/loops/_registrar.py.
    """

    @property
    def agent_name(self) -> str:
        """Human-readable identifier for this agent."""
        ...

    @property
    def domain(self) -> str:
        """Domain identifier (e.g. 'web_navigation', 'software_engineering')."""
        ...

    @property
    def observation_types(self) -> set[str]:
        """Set of observation modalities this agent consumes.

        Examples: ``{"dom", "screenshot", "accessibility_tree"}``.
        """
        ...

    @property
    def system_prompt(self) -> PromptSnapshot | None:
        """System prompt snapshot from the last run."""
        ...

    def get_action_space(self) -> list[ActionDescriptor]:
        """Declare the actions this agent can emit."""
        ...
