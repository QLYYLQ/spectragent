"""SecurityWatchdog — domain allowlist/blocklist enforcement.

Listens to ``NavigateToUrlEvent`` at **high priority** (100) and consumes
the event + raises if the URL is blocked.  This prevents ActionWatchdog's
navigation handler from executing.

Uses agent-cdp's ``event.consume()`` + ``EmitPolicy.FAIL_FAST`` pattern.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from agent_cdp import ConnectionType, EventScope, WatchdogBase

from spectragent.core.browser.watchdogs.events import (
    BrowserErrorEvent,
    NavigateToUrlEvent,
)

logger = logging.getLogger(__name__)


class SecurityWatchdog(WatchdogBase):
    """Enforces domain allowlist/blocklist on navigation events."""

    def __init__(
        self,
        *,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ) -> None:
        self._allowed: set[str] | None = set(allowed_domains) if allowed_domains else None
        self._blocked: set[str] = set(blocked_domains) if blocked_domains else set()
        self._scope: EventScope | None = None

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        # High priority — runs before ActionWatchdog's navigation handler.
        scope.connect(
            NavigateToUrlEvent,
            self._check_navigation,
            mode=ConnectionType.DIRECT,
            priority=100,
        )

    def _check_navigation(self, event: NavigateToUrlEvent) -> None:
        """Block navigation to disallowed domains.

        Consumes the event and raises ValueError if blocked, which
        prevents lower-priority handlers from executing (FAIL_FAST).
        """
        url_check = self._check_url(event.url)
        if url_check == "skip":
            return  # Safe non-HTTP scheme (about:, blob:)
        if url_check == "block_scheme":
            event.consume()
            msg = f"Navigation BLOCKED: unsafe scheme in {event.url}"
            logger.warning(msg)
            if self._scope:
                self._scope.emit(BrowserErrorEvent(
                    error_type="security_blocked", message=msg,
                ))
            raise ValueError(msg)
        domain = url_check

        if self._is_blocked(domain):
            event.consume()
            msg = f"Navigation BLOCKED by SecurityWatchdog: {event.url} (domain: {domain})"
            logger.warning(msg)
            if self._scope:
                self._scope.emit(BrowserErrorEvent(
                    error_type="security_blocked",
                    message=msg,
                ))
            raise ValueError(msg)

    def _is_blocked(self, domain: str) -> bool:
        """Check if a domain is blocked."""
        # If allowlist is set, only those domains are permitted.
        if self._allowed is not None:
            return not self._matches_any(domain, self._allowed)
        # Otherwise, check blocklist.
        return self._matches_any(domain, self._blocked)

    @staticmethod
    def _matches_any(domain: str, domain_set: set[str]) -> bool:
        """Check if domain matches any entry (supports wildcards like .example.com)."""
        for pattern in domain_set:
            if domain == pattern:
                return True
            if pattern.startswith(".") and domain.endswith(pattern):
                return True
            # Allow subdomain matching: pattern "example.com" matches "sub.example.com"
            if domain.endswith("." + pattern):
                return True
        return False

    # Schemes that are safe to navigate to without domain checking.
    _SAFE_SCHEMES: frozenset[str] = frozenset({"about", "blob"})
    # Schemes that are always blocked (code execution / local file access).
    _BLOCKED_SCHEMES: frozenset[str] = frozenset({"javascript", "file", "data", "vbscript"})

    @classmethod
    def _check_url(cls, url: str) -> str:
        """Classify a URL for security.

        Returns:
            ``"skip"`` — safe non-HTTP scheme, no domain check needed.
            ``"block_scheme"`` — dangerous scheme, must block.
            Otherwise — the domain string to check against allow/block lists.
        """
        try:
            parsed = urlparse(url)
            scheme = (parsed.scheme or "").lower()
            if scheme in cls._SAFE_SCHEMES:
                return "skip"
            if scheme in cls._BLOCKED_SCHEMES:
                return "block_scheme"
            if scheme in ("http", "https"):
                return parsed.hostname or ""
            # Unknown scheme — block by default.
            return "block_scheme"
        except Exception:
            return "block_scheme"
