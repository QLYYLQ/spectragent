"""Media playback state of a browser screenshot, and aligning it during restoration.

``docs/architecture_design.md`` §4.4 "截图的媒体状态" and §4.14 "恢复：重定位与校验".
A video keeps playing in real time, while the model thinks too; a same-path
replay does not wait for the model, so the same step's screenshot shows the
same player at another position (task 30 of the Xiaohongshu runs: the source
at 00:41, the replay at 00:10) and the screenshot hash may still pass. So the
playback state of every media element is measured with the screenshot,
compared during restoration, and aligned to the recording where it can be.

**Measuring** (:func:`read_media`, :func:`media_state`). The screenshot
resolver reads every ``<video>`` / ``<audio>`` element just before and just
after ``Page.captureScreenshot``. Every frame is read in its own isolated
world :data:`MEDIA_WORLD` (one per document, reused by name; page scripts
neither see nor disturb it), on the session that serves it: the frames the
tab renders itself on the tab's session, each out-of-process (cross-site)
frame on that frame's own session (``FrameTargets``, the capability's per-tab
discovery). Each frame names itself by its path from the top — ``main``, or
the index of each frame in its parent's frame list (``main/1``), which a
cross-origin parent exposes too — and reports its own media elements (open
shadow roots included) in document order and where its child frames sit. A
frame inside a shadow root is not in its parent's frame list, so it cannot
name itself: its parent lists it as ``<parent>/unplaced-<k>``, which no read
claims, so it is unreachable (its media unknown, never "no media").
Each element records its frame, its order among that frame's media elements,
its box in the top viewport, its source kind (``url`` / ``blob`` / ``stream``
/ ``none`` / ``other``), the URL of a ``url`` source, its duration and whether
it is live (an infinite duration or a stream), and a :class:`MediaReading`
from each read: the exact ``currentTime``, ``paused``, ``seeking``,
``readyState`` and ``ended``. A child frame that answered neither of the two
reads is listed as unreachable — the restoration check then calls the step
unrestorable. A main-frame read that runs out of ``media_state_timeout_s``,
fails or answers with something else is ``unknown`` — never a reason for the
screenshot to fail.

**Matching** (:func:`match_media`). Across two observations an element
corresponds to the candidates in the same frame whose box lies within the
retarget tolerance, of the same source kind, and — for two ``url`` sources —
with an equal URL and an equal duration when both are known (a ``blob:`` URL
changes on every load, and a script-fed source's duration changes as data
arrives). Exactly one candidate at the same document order is
``matched``; none is ``missing``; several, or one at another order, is
``ambiguous``. A restored element nothing corresponds to is ``extra``.

**Aligning** (:func:`align_media`). Before a restored observation's
screenshot, after the settle wait, each matched element is moved to the
recorded position and play state: paused targets first (pause, seek, wait for
``seeked``, ``seeking == false`` and ``readyState >= 2``, and the position it
landed on must be the target), then playing ones
(the same, then ``play()`` and wait for ``playing``), and the capture follows
at once, its screenshot before any other fragment. A recorded element that had ended is sought a short lead before its
end (``media_end_lead_s``) and played through to its natural end (``ended``
fired), because a seek straight to the end leaves a stale frame and no end
screen. ``currentTime`` alone is no proof: a seek into bytes the recording
never fetched reports the target at once while ``seeking`` stays true and the
frame never changes (measured on the site's player). A seek or play that does
not complete within ``media_seek_timeout_s`` is ``failed``, and the element is
put back where it was (which also stops the player's retry storm on the
missing bytes). Every action is recorded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from agent_cdp import CDPCommandProtocol
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spectragent.core.capabilities.browser.dom.capture import MAX_FRAME_DEPTH, FrameTargets
from spectragent.schemas.restoration import (
    MediaAlignment,
    MediaComparison,
    MediaEvidence,
    MediaReadingPoint,
)

logger = logging.getLogger(__name__)

#: The isolated world media is read and aligned in (``Page.createIsolatedWorld``):
#: one per document, reused by name, invisible to the page's own scripts.
MEDIA_WORLD: Final = "af-media"

#: The most media elements one read reports; a page with more is ``truncated``.
MAX_MEDIA_ELEMENTS: Final = 64
#: The most unreachable frames one read reports.
MAX_UNREACHABLE_FRAMES: Final = 32
#: The longest media URL kept, in characters.
MAX_MEDIA_URL_CHARS: Final = 2048
#: Two durations or two positions this close are the same (float noise, not a tolerance).
SAME_TIME_S: Final = 0.001
#: The longest ``detail`` a record carries, in characters.
_DETAIL_MAX_CHARS: Final = 240
#: How long past its own budget the reply of an awaiting in-page call may take.
_REPLY_GRACE_S: Final = 0.5

#: The kind of source a media element plays: an ordinary URL, a ``blob:`` URL
#: (script-fed media: the bytes come from the page's own requests), a stream
#: (``srcObject``), no source, or something else (``data:``, ``file:`` …).
MediaSource = Literal["url", "blob", "stream", "none", "other"]
#: Whether a read around the screenshot produced an answer.
MediaReadStatus = Literal["measured", "unknown"]


# ── the in-page half ──────────────────────────────────────────────────

#: Runs in one frame's own isolated world and describes that document only:
#: its frame path from the top (each step the frame's index in its parent's
#: frame list, ``window[i]`` — readable across origins, so a cross-site frame
#: names itself the way its parent names it), its media elements in document
#: order (open shadow roots included) and its child frame elements. Shared by
#: the read and the alignment, so both find the same element by the same path
#: and order.
_DOCUMENT = """
const afMediaDocument = () => {
  const indexIn = (parentWindow, childWindow) => {
    for (let i = 0; i < parentWindow.length; i++) {
      if (parentWindow[i] === childWindow) return i;
    }
    return -1;
  };
  let path = 'main';
  try {
    const parts = [];
    let win = window;
    while (win !== win.top) {
      const parent = win.parent;
      const index = indexIn(parent, win);
      if (index < 0) return null;
      parts.unshift(index);
      win = parent;
    }
    path = ['main', ...parts].join('/');
  } catch (error) {
    return null;
  }
  const media = [];
  const frames = [];
  const collect = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const tag = el.tagName;
      if (tag === 'VIDEO' || tag === 'AUDIO') media.push(el);
      else if (tag === 'IFRAME' || tag === 'FRAME') frames.push(el);
      if (el.shadowRoot) collect(el.shadowRoot);
    }
  };
  collect(document);
  return {path: path, media: media, frames: frames, indexIn: indexIn};
};
const afMediaNumber = (value) =>
  (typeof value === 'number' && Number.isFinite(value)) ? value : null;
