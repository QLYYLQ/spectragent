"""BaseAction Protocol — contract for all concrete action classes.

An Action is a single conscribe-registered unit — symmetric to an
ObservationFragment. Each action owns its own descriptor, its own execute
behavior, and its own capability-side configuration (init parameters).

See docs/architecture_design.md §4.5 "Action 体系 + 对称协商 + 前缀路由".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spectragent.schemas.action import ActionDescriptor, ActionResult


@runtime_checkable
class BaseAction(Protocol):
    """Contract for all concrete action implementations.

    Attributes:
        action_id: Registry discriminator — matches ``__registry_key__``.
    """

    action_id: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        """Produce an ActionDescriptor for prompt construction.

        ``**context`` lets dynamic actions (e.g. tool_sandbox's
        ``CallToolAction``) vary their descriptor per-task. Static
        actions simply ignore the kwargs.
        """
        ...

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: Any,
        session_id: str,
    ) -> ActionResult:
        """Execute one action instance against the injected capability.

        The capability is injected as a whole to avoid designing a
        per-action "dependency bag". Actions are conceptually internals
        of their capability, so they may touch its private state
        (``_scopes``, ``_selector_maps``, ``_cdp``, etc.).
        """
        ...
