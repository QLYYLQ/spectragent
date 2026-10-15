"""Tabs a page opens, and where the agent's focus goes (docs/architecture_design.md §4.14).

The focused tab — the one every action acts on and every observation
captures — is ``BrowserCapability`` state. ``browser.switch_tab`` moves it;
under ``new_tabs: follow`` (the default) the capability also moves it to a
tab the page opens during the agent's actions, the way a real browser brings
a new tab to the front, and it moves it off a focused tab that closed itself.
This module is the pure half of that: what the control connection's target
discovery says about page targets, in the order they opened
(:class:`TabRegistry`), and the two rules that read it —
:func:`tabs_to_follow` and :func:`tabs_after_close`. The capability feeds the
registry from CDP events and moves the focus; nothing here touches CDP.

What the registry relies on (measured, Chrome 155, headless; the probes are
in the local ``tmp/flywheel/followup/follow_tabs/``):

- ``Target.targetCreated`` names the opening tab in ``openerId`` for every
  way a page opens one: a ``target=_blank`` link with no ``rel``, with
  ``rel=noopener`` or ``rel=opener``, ``window.open`` with or without
  ``noopener``, a form with ``target=_blank``, a link inside a same-site or a
  cross-site iframe (``openerFrameId`` is then the frame). It arrives before
  the reply to the click that opened the tab.
- ``Page.windowOpen`` arrives on the opening tab's session about 10 ms
  before that, carrying ``userGesture``: true for a click, and still true
  for a window the click's handler opens 800 ms later or after a fetch it
  started; false for a window a page opens as it loads and for the second
  window one click opens. Those are the windows a real browser's popup
  blocker stops (af's Chrome runs with ``--disable-popup-blocking``, so they
  open). A link inside a cross-site iframe sends it on the iframe's own
  session, which the control connection does not listen on: no event.
- ``Target.getTargets`` lists tabs in no fixed order (a tab its opener
  cannot reach before its opener, a ``window.open`` tab after it), so the
  registry keeps the order they opened.
- A tab a download opened is destroyed about 75 ms after it was created; a
  tab that closes itself (``window.close()``) sends ``targetDestroyed``.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

#: How long a ``Page.windowOpen`` waits for the tab it opened (``targetCreated``
#: of the same opener). Measured about 10 ms; a window whose tab never comes —
#: none was seen — must not be matched to a later, unrelated one.
WINDOW_OPEN_MATCH_S = 2.0

#: Why the capability moved the focus before a capture.
FocusMoveReason = Literal["new_tab", "tab_closed"]


@dataclass(frozen=True)
class TabSelection:
    """The target and control session selected by one successful tab switch."""

    target_id: str
    session_id: str


class FocusMove(BaseModel):
    """The capability moved the agent's focus to another tab before this capture.

    ``reason``: ``new_tab`` — the tab the agent was on opened this one during
    the agent's actions since the previous observation; ``tab_closed`` — the
    tab the agent was on closed itself, and the focus went to the tab that
    opened it, or to the most recently opened tab still open. ``from_tab`` /
    ``to_tab`` are CDP target ids, the ``tab_id`` ``browser_meta`` lists: the
    tab focused when the observation began, and the one it captured. The
    agent's own ``browser.switch_tab`` is never recorded here: it is the
    step's action.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: FocusMoveReason
    from_tab: str
    to_tab: str


@dataclass
class PageTab:
    """One page target, as the control connection's target discovery reported it."""

    target_id: str
    #: Position in the order the tabs opened (0 = the first one discovery reported).
    order: int
    #: The tab that opened it (``openerId``), or ``None``.
    opener_id: str | None
    url: str
    #: Whether Chrome said the window was opened with the user's activation
    #: (``Page.windowOpen.userGesture``); ``None`` when no such event was seen.
    user_activation: bool | None = None
    closed: bool = False


@dataclass(frozen=True)
class FollowWindow:
    """The agent's actions since the last observation: when a tab they open is followed.

    ``focused``: the tab focused when the window opened (its target id) — the
    opener a followed tab must have. ``mark``: :attr:`TabRegistry.mark` then;
    tabs opened later belong to this window.
    """

    focused: str
    mark: int


def is_tab(info: Mapping[str, Any]) -> bool:
    """Whether a ``TargetInfo`` is a tab: a page target that is not a prerender or the like."""
    return info.get("type") == "page" and not info.get("subtype") and bool(info.get("targetId"))


