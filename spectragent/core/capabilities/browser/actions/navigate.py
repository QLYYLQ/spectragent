"""NavigateAction — navigate the current tab to a URL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import NavigateToUrlEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class NavigateAction(ActionBase):
    """Navigate the current tab to a URL."""

    __registry_key__ = "navigate"

    # ⭐ capability-side configurable side-effects
    wait_until: Annotated[
        str,
        PydanticField(
            description="Navigation wait mode: load | domcontentloaded | networkidle",
        ),
    ] = "load"
    timeout_ms: Annotated[
        int,
        PydanticField(description="Per-call navigation timeout in milliseconds"),
    ] = 30000

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="navigate",
            description="Navigate to a URL.",
            params={"url": "str"},
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: "BrowserCapability",
        session_id: str,
    ) -> ActionResult:
        try:
            scope = capability._scopes[session_id]
            event = NavigateToUrlEvent(
                url=params["url"],
                wait_until=self.wait_until,
                timeout_ms=self.timeout_ms,
            )
            scope.emit(event)
            await event
            return ActionResult(success=True)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
