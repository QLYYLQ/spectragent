"""TypeAction — type text into a DOM element.

Filename uses ``type_action`` to avoid shadowing the Python ``type``
builtin, while the conscribe registry key stays ``"type"`` to match the
canonical BrowserCapability vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import RequiresFragment
from spectragent.core.capabilities.browser.actions._common import (
    effect_from,
    focused_scope,
    handler_field,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import TextInputEffect
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import TypeTextEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class TypeAction(SettleableAction, ActionBase):
    """Type text into a DOM element by its selector_map index."""

    __registry_key__ = "browser.type"

    #: The control the text went into, whether its value changed (never the
    #: value), the clear's keys, and the focus afterwards.
    Effect = TextInputEffect

    # ⭐ capability-side configurable side-effect
    clear_by_default: Annotated[
        bool,
        PydanticField(description="Whether to clear the target element first"),
    ] = True

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int,
            RequiresFragment("browser.dom"),
            PydanticField(description="element index"),
        ]
        text: str
        #: Omitted means ``clear_by_default``. Optional rather than
        #: defaulted here on purpose: the default is the capability's to
        #: apply, and a model that writes nothing must not have a value
        #: manufactured for it on the way in.
        clear: bool | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Type text into a DOM element.",
            # Interpolated, not literal: ``clear_by_default`` is a YAML
            # knob, and this string is what the prompt teaches.
            hints={"clear": f"default={str(self.clear_by_default).lower()}"},
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            element = resolve_index(capability, params["index"])
            text = params.get("text", "")
            # ``params.get("clear", default)`` returned False for an
            # explicit ``clear: null``, overriding the capability knob
            # with a value the model never chose.
            requested = params.get("clear")
            clear = self.clear_by_default if requested is None else bool(requested)
            scope = focused_scope(capability)
            event = TypeTextEvent(
                backend_node_id=element.backend_node_id,
                session_id=element.session_id,
                text=text,
                clear=clear,
            )
            try:
                await dispatch_request(scope, event)
            except RuntimeError as exc:
                # The handler answers with an ``error`` when focusing or typing
                # failed, or when no text field had focus (it then
                # types nothing, and still answers the effect it measured).
                return ActionResult(
                    success=False,
                    error=str(exc),
                    effect=effect_from(self, handler_field(event, "effect_facts")),
                )
            effect = effect_from(self, handler_field(event, "effect_facts"))
            await self.settle()
            return ActionResult(
                success=True,
                typed_into_sensitive_field=handler_field(event, "sensitive"),
                effect=effect,
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
