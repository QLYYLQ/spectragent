"""BaseAction Protocol — contract for all concrete action classes.

An Action is a single conscribe-registered unit — symmetric to an
ObservationFragment. Each action owns its own descriptor, its own execute
behavior, and its own capability-side configuration (init parameters).

See docs/architecture_design.md §4.5 "Action 体系 + 对称协商 + 前缀路由".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic import BaseModel

    from spectragent.schemas.action import ActionDescriptor, ActionResult


@runtime_checkable
class BaseAction(Protocol):
    """Contract for all concrete action implementations.

    Attributes:
        action_id: Registry discriminator — matches ``__registry_key__``.
        Params: The action's parameter contract as a flat Pydantic model.
            It is what the descriptor's ``params_schema`` and ``params``
            prose are both rendered from, and where a parameter's
            observation-fragment dependency is declared
            (:class:`~spectragent.core.actions.params.RequiresFragment`).
            An action that wrote its prose by hand and read its params out
            of a bare dict had no contract at all — only two descriptions
            of one, free to disagree.
    """

    action_id: str
    Params: ClassVar[type[BaseModel]]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        """Produce an ActionDescriptor for prompt construction.

        ``**context`` lets dynamic actions (e.g. tool_sandbox's
        ``CallToolAction``) vary their descriptor per-task, and carries
        the run's active observation ``fragments`` so a param that needs
        one that is not live can be dropped. Static actions simply ignore
        the kwargs.
        """
        ...

    def requires_fragments(self) -> frozenset[str]:
        """Observation fragments this action cannot run without.

        Fully-qualified keys, because the providing capability is the one
        that knows: ``browser.click``'s index addresses ``browser.dom``'s
        selector map.
        """
        ...

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: Any,
    ) -> ActionResult:
        """Execute one action instance against its owning capability.

        The capability is injected whole rather than as a per-action
        "dependency bag": an action belongs to exactly one capability,
        the two ship together, and the capability is therefore free to
        offer its actions whatever surface it likes.

        **What that surface is, is the capability's business.** This
        Protocol prescribes no shape for it and names no attribute of
        any particular domain. Nothing outside a capability's own
        package should read it.

        Note there is no per-call routing argument. Which sub-context an
        action lands in — tab, container, window, device — is capability
        *state*, selected by an action of that capability (``switch_tab``
        and friends) and remembered until changed. A scalar handle
        threaded through this signature would be a single global slot
        that only one domain could ever fill; a capability that truly
        needs per-call routing takes an opaque mapping keyed by
        ``capability_id``, never a bare id.
        """
        ...
