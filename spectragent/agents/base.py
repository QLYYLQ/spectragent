"""BaseAgent Protocol — the public contract for all agent implementations."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.prompt import PromptSnapshot


@runtime_checkable
class BaseAgent(Protocol):
    """Protocol that every agent must satisfy.

    Agents are stateful: ``run()`` drives a full task loop,
    ``step()`` performs a single observe-think-act cycle,
    and ``reset()`` clears internal state between tasks.
    """

    @property
    def agent_name(self) -> str:
        """Human-readable identifier for this agent."""
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

    async def step(self, observation: Any) -> list[Action]:
        """Single observe-think-act cycle. Returns one or more actions."""
        ...

    async def run(self, env: Any, task: str, **kwargs: Any) -> AgentResult:
        """Drive a full task loop against *env* until done or max_steps."""
        ...

    async def reset(self) -> None:
        """Clear internal state (memory, step counter, etc.)."""
        ...
