"""The component identity of a run, resolved once by Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunIdentity:
    """Immutable attribution shared by execution, recording, and results."""

    task_id: str = "unknown"
    model_id: str = ""
    agent_id: str = ""
    loop_id: str = ""
    environment_id: str = ""

    @classmethod
    def resolve(cls, *, agent: Any, loop: Any, env: Any, task_id: str) -> RunIdentity:
        """Resolve identity from the composed components at assembly time."""
        model_id = ""
        llm = getattr(agent, "llm", None)
        for name in ("model", "model_name", "name"):
            value = getattr(llm, name, None)
            if isinstance(value, str) and value:
                model_id = value
                break
        return cls(
            task_id=task_id,
            model_id=model_id,
            agent_id=cls._component_id(agent, "agent_id"),
            loop_id=cls._component_id(loop, "loop_id"),
            environment_id=cls._component_id(env, "env_type"),
        )

    @staticmethod
    def _component_id(component: Any, accessor: str) -> str:
        value = getattr(component, accessor, "")
        return value if isinstance(value, str) else ""