const afMediaReading = (el) => ({
  current_time: afMediaNumber(el.currentTime),
  paused: !!el.paused,
  seeking: !!el.seeking,
  ready_state: typeof el.readyState === 'number' ? el.readyState : null,
  ended: !!el.ended,
});
"""

_READ_PROBE = (
    "(() => {"
    + _DOCUMENT
    + """
  const doc = afMediaDocument();
  if (!doc) return {path: null};
  const kind = (el) => {
    if (el.srcObject) return 'stream';
    const src = el.currentSrc || el.src || '';
    if (!src) return 'none';
    if (src.startsWith('blob:')) return 'blob';
    if (/^https?:/i.test(src)) return 'url';
    return 'other';
  };
  const elements = doc.media.slice(0, %(max_elements)d).map((el, order) => {
    const box = el.getBoundingClientRect();
    const source = kind(el);
    const duration = el.duration;
    return {
      order: order,
      tag: el.tagName.toLowerCase(),
      x: box.x, y: box.y, width: box.width, height: box.height,
      source_kind: source,
      url: source === 'url' ? String(el.currentSrc || el.src).slice(0, %(max_url)d) : null,
      duration: afMediaNumber(duration),
      live: source === 'stream' || duration === Infinity,
      reading: afMediaReading(el),
    };
  });
  const children = [];
  let unplaced = 0;
  for (const frameEl of doc.frames) {
    if (!frameEl.contentWindow) continue;  // no document of its own, so no media
    let index = -1;
    try { index = doc.indexIn(window, frameEl.contentWindow); } catch (error) { index = -1; }
    // A frame missing from this window's frame list (one inside a shadow root)
    // cannot name itself, so its own read never answers: listed under a name no
    // read claims, it stays unreachable — its media unknown, never "none".
    const frame = index < 0 ? doc.path + '/unplaced-' + (unplaced++) : doc.path + '/' + index;
    const box = frameEl.getBoundingClientRect();
    let padX = 0, padY = 0;
    try {
      const style = getComputedStyle(frameEl);
      padX = parseFloat(style.paddingLeft) || 0;
      padY = parseFloat(style.paddingTop) || 0;
    } catch (error) {}
    children.push({frame: frame,
                   x: box.x + frameEl.clientLeft + padX, y: box.y + frameEl.clientTop + padY,
                   width: frameEl.clientWidth, height: frameEl.clientHeight});
  }
  return {path: doc.path, elements: elements, children: children,
          truncated: doc.media.length > %(max_elements)d};
})()
"""
)
_READ_EXPRESSION: Final = _READ_PROBE % {
    "max_elements": MAX_MEDIA_ELEMENTS,
    "max_url": MAX_MEDIA_URL_CHARS,
}

#: Seek one element of this frame (by its path and order) to a position,
#: optionally play it — with ``toEnd``, play it through to its natural end —
#: and report. ``target`` may be ``null``: only the play state is set. A frame
#: that is not the expected one finds nothing.
_ALIGN_PROBE = (
    "(async (framePath, order, target, play, toEnd, budgetMs) => {"
    + _DOCUMENT
    + """
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const doc = afMediaDocument();
  const el = doc && doc.path === framePath ? doc.media[order] : undefined;
  if (!el) return {found: false};
  const before = afMediaReading(el);
  let seeked = target === null;
  let seekMs = null;
  let landed = null;
  const seekStarted = performance.now();
  if (target !== null) {
    let fired = false;
    const onSeeked = () => { fired = true; };
    el.addEventListener('seeked', onSeeked);
    try {
      el.pause();
      el.currentTime = target;
      for (;;) {
        if (fired && !el.seeking && el.readyState >= 2) { seekMs = performance.now() - seekStarted; break; }
        if (performance.now() - seekStarted >= budgetMs) break;
        await sleep(5);
      }
    } finally {
      el.removeEventListener('seeked', onSeeked);
    }
    seeked = seekMs !== null;
    landed = afMediaNumber(el.currentTime);
  } else if (!play) {
    el.pause();
  }
  let played = null;
  let playError = null;
  let endedFired = false;
  if (seeked && play) {
    let playing = false;
    let started = false;
    const onPlaying = () => { playing = true; };
    const onEnded = () => { endedFired = true; };
    el.addEventListener('playing', onPlaying);
    el.addEventListener('ended', onEnded);
    const playStarted = performance.now();
    try {
      const promise = el.play();
      if (promise && typeof promise.then === 'function') {
        promise.then(() => { started = true; },
                     (error) => { playError = String((error && error.name) || error); });
      } else {
        started = true;
      }
      for (;;) {
        if (playError !== null) break;
        if (toEnd ? (endedFired && el.ended) : (playing && started && !el.paused)) break;
        if (performance.now() - playStarted >= budgetMs) break;
        await sleep(5);
      }
    } finally {
      el.removeEventListener('playing', onPlaying);
      el.removeEventListener('ended', onEnded);
    }
    played = playError === null && (toEnd ? (endedFired && el.ended)
                                          : (playing && started && !el.paused));
  }
  return {found: true, before: before, seeked: seeked, seek_ms: seekMs, landed: landed,
          played: played, ended_fired: endedFired, play_error: playError,
          reached: afMediaReading(el)};
})(%(frame)s, %(order)d, %(target)s, %(play)s, %(to_end)s, %(budget_ms)d)
"""
)


# ── the record ────────────────────────────────────────────────────────


class MediaRect(BaseModel):
    """A box in the top-level viewport, CSS pixels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float
    y: float
    width: float
    height: float


