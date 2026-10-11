"""Main-frame navigation state per tab — what an observation waits for (§4.14, M33).

M31 found, and M33 measured (Chrome 155), that clicking a form's submit
button stalled the next observation for 30 s. The click returns before the
navigation it starts: the reply to the last ``Input.dispatchMouseEvent``
arrives about a millisecond *before* ``Page.frameRequestedNavigation``. The
observation that follows sent ``Page.captureScreenshot`` between the request
and the commit, and under pinned's ``--disable-back-forward-cache`` Chrome
refuses a capture sent there or never answers it.

:class:`MainFrameNavigation` answers one question for one tab: is its main
frame between a cross-document navigation's request and its end? It is pure
state. :class:`NavigationFeed` keeps it current from the control
connection's ``Page`` events for that tab's session, and
``BrowserCapability._capture_once`` waits on it before each capture attempt —
not at all when nothing is in flight.

What each interaction sends on the tab's session (measured on raw CDP,
Chrome 155 with pinned's ``--disable-back-forward-cache``):

- form submit (GET or POST), a link, ``location.href = …``:
  ``frameRequestedNavigation`` → ``frameStartedNavigating``
  (``differentDocument``) → ``frameNavigated`` → ``frameStoppedLoading``;
  settled at the commit;
- Enter in a text field: the same, the request arriving before the key's
  last reply — the action returns with the navigation already in flight;
- a 204 or a download response, a ``mailto:`` link: request → start →
  ``frameStoppedLoading`` and no commit; settled when loading stops;
- an unreachable host: the error page commits (``chrome-error://``), then
  Chrome reloads it once on its own (start → stop, no commit);
- a fragment link (``href="#"`` too), ``history.pushState``:
  ``navigatedWithinDocument`` and no request — never in flight;
- a ``javascript:`` link, a submit its handler prevented, ``target=_blank``
  (link or form), ``<a download>``: no request on this session — never in
  flight.

A navigation that starts *after* the observation checked (a page timer) is
not covered here; the observation screenshot's own command bound covers it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_cdp import CDPCommandProtocol

#: ``Page.frameStartedNavigating`` types that keep the document: nothing to wait for.
SAME_DOCUMENT_NAVIGATIONS: frozenset[str] = frozenset({"sameDocument", "historySameDocument"})


class MainFrameNavigation:
    """Whether one tab's main frame has a cross-document navigation in flight.

    In flight from the request (:meth:`requested`) or the start
    (:meth:`started`, cross-document types only) until the commit
    (:meth:`committed`) — or until the frame stops loading without one
    (:meth:`stopped`: a 204, a download, a navigation the browser dropped).
    Every other frame id is ignored: a sub-frame's navigation does not
    replace the document a screenshot shows. With no known main frame id
    nothing counts, and an observation behaves as it did before M33.
    """

    def __init__(self, main_frame_id: str | None) -> None:
        self.main_frame_id = main_frame_id
        self._settled = asyncio.Event()
        self._settled.set()

    @property
    def in_flight(self) -> bool:
        """A main-frame navigation was requested or started and has not ended."""
        return not self._settled.is_set()

    def requested(self, frame_id: str | None) -> None:
        """``Page.frameRequestedNavigation`` (this tab, current tab disposition)."""
        if self._is_main(frame_id):
            self._settled.clear()

    def started(self, frame_id: str | None, navigation_type: str | None) -> None:
        """``Page.frameStartedNavigating``; a same-document type is ignored."""
        if self._is_main(frame_id) and navigation_type not in SAME_DOCUMENT_NAVIGATIONS:
            self._settled.clear()

    def committed(self, frame_id: str | None) -> None:
        """``Page.frameNavigated``: the new document is the frame's."""
        if self._is_main(frame_id):
            self._settled.set()

    def stopped(self, frame_id: str | None) -> None:
        """``Page.frameStoppedLoading``: whatever was in flight ended, committed or not."""
        if self._is_main(frame_id):
            self._settled.set()

    async def wait_settled(self, timeout: float) -> bool:
        """Wait until nothing is in flight, for at most *timeout* seconds.

        Returns ``True`` at once, without suspending, when nothing is in
        flight — an observation that follows no navigation costs nothing.
        ``False`` means the bound was hit with the navigation still in flight;
        the caller decides what to do then.
        """
        if self._settled.is_set():
            return True
        try:
            await asyncio.wait_for(self._settled.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def _is_main(self, frame_id: str | None) -> bool:
        return frame_id is not None and frame_id == self.main_frame_id


class NavigationFeed:
    """One tab session's ``Page`` events on the control connection → its tracker.

    The callbacks run in the connection's receive loop, in wire order, so the
    tracker is current before any reply read after its event. Events of
    another session are dropped; an event without a session id is accepted,
    as ``CDPEventBridge`` accepts it. ``Page.enable`` — sent when the tab is
    attached — is what makes these events fire. :meth:`close` unregisters
    every callback and is idempotent.
    """

    def __init__(
        self, cdp: CDPCommandProtocol, session_id: str, navigation: MainFrameNavigation
    ) -> None:
        self.navigation = navigation
        self._cdp = cdp
        self._session_id = session_id
        self._callbacks: list[tuple[str, Callable[..., None]]] = []
        handlers: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "Page.frameRequestedNavigation": self._on_requested,
            "Page.frameStartedNavigating": self._on_started,
            "Page.frameNavigated": self._on_navigated,
            "Page.frameStoppedLoading": self._on_stopped,
        }
        for method, handler in handlers.items():
            callback = self._for_this_session(handler)
            cdp.on_event(method, callback)
            self._callbacks.append((method, callback))

    def close(self) -> None:
        """Unregister every callback this feed registered."""
        callbacks, self._callbacks = self._callbacks, []
        for method, callback in callbacks:
            self._cdp.off_event(method, callback)

    def _for_this_session(
        self, handler: Callable[[Mapping[str, Any]], None]
    ) -> Callable[..., None]:
        def callback(params: Mapping[str, Any], session_id: str | None = None) -> None:
            incoming = session_id or params.get("sessionId")
            if incoming is not None and incoming != self._session_id:
                return
            handler(params)

        return callback

    def _on_requested(self, params: Mapping[str, Any]) -> None:
        # A request that opens another tab or window, or downloads, leaves this
        # frame's document where it is.
        if params.get("disposition", "currentTab") == "currentTab":
            self.navigation.requested(params.get("frameId"))

    def _on_started(self, params: Mapping[str, Any]) -> None:
        self.navigation.started(params.get("frameId"), params.get("navigationType"))

    def _on_navigated(self, params: Mapping[str, Any]) -> None:
        self.navigation.committed((params.get("frame") or {}).get("id"))

    def _on_stopped(self, params: Mapping[str, Any]) -> None:
        self.navigation.stopped(params.get("frameId"))
