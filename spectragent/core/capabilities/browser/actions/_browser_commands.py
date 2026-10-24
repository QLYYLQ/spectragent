"""The browser commands an action of their own and a browser shortcut both run.

``browser.go_back`` / ``go_forward`` / ``switch_tab`` run these, and so does
``browser.send_keys`` when its chord is the browser's own shortcut for the
command on the measured input platform (``input/shortcuts.py``,
``docs/architecture_design.md`` §4.13): one implementation of history
navigation, reloading and tab switching, whichever action asked. Each returns
the measured facts of the command's effect — the URL before and after and
whether the document loaded for a navigation, the tab before and after for a
tab command — and raises when the command could not run (no history entry, an
unknown tab, the only tab open).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spectragent.core.capabilities.browser.actions._common import focused_scope, handler_field
from spectragent.core.capabilities.browser.effect_probes import (
    UNKNOWN,
    ProbeBudget,
    document_loaded,
    tab_facts,
)
from spectragent.core.capabilities.browser.effects import TabFacts
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import (
    GoBackEvent,
    GoForwardEvent,
    RefreshEvent,
)
from spectragent.schemas.action import Unmeasured

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability
    from spectragent.core.capabilities.browser.tabs import TabSelection


async def history_step(capability: BrowserCapability, *, offset: int) -> Any:
    """Go to the adjacent history entry of the focused tab (``-1`` back, ``1`` forward).

    The tab's ``ActionWatchdog`` runs it (``_navigate_history``) and answers
    the ``NavigationEffect`` facts it measured.
    """
    event = GoBackEvent() if offset < 0 else GoForwardEvent()
    await dispatch_request(focused_scope(capability), event)
    return handler_field(event, "effect_facts")


async def reload_page(capability: BrowserCapability, *, ignore_cache: bool) -> Any:
    """Reload the focused tab's document (``ignore_cache``: a hard reload); its navigation facts."""
    event = RefreshEvent(ignore_cache=ignore_cache)
    await dispatch_request(focused_scope(capability), event)
    return handler_field(event, "effect_facts")


async def focused_tab(capability: BrowserCapability, budget: ProbeBudget) -> TabFacts | Unmeasured:
    """The focused tab and its URL; ``"unknown"`` when there is none to read."""
    try:
        cdp, session_id = capability.cdp, capability.default_session_id
    except RuntimeError:  # not set up: the command itself fails and reports no effect
        return UNKNOWN
    return await tab_facts(cdp, session_id, budget)


async def switch_to_tab(
    capability: BrowserCapability, target_id: str | None, budget: ProbeBudget
) -> tuple[TabSelection, dict[str, Any]]:
    """Move the focus to *target_id* (``None``: the most recently opened other tab).

    Returns the tab selected and the tab facts.

    :meth:`BrowserCapability.switch_tab` moves it: a watchdog is scoped to one
    tab and cannot repoint the capability's focus. The tab focused before is
    read off the focused session before switching, the one focused after is
    the tab ``switch_tab`` selected, each with its current URL, then whether
    the newly focused tab's document had finished loading — all within
    *budget*, through the capability's public control connection.
    """
    before = await focused_tab(capability, budget)
    selected = await capability.switch_tab(target_id)
    cdp = capability.cdp
    after = await tab_facts(cdp, selected.session_id, budget, tab_id=selected.target_id)
    loaded = await document_loaded(cdp, selected.session_id, budget)
    return selected, {"before": before, "after": after, "document_loaded": loaded}


async def close_focused_tab(capability: BrowserCapability, budget: ProbeBudget) -> dict[str, Any]:
    """Close the focused tab (:meth:`BrowserCapability.close_focused_tab`); its tab facts.

    ``before``: the tab closed, read before closing it; ``after``: the tab the
    focus moved to, and whether its document had finished loading.
    """
    before = await focused_tab(capability, budget)
    selected = await capability.close_focused_tab()
    cdp = capability.cdp
    after = await tab_facts(cdp, selected.session_id, budget, tab_id=selected.target_id)
    loaded = await document_loaded(cdp, selected.session_id, budget)
    return {"before": before, "after": after, "document_loaded": loaded}


__all__ = [
    "close_focused_tab",
    "focused_tab",
    "history_step",
    "reload_page",
    "switch_to_tab",
]
