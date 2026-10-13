"""NavigateAction — navigate the current tab to a URL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    handler_error,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import NavigateToUrlEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class NavigateAction(SettleableAction, ActionBase):
    """Navigate the current tab to a URL.

    Waits for the page to commit (``timeout_ms``), then for ``wait_until``
    (``lifecycle_timeout_ms``); a phase that does not arrive in time only logs
    a warning (``ActionWatchdog._on_navigate``). Fails when the navigation
    does not commit in time, or the browser refuses it or reports it failed.
    """

    __registry_key__ = "browser.navigate"

    # ⭐ capability-side configurable side-effects
    wait_until: Annotated[
        str,
        PydanticField(
            description=(
                "What to wait for once the page committed: load | "
                "domcontentloaded | networkidle | '' (no wait). When it does "
                "not arrive within lifecycle_timeout_ms, a warning is logged "
                "and the navigation succeeds with the page as it is; pair "
                "with post_delay_ms for sites that change the page by timer "
                "after load."
            ),
        ),
    ] = "load"
    timeout_ms: Annotated[
        int,
        PydanticField(
            description=(
                "How long the page may take to commit (the browser answering "
                "the navigation), in milliseconds. A navigation that has not "
                "committed by then fails."
            ),
        ),
    ] = 30000
    lifecycle_timeout_ms: Annotated[
        int | None,
        PydanticField(
            description=(
                "How long to wait for wait_until after the commit, in "
                "milliseconds. None (the default): timeout_ms."
            ),
        ),
    ] = None

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        url: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Navigate to a URL.", **context)

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = NavigateToUrlEvent(
                url=params["url"],
                wait_until=self.wait_until,
                timeout_ms=self.timeout_ms,
                lifecycle_timeout_ms=self.lifecycle_timeout_ms,
            )
            scope.emit(event)
            await event
            error = handler_error(event)
            if error is not None:
                return ActionResult(success=False, error=error)
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
