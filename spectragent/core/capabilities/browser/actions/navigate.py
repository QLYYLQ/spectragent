"""NavigateAction — navigate the current tab to a URL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import NavigateToUrlEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class NavigateAction(SettleableAction, ActionBase):
    """Navigate the current tab to a URL."""

    __registry_key__ = "browser.navigate"

    # ⭐ capability-side configurable side-effects
    wait_until: Annotated[
        str,
        PydanticField(
            description=(
                "Navigation wait mode: load | domcontentloaded | "
                "networkidle | '' (no wait). The watchdog blocks "
                "Page.navigate's completion event until this CDP "
                "lifecycle phase fires; pair with post_delay_ms "
                "for sites that mutate DOM via JS timers post-load."
            ),
        ),
    ] = "load"
    timeout_ms: Annotated[
        int,
        PydanticField(description="Per-call navigation timeout in milliseconds"),
    ] = 30000
    lifecycle_timeout_ms: Annotated[
        int | None,
        PydanticField(
            description=(
                "Timeout (ms) for the wait_until lifecycle wait. "
                "When None, falls back to timeout_ms. Independent "
                "from event_timeout (which gates handler completion)."
            ),
        ),
    ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.navigate",
            description="Navigate to a URL.",
            params={"url": "str"},
        )

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
            await self.settle()
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
