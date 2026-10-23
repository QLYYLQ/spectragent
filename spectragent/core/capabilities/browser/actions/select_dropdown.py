"""SelectDropdownAction — select an option from a <select> element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement
from spectragent.core.capabilities.browser.actions._common import (
    effect_from,
    focused_scope,
    handler_field,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import SelectionEffect
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import (
    SelectDropdownEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SelectDropdownAction(SettleableAction, ActionBase):
    """Pick an option from a dropdown by value / visible text / sub-index.

    Reports a :class:`SelectionEffect`: the option selected after the action
    (its text withheld for a sensitive control) and whether it changed.
    """

    __registry_key__ = "browser.select_dropdown"
    Effect = SelectionEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int,
            AddressesElement("browser.dom"),
            PydanticField(description="element index of the <select>"),
        ]
        value: str | None = None
        text: Annotated[str | None, PydanticField(description="visible option text")] = None
        option_index: Annotated[int | None, PydanticField(description="0-based option index")] = (
            None
        )

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Select an option from a dropdown element.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            element = resolve_index(capability, params["index"])
            scope = focused_scope(capability)
            event = SelectDropdownEvent(
                backend_node_id=element.backend_node_id,
                session_id=element.session_id,
                value=params.get("value"),
                text=params.get("text"),
                index=params.get("option_index"),
            )
            await dispatch_request(scope, event)
            facts = handler_field(event, "effect_facts")
            await self.settle()
            return ActionResult(success=True, effect=effect_from(self, facts))
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
