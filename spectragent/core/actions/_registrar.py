"""Action registrar — conscribe-driven auto-registration.

Each concrete Action class sets ``__registry_key__`` and can declare
capability-side configuration fields as ``Annotated[type, Field(...)]``.
Symmetric to :class:`spectragent.schemas.observation._registrar.FragmentBase`,
but uses a ``@dataclass`` base (no pydantic bridge) because actions are
executors, not data-transfer objects.

Reference pattern: :class:`spectragent.agents._registrar.AgentBase`, which
also combines ``@dataclass`` + conscribe metaclass + ``Annotated`` fields.

See docs/architecture_design.md §4.5 and docs/design_discussion.md §3.3.
"""

from __future__ import annotations

from typing import ClassVar

from conscribe import create_registrar

from spectragent.core.actions.base import BaseAction

ActionRegistrar = create_registrar(
    "action",
    BaseAction,
    discriminator_field="action_id",
)


class ActionBase(metaclass=ActionRegistrar.Meta):
    """Concrete base for all action implementations.

    Subclasses set ``__registry_key__`` to a fully-qualified dotted key —
    ``"{source}.{local_id}"`` (e.g. ``"browser.click"``,
    ``"tool_sandbox.call_tool"``). The prefix makes registration across
    capabilities collision-free (``browser.click`` vs ``desktop.click``).
    :attr:`action_id` returns the full key (which is what environments
    route on) and :attr:`source` returns the prefix segment for callers
    that need the owning-capability identity.

    Subclasses should use ``@dataclass`` + ``Annotated[type, Field(...)]``
    fields to expose capability-side configuration to conscribe codegen.

    Example::

        from dataclasses import dataclass
        from typing import Annotated
        from pydantic import Field as PydanticField

        @dataclass
        class ScrollAction(ActionBase):
            __registry_key__ = "browser.scroll"

            default_amount: Annotated[
                int, PydanticField(description="Default scroll px")
            ] = 300

            def descriptor(self, **context):
                return ActionDescriptor(
                    name=self.__registry_key__,
                    description=f"Scroll the page (default {self.default_amount}px).",
                    params={"direction": "up|down|left|right"},
                )

            async def execute(self, params, *, capability):
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def action_id(self) -> str:
        """Fully-qualified registry key (e.g. ``"browser.click"``)."""
        return self.__registry_key__

    @property
    def source(self) -> str:
        """First segment of ``__registry_key__`` — the owning capability."""
        return self.__registry_key__.split(".", 1)[0]