class TabRegistry:
    """Page targets in the order they opened, fed from the control connection's events.

    Pure state: :meth:`created`, :meth:`changed` and :meth:`destroyed` take
    ``Target.targetCreated`` / ``targetInfoChanged`` / ``targetDestroyed``,
    :meth:`window_opened` takes ``Page.windowOpen`` (its opener resolved from
    the session it arrived on), and :meth:`reconcile` takes a
    ``Target.getTargets`` answer. Each ``Page.windowOpen`` waits up to
    :data:`WINDOW_OPEN_MATCH_S` for the next tab the same opener opens; a tab
    that finds none has ``user_activation`` ``None``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tabs: dict[str, PageTab] = {}
        self._next = 0
        #: opener id → (when, userGesture) of windows still waiting for their tab.
        self._opens: dict[str, deque[tuple[float, bool]]] = {}

    @property
    def mark(self) -> int:
        """The position the next tab to open will get."""
        return self._next

    def created(self, info: Mapping[str, Any]) -> None:
        """``Target.targetCreated``."""
        if not is_tab(info):
            return
        target_id = str(info["targetId"])
        if target_id in self._tabs:
            self.changed(info)
            return
        opener = info.get("openerId") or None
        tab = PageTab(
            target_id=target_id,
            order=self._next,
            opener_id=str(opener) if opener else None,
            url=str(info.get("url") or ""),
        )
        self._next += 1
        if tab.opener_id is not None:
            tab.user_activation = self._take_window_open(tab.opener_id)
        self._tabs[target_id] = tab

    def changed(self, info: Mapping[str, Any]) -> None:
        """``Target.targetInfoChanged``: a tab's URL."""
        tab = self._tabs.get(str(info.get("targetId") or ""))
        if tab is not None and info.get("url") is not None:
            tab.url = str(info["url"])

    def destroyed(self, target_id: str) -> None:
        """``Target.targetDestroyed``."""
        tab = self._tabs.get(target_id)
        if tab is not None:
            tab.closed = True

    def window_opened(self, opener_id: str, user_gesture: bool) -> None:
        """``Page.windowOpen`` on the session of tab *opener_id*."""
        self._opens.setdefault(opener_id, deque()).append((self._clock(), bool(user_gesture)))

    def reconcile(self, infos: Iterable[Mapping[str, Any]]) -> None:
        """A ``Target.getTargets`` answer: a tab it lists is open, one it leaves out is closed.

        A tab it lists that no event announced is added after every known one.
        """
        listed: set[str] = set()
        for info in infos:
            if not is_tab(info):
                continue
            listed.add(str(info["targetId"]))
            if str(info["targetId"]) not in self._tabs:
                self.created(info)
        for tab in self._tabs.values():
            if tab.target_id not in listed:
                tab.closed = True

    def get(self, target_id: str) -> PageTab | None:
        return self._tabs.get(target_id)

    def is_closed(self, target_id: str) -> bool:
        """Whether the tab is known and closed (an unknown tab is not known to be closed)."""
        tab = self._tabs.get(target_id)
        return tab is not None and tab.closed

    def open_tabs(self) -> list[PageTab]:
        """Every tab still open, in the order they opened."""
        return [tab for tab in self._ordered() if not tab.closed]

    def opened_since(self, mark: int) -> list[PageTab]:
        """Every tab that opened at or after *mark*, closed ones included, in order."""
        return [tab for tab in self._ordered() if tab.order >= mark]

    def in_opening_order(self, target_ids: Iterable[str]) -> list[str]:
        """*target_ids* sorted by when each opened; ids the registry does not know go last, as given."""
        ids = list(target_ids)
        known = sorted((i for i in ids if i in self._tabs), key=lambda i: self._tabs[i].order)
        return known + [i for i in ids if i not in self._tabs]

    def _ordered(self) -> list[PageTab]:
        return sorted(self._tabs.values(), key=lambda tab: tab.order)

    def _take_window_open(self, opener_id: str) -> bool | None:
        waiting = self._opens.get(opener_id)
        if not waiting:
            return None
        now = self._clock()
        while waiting and now - waiting[0][0] > WINDOW_OPEN_MATCH_S:
            waiting.popleft()  # its tab never came: never matched to a later one
        if not waiting:
            return None
        return waiting.popleft()[1]


def tabs_to_follow(
    registry: TabRegistry, window: FollowWindow, *, exclude: Iterable[str] = ()
) -> list[PageTab]:
    """The tabs the focus may follow after *window*'s actions, the one to follow first.

    A tab qualifies when the window's focused tab opened it during the window,
    it is still open, and Chrome did not report it opened without the user's
    activation (``user_activation`` ``False``: a window a page opened as it
    loaded or from a timer, or one click's second window — what a real
    browser's popup blocker stops). Unknown activation (a link inside a
    cross-site iframe) qualifies. Newest first: a real browser shows the last
    tab that came to the front; the rest are what to try when the newest
    cannot be adopted. *exclude* names tabs not to try again.
    """
    skip = set(exclude)
    candidates = [
        tab
        for tab in registry.opened_since(window.mark)
        if tab.opener_id == window.focused
        and not tab.closed
        and tab.user_activation is not False
        and tab.target_id not in skip
    ]
    return candidates[::-1]


def tabs_after_close(registry: TabRegistry, closed: str) -> list[PageTab]:
    """Where the focus goes when the focused tab *closed* itself, in the order to try.

    The tab that opened it, if it is still open, as a real browser does; then
    every other open tab, the most recently opened first.
    """
    tab = registry.get(closed)
    opener = registry.get(tab.opener_id) if tab is not None and tab.opener_id else None
    order: list[PageTab] = []
    if opener is not None and not opener.closed:
        order.append(opener)
    order += [t for t in reversed(registry.open_tabs()) if t not in order]
    return order


__all__ = [
    "WINDOW_OPEN_MATCH_S",
    "FocusMove",
    "FocusMoveReason",
    "FollowWindow",
    "PageTab",
    "TabRegistry",
    "is_tab",
    "tabs_after_close",
    "tabs_to_follow",
]
