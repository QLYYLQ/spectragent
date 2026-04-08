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

    Subclasses must set:
    - ``__registry_key__``: conscribe registration key (e.g. ``"click"``)

    Subclasses should use ``@dataclass`` + ``Annotated[type, Field(...)]``
    fields to expose capability-side configuration to conscribe codegen.

    Example::

        from dataclasses import dataclass
        from typing import Annotated
        from pydantic import Field as PydanticField

        @dataclass
        class ScrollAction(ActionBase):
            __registry_key__ = "scroll"

            default_amount: Annotated[
                int, PydanticField(description="Default scroll px")
            ] = 300

            def descriptor(self, **context):
                return ActionDescriptor(
                    name="scroll",
                    description=f"Scroll the page (default {self.default_amount}px).",
                    params={"direction": "up|down|left|right"},
                )

            async def execute(self, params, *, capability, session_id):
                ...
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def action_id(self) -> str:
        return self.__registry_key__
