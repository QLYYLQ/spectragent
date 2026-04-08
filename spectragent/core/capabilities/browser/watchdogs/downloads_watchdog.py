"""DownloadsWatchdog — file download tracking via CDP Browser domain.

Bridges ``Browser.downloadWillBegin`` and ``Browser.downloadProgress`` CDP
events, emits ``DownloadStartedEvent``, ``DownloadProgressEvent``, and
``FileDownloadedEvent``.

Also supports direct callback registration for ActionWatchdog cross-concern
(click → download detection without EventBus latency).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_cdp import (
    CDPCommandProtocol,
    CDPEventBridge,
    EventScope,
    WatchdogBase,
)

from spectragent.core.capabilities.browser.watchdogs.events import (
    DownloadProgressEvent,
    DownloadStartedEvent,
    FileDownloadedEvent,
)

logger = logging.getLogger(__name__)


class DownloadsWatchdog(WatchdogBase):
    """Tracks file downloads via CDP Browser.download* events."""

    def __init__(self, cdp: CDPCommandProtocol) -> None:
        self._cdp = cdp
        self._scope: EventScope | None = None
        self._bridge: CDPEventBridge | None = None

        # Active downloads: guid → info.
        self._active: dict[str, dict[str, Any]] = {}

        # Direct callbacks for cross-concern (ActionWatchdog click→download).
        self._on_start_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._on_complete_callbacks: list[Callable[[dict[str, Any]], None]] = []

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope

        self._bridge = CDPEventBridge(self._cdp, scope, session_id=session_id)
        self._bridge.bridge("Browser.downloadWillBegin", self._on_download_will_begin)
        self._bridge.bridge("Browser.downloadProgress", self._on_download_progress)

    def register_download_callbacks(
        self,
        *,
        on_start: Callable[[dict[str, Any]], None] | None = None,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Register direct callbacks (bypasses EventBus for low latency)."""
        if on_start:
            self._on_start_callbacks.append(on_start)
        if on_complete:
            self._on_complete_callbacks.append(on_complete)

    def _on_download_will_begin(self, params: dict[str, Any]) -> DownloadStartedEvent:
        guid = params.get("guid", "")
        info = {
            "guid": guid,
            "url": params.get("url", ""),
            "suggested_filename": params.get("suggestedFilename", ""),
        }
        self._active[guid] = info

        # Direct callbacks.
        for cb in self._on_start_callbacks:
            try:
                cb(info)
            except Exception:
                logger.exception("Download start callback error")

        return DownloadStartedEvent(**info)

    def _on_download_progress(self, params: dict[str, Any]) -> DownloadProgressEvent | FileDownloadedEvent:
        guid = params.get("guid", "")
        state = params.get("state", "")
        received = params.get("receivedBytes", 0)
        total = params.get("totalBytes", 0)

        if state == "completed":
            info = self._active.pop(guid, {})
            info.update({"file_size": total, "guid": guid})

            # Direct callbacks.
            for cb in self._on_complete_callbacks:
                try:
                    cb(info)
                except Exception:
                    logger.exception("Download complete callback error")

            return FileDownloadedEvent(
                guid=guid,
                url=info.get("url", ""),
                file_name=info.get("suggested_filename", ""),
                file_size=total,
            )

        return DownloadProgressEvent(
            guid=guid,
            received_bytes=received,
            total_bytes=total,
            state=state,
        )