class MediaReading(BaseModel):
    """One media element at one instant; a field not measured is ``None``, never 0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_time: float | None = None
    paused: bool | None = None
    seeking: bool | None = None
    ready_state: int | None = None
    ended: bool | None = None


class MediaElementState(BaseModel):
    """One ``<video>`` / ``<audio>`` element around one screenshot.

    ``frame`` and ``order`` place it: the frame path (``main``, ``main/1`` …)
    and its document order among that frame's media elements. ``url`` is
    kept only for a ``url`` source (``source_kind``). ``duration`` is ``None`` when unknown;
    ``live`` is an infinite duration or a stream, which has no position to
    compare. ``before`` / ``after``: the readings taken just before and just
    after the screenshot (``None``: that read did not see the element, or
    did not answer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame: str
    order: int = Field(ge=0)
    tag: Literal["video", "audio"]
    rect: MediaRect
    source_kind: MediaSource
    url: str | None = None
    duration: float | None = None
    live: bool = False
    before: MediaReading | None = None
    after: MediaReading | None = None

    @property
    def label(self) -> str:
        """How records name it: ``<frame>#<order> <tag>``."""
        return element_label(self.frame, self.order, self.tag)

    def reading(self, point: MediaReadingPoint) -> MediaReading | None:
        return self.before if point == "before" else self.after


class MediaFrameState(BaseModel):
    """A child frame neither read reached (no answer, or in a shadow root): its media is unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame: str
    rect: MediaRect


class MediaState(BaseModel):
    """The media playback state of one screenshot (``ScreenshotObs.media``).

    ``read_before`` / ``read_after``: whether each read around the
    screenshot answered; ``detail`` says why one did not. ``truncated``: the
    page had more than :data:`MAX_MEDIA_ELEMENTS` media elements and only
    the first were kept.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_before: MediaReadStatus
    read_after: MediaReadStatus
    elements: list[MediaElementState] = Field(default_factory=list)
    unreachable_frames: list[MediaFrameState] = Field(default_factory=list)
    truncated: bool = False
    detail: str | None = None

    @property
    def known(self) -> bool:
        """Whether either read answered: a state with neither tells nothing."""
        return self.read_before == "measured" or self.read_after == "measured"


def element_label(frame: str, order: int, tag: str) -> str:
    return f"{frame}#{order} {tag}"


def _clip(text: str, limit: int = _DETAIL_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── reading ───────────────────────────────────────────────────────────


#: Where a frame's document is read and aligned: its CDP session and frame id.
FrameAddress = tuple[str | None, str]


@dataclass(frozen=True)
class MediaRead:
    """One read's answer, or why there is none (``error``).

    ``elements`` carry their frame path and a box in the top viewport;
    ``unreachable`` are the child frames some read saw but whose own read did
    not answer. ``addresses`` say where each frame read was made — the
    session and frame id an alignment of that frame's elements goes to; they
    are never recorded.
    """

    elements: tuple[dict[str, Any], ...] = ()
    unreachable: tuple[dict[str, Any], ...] = ()
    truncated: bool = False
    error: str | None = None
    addresses: Mapping[str, FrameAddress] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def answered(self) -> frozenset[str]:
        """The frame paths whose own read answered (those an alignment can reach)."""
        return frozenset(self.addresses)


async def _bounded(reply: Any, timeout: float) -> Any:
    return await asyncio.wait_for(reply, max(timeout, 0.0))


async def _world(
    cdp: CDPCommandProtocol, session_id: str | None, frame_id: str, timeout: float
) -> int:
    world = await _bounded(
        cdp.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": MEDIA_WORLD},
            session_id=session_id,
        ),
        timeout,
    )
    context = world.get("executionContextId") if isinstance(world, dict) else None
    if not isinstance(context, int):
        raise ValueError("the media world has no execution context")
    return context


def _value(reply: Any, what: str) -> Any:
    if not isinstance(reply, dict):
        raise ValueError(f"the {what} returned no result")
    if reply.get("exceptionDetails"):
        raise ValueError(f"the {what} raised in the page")
    return (reply.get("result") or {}).get("value")


async def _read_frame(
    cdp: CDPCommandProtocol, session_id: str | None, frame_id: str, timeout: float
) -> dict[str, Any]:
    """One frame's document, read in its own isolated world (``_READ_PROBE``)."""
    context = await _world(cdp, session_id, frame_id, timeout)
    reply = await cdp.send(
        "Runtime.evaluate",
        {"expression": _READ_EXPRESSION, "contextId": context, "returnByValue": True},
        session_id=session_id,
    )
    value = _value(reply, "media read")
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("path"), str)
        or not isinstance(value.get("elements"), list)
        or not isinstance(value.get("children"), list)
    ):
        raise ValueError("the media read returned no result")
    return value


