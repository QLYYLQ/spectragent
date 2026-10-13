"""SecurityWatchdog: a URL with no scheme is told it is missing one (§4.10 "起始页").

A LexBench start page written as ``www.bilibili.com`` used to be reported as
"unsafe scheme", which sent the reader looking for a dangerous URL. A URL with
no scheme now says the scheme is missing; a disallowed scheme keeps its own
message, and so does a domain outside the allow list.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_cdp import ScopeGroup

from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    NavigateToUrlEvent,
)
from spectragent.core.capabilities.browser.watchdogs.security_watchdog import SecurityWatchdog


class _Scope:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


def _blocked(url: str, **lists: Any) -> tuple[str, NavigateToUrlEvent, _Scope]:
    watchdog = SecurityWatchdog(**lists)
    scope = _Scope()
    watchdog._scope = scope  # type: ignore[assignment]
    event = NavigateToUrlEvent(url=url)
    with pytest.raises(ValueError) as info:
        watchdog._check_navigation(event)
    return str(info.value), event, scope


def test_a_url_with_no_scheme_is_told_the_scheme_is_missing() -> None:
    message, event, scope = _blocked("www.bilibili.com")
    assert message == (
        "Navigation BLOCKED: missing scheme in www.bilibili.com (a page to open is a complete "
        "URL starting with http:// or https://)"
    )
    assert event.consumed, "the navigation handler never runs"
    (error,) = scope.emitted
    assert isinstance(error, BrowserErrorEvent)
    assert (error.error_type, error.message) == ("security_blocked", message)


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://a.example/"]
)
def test_a_disallowed_scheme_keeps_its_own_message(url: str) -> None:
    message, _event, _scope = _blocked(url)
    assert message == f"Navigation BLOCKED: unsafe scheme in {url}"


def test_a_domain_outside_the_allow_list_keeps_its_own_message() -> None:
    message, _event, _scope = _blocked("https://www.bilibili.com/", allowed_domains=["example.com"])
    assert message == (
        "Navigation BLOCKED by SecurityWatchdog: https://www.bilibili.com/ "
        "(domain: www.bilibili.com)"
    )


@pytest.mark.parametrize("url", ["https://www.bilibili.com/", "http://127.0.0.1:8/", "about:blank"])
def test_an_allowed_url_passes(url: str) -> None:
    watchdog = SecurityWatchdog()
    event = NavigateToUrlEvent(url=url)
    watchdog._check_navigation(event)
    assert not event.consumed


async def test_through_a_scope_the_emit_itself_raises_it() -> None:
    """``NavigateAction`` turns this raise into its failed result."""
    group = ScopeGroup("test-security")
    scope = await group.create_scope("tab")
    SecurityWatchdog().attach(scope, session_id="sid")
    try:
        with pytest.raises(ValueError, match="missing scheme in www.bilibili.com"):
            scope.emit(NavigateToUrlEvent(url="www.bilibili.com"))
    finally:
        await group.close_all()
