"""Compatibility validation — checks Agent satisfies Loop's required Protocol.

Reads ``__requires_protocol__`` from the Loop class (set via conscribe
registration metadata), not from a hardcoded mapping dict.

See docs/execution_dag.md M03 "兼容性验证".
"""

from __future__ import annotations

from typing import Any


class IncompatibleError(Exception):
    """Raised when an Agent does not satisfy a Loop's required Protocol."""

    def __init__(
        self,
        agent_name: str,
        protocol_name: str,
        loop_id: str,
        missing_methods: list[str],
    ) -> None:
        self.agent_name = agent_name
        self.protocol_name = protocol_name
        self.loop_id = loop_id
        self.missing_methods = missing_methods
        super().__init__(
            f"Agent '{agent_name}' does not satisfy {protocol_name} "
            f"required by loop '{loop_id}'. "
            f"Missing methods: {', '.join(missing_methods)}"
        )


def _find_missing_methods(obj: Any, protocol: type) -> list[str]:
    """Find Protocol methods missing from obj."""
    missing: list[str] = []
    for name in dir(protocol):
        if name.startswith("_"):
            continue
        member = getattr(protocol, name, None)
        if callable(member) and not hasattr(obj, name):
            missing.append(name)
    return sorted(missing)


def validate_compatibility(agent: Any, loop: Any) -> None:
    """Check that agent satisfies the Loop's required Protocol.

    Reads ``loop.__requires_protocol__`` to determine which Protocol to check.
    If the Loop does not declare a required Protocol, validation passes.

    Args:
        agent: Agent instance to validate.
        loop: Loop instance (must have ``__requires_protocol__`` class attribute).

    Raises:
        IncompatibleError: If the agent does not satisfy the required Protocol.
    """
    protocol = getattr(loop, "__requires_protocol__", None)
    if protocol is None:
        return

    if not isinstance(agent, protocol):
        agent_name = getattr(agent, "agent_name", type(agent).__name__)
        loop_id = getattr(loop, "loop_id", type(loop).__name__)
        missing = _find_missing_methods(agent, protocol)
        raise IncompatibleError(
            agent_name=agent_name,
            protocol_name=protocol.__name__,
            loop_id=loop_id,
            missing_methods=missing,
        )
