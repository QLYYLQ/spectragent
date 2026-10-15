"""PopupsWatchdog — automatic JS dialog handling via CDP.

Bridges ``Page.javascriptDialogOpening`` and auto-accepts alerts/confirms,
auto-cancels prompts.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from agent_cdp import (
    CDPCommandProtocol,
    CDPEventBridge,
    ConnectionType,
    EventScope,
    Participation,
    ResultContract,
)

from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import DialogOpenedEvent

logger = logging.getLogger(__name__)


class PopupsWatchdog(BrowserWatchdog):
    """Auto-handles JavaScript dialogs (alert, confirm, prompt, beforeunload)."""

    level: ClassVar[WatchdogLevel] = "tab"

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        auto_accept: bool = True,
    ) -> None:
        self._cdp = cdp
        self._auto_accept = auto_accept

    @classmethod
    def build(cls, ctx: WatchdogContext) -> PopupsWatchdog:
        return cls(ctx.cdp)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        bridge = CDPEventBridge(self._cdp, scope, session_id=session_id)
        bridge.bridge("Page.javascriptDialogOpening", self._make_dialog_event)

        async def handle(event: DialogOpenedEvent) -> None:
            await self._on_dialog_opened(event, session_id=session_id)

        connection = scope.connect(
            DialogOpenedEvent,
            handle,
            mode=ConnectionType.QUEUED,
            participation=Participation.HANDLER,
            result_contract=ResultContract(lambda value: value is None),
        )
        self.track_binding(
            scope, session_id, connections=[connection], close_callbacks=[bridge.close]
        )

    def _make_dialog_event(self, params: dict[str, Any]) -> DialogOpenedEvent:
        return DialogOpenedEvent(
            dialog_type=params.get("type", ""),
            message=params.get("message", ""),
            url=params.get("url", ""),
            default_prompt=params.get("defaultPrompt", ""),
        )

    async def _on_dialog_opened(self, event: DialogOpenedEvent, *, session_id: str | None) -> None:
        """Auto-dismiss JavaScript dialog."""
        if not self._auto_accept:
            return

        accept = event.dialog_type in ("alert", "confirm", "beforeunload")
        logger.info(
            "Auto-%s dialog: type=%s message=%s",
            "accepting" if accept else "dismissing",
            event.dialog_type,
            event.message[:100],
        )
        try:
            await self._cdp.send(
                "Page.handleJavaScriptDialog",
                {
                    "accept": accept,
                },
                session_id=session_id,
            )
        except Exception as exc:
            logger.error("Failed to handle dialog: %s", exc)
            raise
