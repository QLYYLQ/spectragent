"""Stage 1 of the DOM pipeline: CDP capture across every target of a tab.

A tab's main document, its same-process iframes and its shadow roots come
back from one ``DOM.getDocument(pierce=True)`` on the tab's session. An
out-of-process iframe (a cross-site frame under site isolation) does not:
the tab's DOM shows the ``<iframe>`` element with a ``frameId`` and no
``contentDocument``, and the frame's document is only reachable through a
CDP session attached to the frame's own target.

:class:`FrameTargets` keeps those sessions. It turns on
``Target.setAutoAttach`` (flattened, iframes only) for a session the first
time that session is captured, and tracks the ``Target.attachedToTarget`` /
``Target.detachedFromTarget`` events Chrome sends for it from then on —
including for frames that already exist, which Chrome attaches before it
answers ``setAutoAttach``. Nested out-of-process frames are reached the same
way one level down.

:func:`capture_target` produces a :class:`TargetCapture` for one session and,
recursively, for every frame target attached below it. Grafting a frame's
document under its ``<iframe>`` is the cleaner's job; capture only records
which frame each child capture belongs to.

A frame target is another renderer process, often a third party's, and CDP
commands to it wait for that renderer. So frames are captured against one
deadline per observation (:data:`FRAME_CAPTURE_TIMEOUT_S`): a frame that has
not answered by then is left out and its ``<iframe>`` stays opaque, while
the tab's own documents are never cut short. And nothing is evaluated in a
frame's page world: the optional JS click-listener scan runs in the tab's
main document only (see :func:`capture_target`).

Why this is not a watchdog: nothing here resolves a fragment or reacts to a
scope event. It is the DOM resolver's plumbing, owned by ``DOMWatchdog`` the
way ``HumanMouse`` is owned by ``ActionWatchdog``. Its CDP event callbacks
only update dictionaries — a callback that awaited a CDP command would
deadlock the client, which dispatches events on the task that reads replies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, TypeVar

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.dom.js import event_listeners
from spectragent.core.capabilities.browser.dom.models import TargetCapture

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: How many out-of-process frames deep capture follows (browser-use uses the
#: same bound). It exists so a frame that embeds itself cannot turn one
#: observation into unbounded CDP traffic; a deeper frame stays opaque.
MAX_FRAME_DEPTH = 5

#: Seconds one observation waits for its out-of-process frames, all of them
#: together, counted from the start of the capture. A busy third-party
#: renderer (an ad spinning its main thread) answers nothing until it is
#: done, and ``CDPClient.send`` has no time limit of its own — without this,
#: one such frame costs the agent the whole DOM observation. Healthy frames
#: answer in tens of milliseconds.
FRAME_CAPTURE_TIMEOUT_S = 2.0

#: ``Target.setAutoAttach`` parameters for frame discovery. ``flatten`` puts
#: every child session on the one browser WebSocket; ``waitForDebuggerOnStart``
#: is off because nothing needs to run before the frame does; the filter keeps
#: workers and other related targets out.
_AUTO_ATTACH_PARAMS: dict[str, Any] = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
    "filter": [{"type": "iframe"}],
}


@dataclass(frozen=True, slots=True)
class FrameTarget:
    """One out-of-process frame attached below a session."""

    #: The frame's target id — equal to the ``frameId`` of its ``<iframe>``.
    target_id: str
    session_id: str
    #: The frame's handle namespace (see ``dom.models.element_handle``).
    namespace: int


class FrameTargets:
    """The out-of-process frames of one tab, as CDP sessions.

    Namespaces are assigned per frame target id the first time the frame is
    seen and never reused within the tab, so an element's selector-map key
    survives observations — and a frame that swaps renderer processes keeps
    its id, so it keeps its namespace too.
    """

    def __init__(self, cdp: CDPCommandProtocol) -> None:
        self._cdp = cdp
        self._root: str | None = None
        #: child session id → the frame it serves.
        self._frames: dict[str, FrameTarget] = {}
        #: child session id → the session it was attached under.
        self._parents: dict[str, str] = {}
        #: frame target id → namespace. Grows only.
        self._namespaces: dict[str, int] = {}
        #: sessions ``Target.setAutoAttach`` has been accepted on.
        self._auto_attached: set[str] = set()

    # ── lifecycle ─────────────────────────────────────────────────────

    def watch(self, root_session_id: str) -> None:
        """Start tracking frames below the tab session ``root_session_id``."""
        if self._root is not None:
            return
        self._root = root_session_id
        self._cdp.on_event("Target.attachedToTarget", self._on_attached)
        self._cdp.on_event("Target.detachedFromTarget", self._on_detached)

    def close(self) -> None:
        """Stop tracking and forget every session. Idempotent."""
        if self._root is None:
            return
        self._cdp.off_event("Target.attachedToTarget", self._on_attached)
        self._cdp.off_event("Target.detachedFromTarget", self._on_detached)
        self._root = None
        self._frames.clear()
        self._parents.clear()
        self._auto_attached.clear()

    # ── queries ───────────────────────────────────────────────────────

    async def children(self, session_id: str) -> list[FrameTarget]:
        """Frame targets attached directly below ``session_id``.

        The first call for a session enables auto-attach on it. Chrome sends
        ``attachedToTarget`` for the frames that already exist before it
        replies, so the answer is complete on that first call too. A refused
        ``setAutoAttach`` leaves the session without child frames for this
        observation and is retried on the next one.
        """
        if session_id != self._root and session_id not in self._frames:
            return []
        if session_id not in self._auto_attached:
            try:
                await self._cdp.send(
                    "Target.setAutoAttach", dict(_AUTO_ATTACH_PARAMS), session_id=session_id
                )
            except Exception as exc:
                logger.warning(
                    "Target.setAutoAttach failed on session %s: %s — its "
                    "out-of-process iframes stay opaque this observation",
                    session_id,
                    exc,
                )
                return []
            self._auto_attached.add(session_id)
        return [
            frame for child, frame in self._frames.items() if self._parents.get(child) == session_id
        ]

    # ── CDP events ────────────────────────────────────────────────────

    def _on_attached(self, params: dict[str, Any], parent_session_id: str | None) -> None:
        if parent_session_id is None:
            return  # a tab attached at browser level — not a frame of ours
        if parent_session_id != self._root and parent_session_id not in self._frames:
            return  # another tab's frame
        info = params.get("targetInfo") or {}
        child = params.get("sessionId")
        target_id = info.get("targetId")
        if info.get("type") != "iframe" or not child or not target_id:
            return
        namespace = self._namespaces.get(target_id)
        if namespace is None:
            namespace = len(self._namespaces) + 1
            self._namespaces[target_id] = namespace
        self._frames[child] = FrameTarget(
            target_id=target_id, session_id=child, namespace=namespace
        )
        self._parents[child] = parent_session_id

    def _on_detached(self, params: dict[str, Any], _parent_session_id: str | None) -> None:
        child = params.get("sessionId")
        if not child or child not in self._frames:
            return
        # Chrome detaches nested frames before their parent, but a parent
        # going first must not strand its subtree in the table.
        doomed = [child]
        while doomed:
            session = doomed.pop()
            self._frames.pop(session, None)
            self._parents.pop(session, None)
            self._auto_attached.discard(session)
            doomed.extend(s for s, parent in self._parents.items() if parent == session)


async def capture_target(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    *,
    frames: FrameTargets | None,
    js_click_listeners: bool,
    required_computed_styles: list[str],
    frame_timeout_s: float = FRAME_CAPTURE_TIMEOUT_S,
) -> TargetCapture:
    """Capture the tab's documents and, recursively, its frame targets.

    Every CDP call is best-effort, as it always was for the tab: a failed
    call contributes an empty payload instead of failing the observation. A
    frame target that fails outright — it detached mid-capture, say — or has
    not answered within ``frame_timeout_s`` of the start of this call is left
    out, and its ``<iframe>`` stays opaque for this observation. The limit is
    one deadline shared by every frame, nested ones included, so a page
    with many slow frames costs the same bounded wait as a page with one.

    ``frames`` is ``None`` when frame discovery is off or not possible (no
    tab session): auto-attach must never be sent to the browser-level
    session.

    ``js_click_listeners`` scans the tab's main document only. The scan
    evaluates script in the page's own world, where page code can watch it
    run; inside a third party's frame — a captcha, an anti-bot vendor, a
    payment field — that is a signal no run should send, and those frames'
    elements are found by tag, role, attributes and accessibility instead.
    """
    deadline = asyncio.get_running_loop().time() + frame_timeout_s
    capture = await _capture_target(
        cdp,
        session_id,
        frames=frames,
        js_click_listeners=js_click_listeners,
        required_computed_styles=required_computed_styles,
        deadline=deadline,
        frame=None,
        depth=0,
    )
    assert capture is not None  # only a frame target is ever left out
    return capture


async def _capture_target(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    *,
    frames: FrameTargets | None,
    js_click_listeners: bool,
    required_computed_styles: list[str],
    deadline: float,
    frame: FrameTarget | None,
    depth: int,
) -> TargetCapture | None:
    """One target (the tab when ``frame`` is ``None``) and the frames below it.

    ``None`` when ``frame``'s own documents miss the deadline: without its
    document a frame's children have nowhere to hang, so they go with it.
    The tab's documents are awaited in full whatever the deadline says.
    """
    child_targets: list[FrameTarget] = []
    if frames is not None and session_id is not None and depth < MAX_FRAME_DEPTH:
        found = await _by_deadline(deadline, frames.children(session_id))
        if found is None:
            logger.warning(
                "frame discovery on session %s did not answer in time — its "
                "out-of-process iframes stay opaque this observation",
                session_id,
            )
        child_targets = found or []

    async def child_capture(child: FrameTarget) -> TargetCapture | None:
        try:
            return await _capture_target(
                cdp,
                child.session_id,
                frames=frames,
                js_click_listeners=False,
                required_computed_styles=required_computed_styles,
                deadline=deadline,
                frame=child,
                depth=depth + 1,
            )
        except Exception as exc:
            logger.debug("capture of frame target %s failed: %s", child.target_id, exc)
            return None

    own = _capture_documents(cdp, session_id, js_click_listeners, required_computed_styles)
    documents, children = await asyncio.gather(
        own if frame is None else _by_deadline(deadline, own),
        asyncio.gather(*(child_capture(child) for child in child_targets)),
    )
    if documents is None:
        assert frame is not None
        logger.warning(
            "frame target %s did not answer in time — its iframe stays opaque this observation",
            frame.target_id,
        )
        return None
    raw_dom, snapshot, (ax_tree, root_frame), js_ids = documents
    return TargetCapture(
        raw_dom=raw_dom,
        snapshot=snapshot,
        ax_tree=ax_tree,
        session_id=session_id,
        target_id=frame.target_id if frame is not None else None,
        namespace=frame.namespace if frame is not None else 0,
        js_click_listener_ids=js_ids,
        frames={
            child.target_id: capture
            for child, capture in zip(child_targets, children, strict=True)
            if capture is not None
        },
        loader_id=root_frame.get("loaderId"),
        url=root_frame.get("url"),
    )


async def _by_deadline(deadline: float, awaitable: Awaitable[_T]) -> _T | None:
    """``awaitable``'s result, or ``None`` if it is not ready by ``deadline``.

    On timeout the awaitable is cancelled; a CDP reply that arrives later is
    dropped by the client, which ignores replies to cancelled requests.
    """
    remaining = deadline - asyncio.get_running_loop().time()
    try:
        return await asyncio.wait_for(awaitable, timeout=max(remaining, 0.0))
    except TimeoutError:
        return None


async def _capture_documents(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    js_click_listeners: bool,
    required_computed_styles: list[str],
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], dict[str, Any]], set[int] | None]:
    """The three protocol captures (+ optional listener scan) for one session.

    The accessibility capture also hands back the session's root frame
    (``Page.getFrameTree``), whose ``loaderId`` / ``url`` stamp the capture.
    """

    async def get_document() -> dict[str, Any]:
        try:
            result = await cdp.send(
                "DOM.getDocument", {"depth": -1, "pierce": True}, session_id=session_id
            )
            return result or {}
        except Exception as exc:
            logger.warning("DOM.getDocument failed on %s: %s", session_id, exc)
            return {}

    async def get_snapshot() -> dict[str, Any]:
        try:
            result = await cdp.send(
                "DOMSnapshot.captureSnapshot",
                {
                    "computedStyles": list(required_computed_styles),
                    "includeDOMRects": True,
                    "includePaintOrder": True,
                },
                session_id=session_id,
            )
            return result or {}
        except Exception as exc:
            logger.warning("DOMSnapshot.captureSnapshot failed on %s: %s", session_id, exc)
            return {}

    async def get_js_listeners() -> set[int] | None:
        if not js_click_listeners:
            return None
        try:
            # Looked up on the module at call time, so a test can patch it.
            return await event_listeners.detect_js_click_listeners(cdp.send, session_id=session_id)
        except Exception as exc:
            logger.warning("JS listener detection failed on %s: %s", session_id, exc)
            return None

    raw_dom, snapshot, ax_tree, js_ids = await asyncio.gather(
        get_document(), get_snapshot(), _capture_ax_tree(cdp, session_id), get_js_listeners()
    )
    return raw_dom, snapshot, ax_tree, js_ids


async def _capture_ax_tree(
    cdp: CDPCommandProtocol, session_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accessibility nodes of every frame this session renders, merged.

    ``Accessibility.getFullAXTree`` without a ``frameId`` answers for the
    main frame only, so a same-process iframe's roles, names and states were
    missing even from the elements that did reach the tree. The session's
    frame tree lists exactly the frames it can answer for.

    Returns the nodes and the frame tree's root frame (``{}`` when the tree
    could not be read): its ``loaderId`` and ``url`` are the document stamp
    of this capture.
    """

    async def full_tree(params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            result = await cdp.send("Accessibility.getFullAXTree", params, session_id=session_id)
            return list((result or {}).get("nodes", []))
        except Exception as exc:
            logger.warning(
                "Accessibility.getFullAXTree%s failed on %s: %s", params, session_id, exc
            )
            return []

    try:
        frame_tree = await cdp.send("Page.getFrameTree", {}, session_id=session_id)
    except Exception as exc:
        logger.debug("Page.getFrameTree failed on %s: %s", session_id, exc)
        frame_tree = None
    tree = (frame_tree or {}).get("frameTree")
    root_frame = dict((tree or {}).get("frame") or {})
    frame_ids = _frame_ids(tree)
    if not frame_ids:
        return {"nodes": await full_tree({})}, root_frame
    per_frame = await asyncio.gather(*(full_tree({"frameId": fid}) for fid in frame_ids))
    return {"nodes": [node for nodes in per_frame for node in nodes]}, root_frame


def _frame_ids(tree: dict[str, Any] | None) -> list[str]:
    """Frame ids of a ``Page.FrameTree``, root first."""
    ids: list[str] = []
    stack = [tree] if tree else []
    while stack:
        node = stack.pop(0)
        frame_id = (node.get("frame") or {}).get("id")
        if frame_id:
            ids.append(frame_id)
        stack.extend(node.get("childFrames") or [])
    return ids
