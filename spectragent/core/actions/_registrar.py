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

#: Separator between the owning capability and the local action id.
#:
#: Declaring it is what lets conscribe expand a capability-relative
#: ``__wiring__`` entry (``"click"``) into every provider that offers it, and
#: what lets negotiation project the environment's fully-qualified keys down
#: to the short names an agent declares. It does not change the
#: discriminator: keys stay flat and fully qualified everywhere else —
#: registration, routing, descriptors, the trace.
ACTION_KEY_SEPARATOR = "."

ActionRegistrar = create_registrar(
    "action",
    BaseAction,
    discriminator_field="action_id",
    key_separator=ACTION_KEY_SEPARATOR,
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

    **Consumers do not have to spell the prefix out.** An agent needing a
    click says ``"click"`` in its ``__wiring__``; conscribe widens that
    to every registered provider at config-generation time, and
    negotiation binds it to the one this run holds (or refuses two).
    Writing ``"browser.click"`` instead pins that provider. Either way
    the key that leaves negotiation is fully qualified.

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
                    name=self.action_id,
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
