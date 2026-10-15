"""SecurityWatchdog — domain allowlist/blocklist enforcement.

Listens to ``NavigateToUrlEvent`` at **high priority** (100) and consumes
the event + raises if the URL is blocked.  This prevents ActionWatchdog's
navigation handler from executing.

Uses agent-cdp's ``event.consume()`` + ``EmitPolicy.FAIL_FAST`` pattern.
"""

from __future__ import annotations

import logging
from typing import ClassVar, NoReturn
from urllib.parse import urlparse

from agent_cdp import ConnectionType, EventScope, Participation

from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    NavigateToUrlEvent,
)

logger = logging.getLogger(__name__)


class SecurityWatchdog(BrowserWatchdog):
    """Enforces domain allowlist/blocklist on navigation events."""

    level: ClassVar[WatchdogLevel] = "tab"

    def __init__(
        self,
        *,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ) -> None:
        self._allowed: set[str] | None = set(allowed_domains) if allowed_domains else None
        self._blocked: set[str] = set(blocked_domains) if blocked_domains else set()
        self._scope: EventScope | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> SecurityWatchdog:
        return cls(
            allowed_domains=ctx.profile.security_allowed_domains,
            blocked_domains=ctx.profile.security_blocked_domains,
        )

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        # High priority — runs before ActionWatchdog's navigation handler.
        connection = scope.connect(
            NavigateToUrlEvent,
            self._check_navigation,
            mode=ConnectionType.DIRECT,
            priority=100,
            participation=Participation.OBSERVER,
        )

        self.track_binding(scope, session_id, connections=[connection])

    def _check_navigation(self, event: NavigateToUrlEvent) -> None:
        """Block a navigation to a URL with no scheme, an unsafe scheme or a disallowed domain.

        Each has its own message: a URL with no scheme (``www.example.com``)
        is told it is missing one, not that it is unsafe. Consumes the event
        and raises ValueError if blocked, which prevents lower-priority
        handlers from executing (FAIL_FAST).
        """
        url_check = self._check_url(event.url)
        if url_check == "skip":
            return  # Safe non-HTTP scheme (about:, blob:)
        if url_check == "missing_scheme":
            self._block(
                event,
                f"Navigation BLOCKED: missing scheme in {event.url} (a page to open is a "
                f"complete URL starting with http:// or https://)",
            )
        if url_check == "block_scheme":
            self._block(event, f"Navigation BLOCKED: unsafe scheme in {event.url}")
        domain = url_check

        if self._is_blocked(domain):
            self._block(
                event, f"Navigation BLOCKED by SecurityWatchdog: {event.url} (domain: {domain})"
            )

    def _block(self, event: NavigateToUrlEvent, msg: str) -> NoReturn:
        """Consume *event*, report *msg* and raise it — lower-priority handlers never run."""
        event.consume()
        logger.warning(msg)
        if self._scope:
            self._scope.emit(
                BrowserErrorEvent(
                    error_type="security_blocked",
                    message=msg,
                )
            )
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
            ``"missing_scheme"`` — no scheme at all (``www.example.com``): not
            a URL that can be opened, and not a dangerous one either.
            ``"block_scheme"`` — dangerous scheme, must block.
            Otherwise — the domain string to check against allow/block lists.
        """
        try:
            parsed = urlparse(url)
            scheme = (parsed.scheme or "").lower()
            if not scheme:
                return "missing_scheme"
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