def _frame_ids(tree: Any) -> list[str]:
    """Every frame id of a ``Page.getFrameTree`` reply, depth first."""
    ids: list[str] = []
    pending = [((tree or {}).get("frameTree") if isinstance(tree, dict) else None)]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        frame_id = (node.get("frame") or {}).get("id")
        if isinstance(frame_id, str) and frame_id:
            ids.append(frame_id)
        pending.extend(reversed(node.get("childFrames") or []))
    return ids


async def _frames_of(
    cdp: CDPCommandProtocol, session_id: str | None, root_frame_id: str
) -> list[FrameAddress]:
    """The frames one session serves: its frame tree, else just its root frame."""
    try:
        ids = _frame_ids(await cdp.send("Page.getFrameTree", {}, session_id=session_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("media read: frame tree of session %s failed: %r", session_id, exc)
        ids = []
    if root_frame_id not in ids:
        ids.insert(0, root_frame_id)
    return [(session_id, frame_id) for frame_id in ids]


async def _addresses(
    cdp: CDPCommandProtocol,
    session_id: str | None,
    frame_id: str,
    frames: FrameTargets | None,
) -> list[FrameAddress]:
    """Every frame of the tab: those it renders itself, then each out-of-process frame's own."""
    found = await _frames_of(cdp, session_id, frame_id)
    if frames is None or session_id is None:
        return found
    frames.watch(session_id)  # idempotent: the tab's first read starts the discovery
    pending = [(session_id, 0)]
    while pending:
        parent, depth = pending.pop(0)
        if depth >= MAX_FRAME_DEPTH:
            continue
        for child in await frames.children(parent):
            found.extend(await _frames_of(cdp, child.session_id, child.target_id))
            pending.append((child.session_id, depth + 1))
    return found


def _depth(path: str) -> int:
    return path.count("/")


def _compose(reads: dict[str, tuple[FrameAddress, dict[str, Any]]]) -> MediaRead:
    """Frame reads put together: top-viewport boxes, element cap, frames that did not answer."""
    # Each child frame's content box, as its parent's read placed it.
    placed: dict[str, dict[str, Any]] = {}
    for path, (_address, value) in reads.items():
        for child in value["children"]:
            if isinstance(child, dict) and isinstance(child.get("frame"), str):
                placed.setdefault(child["frame"], {**child, "parent": path})
    offsets: dict[str, tuple[float, float]] = {"main": (0.0, 0.0)}
    for path in sorted(placed, key=_depth):
        box = placed[path]
        parent = offsets.get(box["parent"])
        if parent is None:
            continue
        try:
            offsets[path] = (parent[0] + float(box["x"]), parent[1] + float(box["y"]))
        except (KeyError, TypeError, ValueError):
            continue
    elements: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(reads, key=lambda p: (_depth(p), p)):
        _address, value = reads[path]
        truncated = truncated or bool(value.get("truncated"))
        dx, dy = offsets.get(path, (0.0, 0.0))
        for raw in value["elements"]:
            if not isinstance(raw, dict):
                continue
            if len(elements) >= MAX_MEDIA_ELEMENTS:
                truncated = True
                break
            try:
                elements.append(
                    {**raw, "frame": path, "x": float(raw["x"]) + dx, "y": float(raw["y"]) + dy}
                )
            except (KeyError, TypeError, ValueError):
                continue
    unreachable: list[dict[str, Any]] = []
    for path in sorted(placed, key=lambda p: (_depth(p), p)):
        if path in reads or len(unreachable) >= MAX_UNREACHABLE_FRAMES:
            continue
        box = placed[path]
        parent = offsets.get(box["parent"], (0.0, 0.0))
        try:
            unreachable.append(
                {
                    "frame": path,
                    "x": float(box["x"]) + parent[0],
                    "y": float(box["y"]) + parent[1],
                    "width": float(box["width"]),
                    "height": float(box["height"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return MediaRead(
        elements=tuple(elements),
        unreachable=tuple(unreachable),
        truncated=truncated,
        addresses={path: address for path, (address, _value) in reads.items()},
    )


async def read_media(
    cdp: CDPCommandProtocol,
    *,
    session_id: str | None,
    frame_id: str | None,
    frames: FrameTargets | None,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> MediaRead:
    """Read every media element of the tab, every frame in its own world, within *timeout_s*.

    The main frame and the frames the tab renders itself are read on the
    tab's session; each out-of-process frame (a cross-site iframe) on its own
    session (*frames*, ``dom/capture.py``'s ``FrameTargets``). All reads run
    together against one deadline. A child frame whose own read did not
    answer is listed as unreachable; a main frame that did not answer makes
    the whole read unknown. Never raises for the page's sake.
    """
    if frame_id is None:
        return MediaRead(error="the tab's main frame is not known")
    deadline = clock() + max(timeout_s, 0.0)
    main = asyncio.ensure_future(_read_frame(cdp, session_id, frame_id, timeout_s))
    tasks: dict[asyncio.Future[dict[str, Any]], FrameAddress] = {main: (session_id, frame_id)}
    try:
        try:
            addresses = await _bounded(
                _addresses(cdp, session_id, frame_id, frames), deadline - clock()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the main frame's read may still answer
            logger.debug("media read (sid=%s): frame discovery failed: %r", session_id, exc)
            addresses = []
        for address in addresses:
            if address != (session_id, frame_id) and address not in tasks.values():
                task = asyncio.ensure_future(_read_frame(cdp, address[0], address[1], timeout_s))
                tasks[task] = address
        await asyncio.wait(list(tasks), timeout=max(deadline - clock(), 0.0))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    if main.cancelled() or not main.done() or isinstance(main.exception(), TimeoutError):
        return MediaRead(error=f"the media read got no answer within {timeout_s:g}s")
    if main.exception() is not None:
        exc = main.exception()
        logger.debug("media read (sid=%s) failed: %r", session_id, exc)
        return MediaRead(error=_clip(f"the media read failed: {exc}"))
    reads: dict[str, tuple[FrameAddress, dict[str, Any]]] = {}
    for task, address in tasks.items():
        if task.done() and not task.cancelled() and task.exception() is None:
            value = task.result()
            reads.setdefault(value["path"], (address, value))
    if "main" not in reads:
        return MediaRead(error="the media read returned no result")
    return _compose(reads)


def _reading(raw: Any) -> MediaReading | None:
    if not isinstance(raw, dict):
        return None
    try:
        return MediaReading.model_validate(raw)
    except ValidationError:
        return None


def _element_key(raw: dict[str, Any]) -> tuple[str, int] | None:
    frame, order = raw.get("frame"), raw.get("order")
    if not isinstance(frame, str) or not isinstance(order, int) or isinstance(order, bool):
        return None
    return frame, order


def _element(raw: dict[str, Any], before: Any, after: Any) -> MediaElementState | None:
    try:
        return MediaElementState(
            frame=raw["frame"],
            order=raw["order"],
            tag=raw["tag"],
            rect=MediaRect(x=raw["x"], y=raw["y"], width=raw["width"], height=raw["height"]),
            source_kind=raw["source_kind"],
            url=raw.get("url"),
            duration=raw.get("duration"),
            live=bool(raw.get("live")),
            before=_reading(before),
            after=_reading(after),
        )
    except (KeyError, TypeError, ValidationError):
        return None


def media_state(before: MediaRead, after: MediaRead) -> MediaState:
    """The screenshot's media state from the reads just before and just after it.

    An element is placed by its first read (before, else after); its two
    readings are joined by frame path and order. An element only one read
    saw has ``None`` for the other reading. A frame is unreachable when it
    answered neither read: one that answered either has its media known.
    """
    by_key: dict[tuple[str, int], list[dict[str, Any] | None]] = {}
    for index, read in enumerate((before, after)):
        for raw in read.elements:
            key = _element_key(raw)
            if key is None:
                continue
            slot = by_key.setdefault(key, [None, None])
            slot[index] = raw
    elements: list[MediaElementState] = []
    for pair in by_key.values():
        place = pair[0] if pair[0] is not None else pair[1]
        assert place is not None
        element = _element(
            place,
            pair[0].get("reading") if pair[0] is not None else None,
            pair[1].get("reading") if pair[1] is not None else None,
        )
        if element is not None:
            elements.append(element)
    elements.sort(key=lambda e: (e.frame, e.order))
    # A frame is unreachable only when no read answered for it: one that
    # answered the other read has its media known, with that reading.
    answered = before.answered | after.answered
    frames: dict[str, MediaFrameState] = {}
    for read in (before, after):
        for raw in read.unreachable:
            if raw.get("frame") in answered:
                continue
            try:
                frame = MediaFrameState(
                    frame=raw["frame"],
                    rect=MediaRect(
                        x=raw["x"], y=raw["y"], width=raw["width"], height=raw["height"]
                    ),
                )
            except (KeyError, TypeError, ValidationError):
                continue
            frames.setdefault(frame.frame, frame)
    problems = [
        f"{point}: {read.error}"
        for point, read in (("before", before), ("after", after))
        if read.error is not None
    ]
    return MediaState(
        read_before="measured" if before.ok else "unknown",
        read_after="measured" if after.ok else "unknown",
        elements=elements,
        unreachable_frames=sorted(frames.values(), key=lambda f: f.frame),
        truncated=before.truncated or after.truncated,
        detail=_clip("; ".join(problems)) if problems else None,
    )


# ── matching ──────────────────────────────────────────────────────────


def _within(a: MediaRect, b: MediaRect, tolerance_px: float) -> bool:
    return (
        abs(a.x - b.x) <= tolerance_px
        and abs(a.y - b.y) <= tolerance_px
        and abs(a.width - b.width) <= tolerance_px
        and abs(a.height - b.height) <= tolerance_px
    )


def _same_source(a: MediaElementState, b: MediaElementState) -> bool:
    """Same kind and tag; for two ``url`` sources also the same URL and duration.

    A ``url`` source's duration is the file's own. A script-fed (``blob:``)
    source's is whatever the page's script sets, and it changes as data
    arrives: Xiaohongshu's player reports 30.93 s until the last segment is
    appended and 31.04 s after (measured), so it identifies nothing.
    """
    if a.source_kind != b.source_kind or a.tag != b.tag:
        return False
    if a.source_kind != "url":
        return True
    if a.url is not None and b.url is not None and a.url != b.url:
        return False
    return not (
        a.duration is not None
        and b.duration is not None
        and abs(a.duration - b.duration) > SAME_TIME_S
    )


@dataclass(frozen=True)
class MediaMatch:
    """How one element corresponds across the two states (:func:`match_media`)."""

    correspondence: Literal["matched", "missing", "extra", "ambiguous"]
    recorded: MediaElementState | None
    restored: MediaElementState | None
    detail: str = ""

    @property
    def label(self) -> str:
        element = self.recorded if self.recorded is not None else self.restored
        assert element is not None
        return element.label


def match_media(
    recorded: Sequence[MediaElementState],
    restored: Sequence[MediaElementState],
    *,
    tolerance_px: float,
) -> list[MediaMatch]:
    """Every recorded element's counterpart, then the restored elements nothing matched.

    Candidates share the frame, lie within *tolerance_px* on all four box
    values and have the same source (kind and tag; an equal URL for two
    ``url`` sources, and an equal duration when both of those are known). Exactly one
    candidate at the same document order is ``matched``; none ``missing``;
    otherwise ``ambiguous``. A restored element that is the matched
    counterpart of no recorded one is ``extra``.
    """
    matches: list[MediaMatch] = []
    used: set[tuple[str, int]] = set()
    for element in recorded:
        candidates = [
            other
            for other in restored
            if other.frame == element.frame
            and _within(element.rect, other.rect, tolerance_px)
            and _same_source(element, other)
        ]
        if not candidates:
            matches.append(
                MediaMatch(
                    "missing",
                    element,
                    None,
                    "no media element in the same frame, place and source",
                )
            )
        elif len(candidates) == 1 and candidates[0].order == element.order:
            used.add((candidates[0].frame, candidates[0].order))
            matches.append(MediaMatch("matched", element, candidates[0]))
        else:
            orders = ", ".join(str(c.order) for c in candidates)
            matches.append(
                MediaMatch(
                    "ambiguous",
                    element,
                    None,
                    f"{len(candidates)} candidate(s) at document order {orders}; "
                    f"recorded order {element.order}",
                )
            )
    for other in restored:
        if (other.frame, other.order) not in used:
            matches.append(
                MediaMatch("extra", None, other, "no recorded media element corresponds to it")
            )
    return matches


def _common_point(
    recorded: MediaElementState, restored: MediaElementState
) -> MediaReadingPoint | None:
    for point in ("before", "after"):
        if recorded.reading(point) is not None and restored.reading(point) is not None:
            return point
    return None


def compare_media(
    recorded: MediaState | None,
    restored: MediaState | None,
    *,
    tolerance_px: float,
) -> tuple[list[MediaComparison] | None, str]:
    """Element by element comparison of two screenshots' media states, or ``None`` and why.

    Measures only — positions and play states side by side; the restorer
    judges them against its tolerance (``loops/restore/checks.py``).
    """
    if recorded is None or restored is None:
        side = "recorded" if recorded is None else "restored"
        return None, f"the {side} screenshot carries no media state"
    for side, state in (("recorded", recorded), ("restored", restored)):
        if not state.known:
            return None, f"the {side} media state is unknown ({state.detail or 'no read answered'})"
    comparisons: list[MediaComparison] = []
    for match in match_media(recorded.elements, restored.elements, tolerance_px=tolerance_px):
        if match.correspondence != "matched":
            element = match.recorded if match.recorded is not None else match.restored
            assert element is not None
            comparisons.append(
                MediaComparison(
                    element=match.label,
                    correspondence=match.correspondence,
                    live=element.live,
                    detail=match.detail,
                )
            )
            continue
        assert match.recorded is not None and match.restored is not None
        point = _common_point(match.recorded, match.restored)
        before = match.recorded.reading(point) if point else None
        after = match.restored.reading(point) if point else None
        comparisons.append(
            MediaComparison(
                element=match.label,
                correspondence="matched",
                live=match.recorded.live or match.restored.live,
                reading=point,
                recorded_time=before.current_time if before else None,
                restored_time=after.current_time if after else None,
                recorded_paused=before.paused if before else None,
                restored_paused=after.paused if after else None,
                detail="" if point else "no reading both sides took at the same point",
            )
        )
    return comparisons, ""


def media_evidence(
    recorded: MediaState | None,
    restored: MediaState | None,
    *,
    tolerance_px: float,
    alignment: list[MediaAlignment] | None,
    alignment_detail: str = "",
) -> MediaEvidence:
    comparisons, detail = compare_media(recorded, restored, tolerance_px=tolerance_px)
    return MediaEvidence(
        comparisons=comparisons,
        detail=detail,
        recorded_unreachable_frames=_unreachable(recorded),
        restored_unreachable_frames=_unreachable(restored),
        alignment=alignment,
        alignment_detail=alignment_detail,
    )


def _unreachable(state: MediaState | None) -> list[str]:
    return [frame.frame for frame in state.unreachable_frames] if state is not None else []


# ── aligning ──────────────────────────────────────────────────────────


@dataclass
class AlignmentReport:
    """What one alignment did, for the next restoration check."""

    records: list[MediaAlignment] = field(default_factory=list)
    detail: str = ""

    @property
    def left_playing(self) -> bool:
        """Whether an element the recording showed playing now plays from its target.

        The capture then takes its screenshot before every other fragment
        (``BrowserStateRequestEvent.screenshot_first``): anything captured
        in between moves the element on.
        """
        return any(
            record.target_paused is False and record.outcome in ("aligned", "already_aligned")
            for record in self.records
        )


@dataclass
class PendingAlignment:
    """An alignment a restorer requested for the next observation (``align_next_observation``).

    It belongs to exactly one observation cycle: the first one opened after
    the request (:meth:`bind`). The capture of that cycle aligns once and
    keeps the :attr:`report`; the next restoration check takes it. A request
    still pending when a later cycle opens is dropped, so an alignment never
    reaches an observation after the one it was requested for.
    """

    recorded: MediaState | None
    seek_timeout_s: float
    end_lead_s: float
    cycle: int | None = None
    report: AlignmentReport | None = None

    def bind(self, cycle: int) -> bool:
        """Belong to *cycle* if unbound; whether this request still applies to *cycle*."""
        if self.cycle is None:
            self.cycle = cycle
        return self.cycle == cycle

    def due(self, cycle: int) -> bool:
        """Whether the capture of *cycle* still has to align."""
        return self.cycle == cycle and self.report is None

    def take(self) -> AlignmentReport:
        """What was aligned; a request no capture acted on says so."""
        if self.report is not None:
            return self.report
        return AlignmentReport(detail="no capture ran after the alignment was requested")


def _target(element: MediaElementState) -> MediaReading | None:
    """The recorded reading to align to: the one just before the screenshot, else just after."""
    for reading in (element.before, element.after):
        if reading is not None and reading.current_time is not None and reading.paused is not None:
            return reading
    return None


async def _call_align(
    cdp: CDPCommandProtocol,
    *,
    address: FrameAddress,
    element: MediaElementState,
    target: float | None,
    play: bool,
    budget_s: float,
    to_end: bool = False,
) -> dict[str, Any]:
    session_id, frame_id = address
    expression = _ALIGN_PROBE % {
        "frame": json.dumps(element.frame),
        "order": element.order,
        "target": "null" if target is None else repr(float(target)),
        "play": "true" if play or to_end else "false",
        "to_end": "true" if to_end else "false",
        "budget_ms": max(1, round(budget_s * 1000)),
    }
    context = await _world(cdp, session_id, frame_id, budget_s + _REPLY_GRACE_S)
    reply = await _bounded(
        cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": context,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=session_id,
        ),
        2 * budget_s + _REPLY_GRACE_S,
    )
    value = _value(reply, "media alignment")
    if not isinstance(value, dict):
        raise ValueError("the media alignment returned no result")
    return value


async def align_media(
    cdp: CDPCommandProtocol,
    *,
    session_id: str | None,
    frame_id: str | None,
    frames: FrameTargets | None,
    recorded: MediaState | None,
    tolerance_px: float,
    read_timeout_s: float,
    seek_timeout_s: float,
    end_lead_s: float,
) -> AlignmentReport:
    """Move every matched media element to its recorded position and play state (module docstring).

    Each element is aligned in its own frame's world, on the session that
    frame was read on — a cross-site frame's own session included. Never
    raises for the page's sake: what cannot be done is recorded.
    """
    if recorded is None:
        return AlignmentReport(detail="the recorded screenshot carries no media state")
    if not recorded.known:
        return AlignmentReport(
            detail=f"the recorded media state is unknown ({recorded.detail or 'no read answered'})"
        )
    live = await read_media(
        cdp, session_id=session_id, frame_id=frame_id, frames=frames, timeout_s=read_timeout_s
    )
    if not live.ok:
        return AlignmentReport(
            records=[
                MediaAlignment(
                    element=e.label,
                    outcome="unknown",
                    detail=_clip(f"the page's media could not be read: {live.error}"),
                )
                for e in recorded.elements
            ],
            detail=_clip(f"the page's media could not be read: {live.error}"),
        )
    current = media_state(live, MediaRead(error="not read"))
    report = AlignmentReport(
        detail="" if recorded.elements else "no media element was recorded on this screenshot"
    )
    planned: list[tuple[MediaElementState, MediaElementState, MediaReading]] = []
    for match in match_media(recorded.elements, current.elements, tolerance_px=tolerance_px):
        if match.correspondence == "extra":
            continue
        element = match.recorded
        assert element is not None
        if match.correspondence != "matched":
            report.records.append(
                MediaAlignment(
                    element=element.label,
                    outcome="not_restorable",
                    detail=_clip(f"{match.correspondence}: {match.detail}"),
                )
            )
            continue
        assert match.restored is not None
        if element.live or match.restored.live:
            report.records.append(
                MediaAlignment(
                    element=element.label,
                    outcome="not_restorable",
                    detail="a live stream has no position to align",
                )
            )
            continue
        target = _target(element)
        now = match.restored.before
        if target is None:
            report.records.append(
                MediaAlignment(
                    element=element.label,
                    outcome="unknown",
                    from_time=now.current_time if now else None,
                    from_paused=now.paused if now else None,
                    detail="the recorded position or play state is not known",
                )
            )
            continue
        assert target.current_time is not None
        if (
            now is not None
            and now.current_time is not None
            and now.paused == target.paused
            and abs(now.current_time - target.current_time) <= SAME_TIME_S
            and (not target.ended or now.ended is True)
        ):
            report.records.append(
                MediaAlignment(
                    element=element.label,
                    outcome="already_aligned",
                    target_time=target.current_time,
                    target_paused=target.paused,
                    from_time=now.current_time,
                    from_paused=now.paused,
                    reached_time=now.current_time,
                    reached_paused=now.paused,
                    ready_state=now.ready_state,
                    seeking=now.seeking,
                )
            )
            continue
        planned.append((element, match.restored, target))
    # Paused targets first: a playing one aligned last drifts least before the capture.
    planned.sort(key=lambda item: not bool(item[2].paused))
    for element, restored, target in planned:
        address = live.addresses.get(restored.frame)
        if address is None:
            report.records.append(
                MediaAlignment(
                    element=element.label,
                    outcome="not_restorable",
                    detail="the frame it is in was not read",
                )
            )
            continue
        report.records.append(
            await _align_one(
                cdp,
                address=address,
                recorded=element,
                restored=restored,
                target=target,
                seek_timeout_s=seek_timeout_s,
                end_lead_s=end_lead_s,
            )
        )
    return report


def alignment_raised(recorded: MediaState | None, error: BaseException) -> AlignmentReport:
    """The report of an alignment that raised part way: every recorded element ``failed``.

    Where each element was left is not known, so none may pass.
    """
    detail = _clip(f"the alignment raised {type(error).__name__}: {error}")
    elements = recorded.elements if recorded is not None else []
    return AlignmentReport(
        records=[
            MediaAlignment(element=e.label, outcome="failed", detail=detail) for e in elements
        ],
        detail=detail,
    )


async def _align_one(
    cdp: CDPCommandProtocol,
    *,
    address: FrameAddress,
    recorded: MediaElementState,
    restored: MediaElementState,
    target: MediaReading,
    seek_timeout_s: float,
    end_lead_s: float,
) -> MediaAlignment:
    assert target.current_time is not None  # _target only returns a known position
    # A recorded end is reached the way the source reached it: by playing into
    # it. Seeking straight to the end leaves a stale frame and no end screen
    # (measured on Xiaohongshu's player), so seek a short lead before it and
    # play through to the natural end.
    to_end = target.ended is True
    play = target.paused is False
    seek_to = max(0.0, target.current_time - end_lead_s) if to_end else target.current_time
    action: Literal["pause_and_seek", "seek_and_play", "seek_and_play_to_end"] = (
        "seek_and_play_to_end" if to_end else "seek_and_play" if play else "pause_and_seek"
    )
    base: dict[str, Any] = {
        "element": recorded.label,
        "action": action,
        "target_time": target.current_time,
        "target_paused": target.paused,
    }
    try:
        result = await _call_align(
            cdp,
            address=address,
            element=restored,
            target=seek_to,
            play=play,
            budget_s=seek_timeout_s,
            to_end=to_end,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        now = restored.before or MediaReading()
        rolled_back = await _roll_back(
            cdp,
            address=address,
            element=restored,
            before=now,
            seek_timeout_s=seek_timeout_s,
        )
        return MediaAlignment(
            **base,
            outcome="failed",
            from_time=now.current_time,
            from_paused=now.paused,
            rolled_back=rolled_back,
            detail=_clip(f"the alignment did not answer: {type(exc).__name__}: {exc}"),
        )
    if not result.get("found"):
        return MediaAlignment(
            **base,
            outcome="not_restorable",
            detail="the element was gone when it was to be aligned",
        )
    before = _reading(result.get("before")) or MediaReading()
    reached = _reading(result.get("reached")) or MediaReading()
    seek_ms = result.get("seek_ms")
    seek_ms = float(seek_ms) if isinstance(seek_ms, int | float) else None
    landed = result.get("landed")
    landed = float(landed) if isinstance(landed, int | float) else None
    # A completed seek is not enough: it must have landed on the target. A seek
    # past what a script-fed player has appended completes somewhere else
    # (measured on Xiaohongshu: aimed at 31.04 s, landed at 26.66 s).
    on_target = landed is not None and abs(landed - seek_to) <= SAME_TIME_S
    seeked = bool(result.get("seeked"))
    completed = seeked and on_target and (not (play or to_end) or result.get("played") is True)
    record: dict[str, Any] = {
        **base,
        "from_time": before.current_time,
        "from_paused": before.paused,
        "reached_time": reached.current_time,
        "reached_paused": reached.paused,
        "ready_state": reached.ready_state,
        "seeking": reached.seeking,
        "seek_ms": seek_ms,
    }
    if completed:
        if to_end:
            detail = f"seeked to {seek_to:g} s and played through to its natural end"
        elif play:
            detail = "played after the seek; the player may show its controls"
        else:
            detail = "paused and seeked"
        return MediaAlignment(**record, outcome="aligned", detail=detail)
    if not seeked:
        why = (
            f"the seek did not complete within {seek_timeout_s:g}s "
            f"(seeking {reached.seeking}, readyState {reached.ready_state})"
        )
    elif not on_target:
        why = f"the seek landed at {landed} s, not at {seek_to:g} s"
    elif to_end:
        why = (
            f"the end was not reached within {seek_timeout_s:g}s of playing from {seek_to:g} s"
            + (f" ({result.get('play_error')})" if result.get("play_error") else "")
        )
    else:
        why = f"play did not start within {seek_timeout_s:g}s" + (
            f" ({result.get('play_error')})" if result.get("play_error") else ""
        )
    rolled_back = await _roll_back(
        cdp,
        address=address,
        element=restored,
        before=before,
        seek_timeout_s=seek_timeout_s,
    )
    return MediaAlignment(
        **record,
        outcome="failed",
        rolled_back=rolled_back,
        detail=_clip(
            why
            + ("; put back where it was" if rolled_back else "; putting it back did not complete")
        ),
    )


async def _roll_back(
    cdp: CDPCommandProtocol,
    *,
    address: FrameAddress,
    element: MediaElementState,
    before: MediaReading,
    seek_timeout_s: float,
) -> bool:
    """Seek back to the position before alignment and restore its play state; whether that completed."""
    play = before.paused is False
    try:
        result = await _call_align(
            cdp,
            address=address,
            element=element,
            target=before.current_time,
            play=play,
            budget_s=seek_timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("media roll-back of %s failed: %r", element.label, exc)
        return False
    return (
        bool(result.get("found"))
        and bool(result.get("seeked"))
        and (not play or result.get("played") is True)
    )


__all__ = [
    "MAX_MEDIA_ELEMENTS",
    "MAX_MEDIA_URL_CHARS",
    "MAX_UNREACHABLE_FRAMES",
    "MEDIA_WORLD",
    "SAME_TIME_S",
    "AlignmentReport",
    "MediaElementState",
    "MediaFrameState",
    "MediaMatch",
    "MediaRead",
    "MediaReadStatus",
    "MediaReading",
    "MediaRect",
    "MediaSource",
    "MediaState",
    "PendingAlignment",
    "align_media",
    "alignment_raised",
    "compare_media",
    "element_label",
    "match_media",
    "media_evidence",
    "media_state",
    "read_media",
]
