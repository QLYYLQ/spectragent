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
        self._scope: EventScope | None = None
        self._bridge: CDPEventBridge | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> PopupsWatchdog:
        return cls(ctx.cdp)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id

        self._bridge = CDPEventBridge(self._cdp, scope, session_id=session_id)
        self._bridge.bridge(
            "Page.javascriptDialogOpening",
            self._make_dialog_event,
        )

        # Handle the event to auto-dismiss.
        scope.connect(
            DialogOpenedEvent,
            self._on_dialog_opened,
            mode=ConnectionType.QUEUED,
        )

    def detach(self) -> None:
        """Unregister this watchdog's CDP callbacks. Idempotent.

        Without this the bridge outlives its scope: ``close_tab`` /
        ``teardown`` close the EventScope but the CDP client still holds
        the bridge callback, and ``EventScope.emit`` raises RuntimeError
        on a closed scope — swallowed by CDPClient's dispatch loop, so
        the leak shows up only as recurring exception spam.
        """
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
        self._scope = None

    def _make_dialog_event(self, params: dict[str, Any]) -> DialogOpenedEvent:
        return DialogOpenedEvent(
            dialog_type=params.get("type", ""),
            message=params.get("message", ""),
            url=params.get("url", ""),
            default_prompt=params.get("defaultPrompt", ""),
        )

    async def _on_dialog_opened(self, event: DialogOpenedEvent) -> None:
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
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.error("Failed to handle dialog: %s", exc)
