"""CrashWatchdog — browser target crash detection and focus recovery.

Bridges CDP ``Target.targetCrashed`` events and emits ``TargetCrashedEvent``.
Works with SessionManager for automatic agent focus recovery.
"""

from __future__ import annotations

import logging

from agent_cdp import (
    CDPCommandProtocol,
    CDPEventBridge,
    ConnectionType,
    EventScope,
    WatchdogBase,
)

from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    TargetCrashedEvent,
)

logger = logging.getLogger(__name__)


class CrashWatchdog(WatchdogBase):
    """Detects browser target crashes via CDP and emits TargetCrashedEvent."""

    def __init__(self, cdp: CDPCommandProtocol) -> None:
        self._cdp = cdp
        self._scope: EventScope | None = None
        self._session_id: str | None = None
        self._bridge: CDPEventBridge | None = None

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id

        # Bridge CDP crash events into the scope.
        self._bridge = CDPEventBridge(self._cdp, scope, session_id=session_id)
        self._bridge.bridge(
            "Inspector.targetCrashed",
            lambda params: TargetCrashedEvent(
                target_id=session_id or "",
                error=params.get("reason", "target crashed"),
            ),
        )

        # Also listen for network-level errors that may indicate issues.
        self._bridge.bridge(
            "Inspector.detached",
            lambda params: BrowserErrorEvent(
                error_type="inspector_detached",
                message=params.get("reason", ""),
            ),
        )

        # Listen to our own TargetCrashedEvent to log it.
        scope.connect(
            TargetCrashedEvent,
            self._on_target_crashed,
            mode=ConnectionType.DIRECT,
        )

    async def _on_target_crashed(self, event: TargetCrashedEvent) -> None:
        logger.error(
            "Target crashed: target_id=%s error=%s",
            event.target_id, event.error,
        )
