"""ClickAction — click a DOM element by index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import RequiresFragment
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    resolve_index,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import ClickElementEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ClickAction(SettleableAction, ActionBase):
    """Click a DOM element by its selector_map index."""

    __registry_key__ = "browser.click"

    class Params(BaseModel):
        """An element of the selector map, which only ``browser.dom`` builds.

        The dependency is on the param, and the param is required, so the
        whole action inherits it: a run that did not negotiate
        ``browser.dom`` has no index to click and must not be told it can.
        """

        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int,
            RequiresFragment("browser.dom"),
            PydanticField(description="element index from selector_map"),
        ]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Click a DOM element by its index in the selector map.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            element = resolve_index(capability, params["index"])
            scope = focused_scope(capability)
            event = ClickElementEvent(
                backend_node_id=element.backend_node_id,
                session_id=element.session_id,
            )
            await dispatch_request(scope, event)
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
