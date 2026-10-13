"""Main-frame navigation and load state per tab — what an observation waits for (§4.14).

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

:class:`MainFrameLoad` answers the next question the observation asks once
nothing is in flight — has the main frame's current document loaded? — from
the same feed's ``Page.lifecycleEvent``s (enabled per tab with
``Page.setLifecycleEventsEnabled``; Chrome reports the current document's
events again when they are enabled, which is how a tab adopted mid-load is
known). The settle wait reads it (``settle.py``).
"""

from __future__ import annotations

import asyncio
import time
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

    def gone(self) -> None:
        """The tab is gone: nothing of it is in flight any more, and nothing waits for it."""
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


class MainFrameLoad:
    """Whether one tab's main-frame document has loaded, from ``Page.lifecycleEvent``.

    Pure state, like :class:`MainFrameNavigation`. Chrome numbers each
    document by its loader id and reports each lifecycle event once per
    document; a new loader id (``init`` of a new document, or the ``commit``
    Chrome reports again for the current one when lifecycle events are
    enabled) starts a new document that has not loaded. A document is loaded
    once ``load`` arrives for its loader id. A document this tracker has heard
    nothing of is not :attr:`loading`: the wait never stalls on what it
    cannot know.

    Each document is waited on for at most one ``budget_s`` in total, across
    observations (:meth:`wait_loaded`): a document that has not loaded in that
    long (a resource that never finishes) is not waited on again.
    """

    def __init__(
        self, main_frame_id: str | None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.main_frame_id = main_frame_id
        self._clock = clock
        self._loader_id: str | None = None
        self._loaded = False
        #: When the current document was first heard of (``clock`` time).
        self._heard_at = 0.0
        #: Seconds the current document has been waited on, across observations.
        self._waited_s = 0.0
        self._changed = asyncio.Event()

    @property
    def loader_id(self) -> str | None:
        """The current document's loader id, ``None`` until one is heard of."""
        return self._loader_id

    @property
    def loading(self) -> bool:
        """The current document is known and has not reached ``load``."""
        return self._loader_id is not None and not self._loaded

    def lifecycle(self, frame_id: str | None, loader_id: str | None, name: str | None) -> None:
        """``Page.lifecycleEvent``: *name* reached by the document of *loader_id*."""
        if frame_id is None or frame_id != self.main_frame_id or not loader_id or not name:
            return
        if loader_id != self._loader_id:
            self._loader_id = loader_id
            self._loaded = False
            self._heard_at = self._clock()
            self._waited_s = 0.0
        if name == "load":
            self._loaded = True
        self._changed.set()

    async def wait_loaded(self, timeout: float, *, budget_s: float) -> bool:
        """Wait until the current document has loaded; ``False`` if *timeout* ended the wait first.

        Returns ``True`` at once, without suspending, when nothing is
        loading, or when earlier observations already waited on this document
        for *budget_s* in total. Otherwise waits at most *timeout* seconds,
        and at most what is left of the document's budget. A new document in
        the meantime is waited for in its place, on its own budget.
        """
        started = self._clock()
        try:
            while True:
                if not self.loading:
                    return True
                if self._waited_s >= budget_s:
                    # Waited on for a whole budget by earlier observations: a
                    # document that never finishes loading is not waited on again.
                    return True
                now = self._clock()
                on_this_document = now - max(started, self._heard_at)
                left = min(timeout - (now - started), budget_s - self._waited_s - on_this_document)
                if left <= 0:
                    return False
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), left)
                except TimeoutError:
                    continue
        finally:
            if self._loader_id is not None:
                self._waited_s += self._clock() - max(started, self._heard_at)

    def gone(self) -> None:
        """The tab is gone: nothing of it is loading any more."""
        self._loader_id = None
        self._loaded = False
        self._changed.set()


class NavigationFeed:
    """One tab session's ``Page`` events on the control connection → its trackers.

    The callbacks run in the connection's receive loop, in wire order, so the
    trackers are current before any reply read after their event. Events of
    another session are dropped; an event without a session id is accepted,
    as ``CDPEventBridge`` accepts it. ``Page.enable`` — sent when the tab is
    attached — is what makes these events fire, and
    ``Page.setLifecycleEventsEnabled`` the lifecycle ones (the capability
    enables them only after the feed exists, so the current document's
    events, which Chrome reports again then, reach :attr:`load`).
    :meth:`close` unregisters every callback and is idempotent.

    The feed also closes itself when its session detaches
    (``Target.detachedFromTarget`` naming it): a popup the page closes on its
    own never passes through ``close_tab``, and its callbacks would otherwise
    stay registered until teardown. The tracker is settled then, so nothing
    keeps waiting on a tab that is gone.
    """

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        session_id: str,
        navigation: MainFrameNavigation,
        load: MainFrameLoad | None = None,
    ) -> None:
        self.navigation = navigation
        self.load = load if load is not None else MainFrameLoad(navigation.main_frame_id)
        self._cdp = cdp
        self._session_id = session_id
        self._callbacks: list[tuple[str, Callable[..., None]]] = []
        handlers: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "Page.frameRequestedNavigation": self._on_requested,
            "Page.frameStartedNavigating": self._on_started,
            "Page.frameNavigated": self._on_navigated,
            "Page.frameStoppedLoading": self._on_stopped,
            "Page.lifecycleEvent": self._on_lifecycle,
            # Sent on the browser session, naming the detached session in its params.
            "Target.detachedFromTarget": self._on_detached,
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

    def _on_lifecycle(self, params: Mapping[str, Any]) -> None:
        self.load.lifecycle(params.get("frameId"), params.get("loaderId"), params.get("name"))

    def _on_detached(self, params: Mapping[str, Any]) -> None:
        if params.get("sessionId") != self._session_id:
            return
        self.close()
        self.navigation.gone()
        self.load.gone()
