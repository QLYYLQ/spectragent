"""``browser.frames`` — screenshots the agent never sees, kept for offline labels (§4.14).

Two kinds of frame, each a clean viewport screenshot of the focused tab
(``Page.captureScreenshot``, PNG, no cursor drawn into it) taken on the
recorder connection's session for that tab, so it has the pixel size of the
observation's screenshot without touching the control connection:

- **``before_action``** — the moment before each ``env.act`` call does
  anything to the page (before any scroll into view, pointer move or key).
  The archive run announces every call on the ``action_boundary`` channel,
  numbered since the observation cycle, and the action waits for this
  session — at most ``max_action_wait_s``, and not at all on a tab whose last
  :data:`TIMEOUTS_BEFORE_NOT_WAITING` captures timed out with no success in
  between (a background capture then probes the tab, and the first success
  resumes waiting). The first frame of a step is the screen after the model
  finished thinking, the later ones the screens between the step's actions,
  which no observation shows. Each entry says when the action was let go
  (``released_at``), so the recording's share of an action span is
  ``released_at - requested_at``.
- **``after_observation``** — ``after_observation_delay_s`` after each
  committed observation cycle, once, to judge by pixels whether the observed
  frame was still changing. It is cancelled (``superseded``) when an action
  or a new observation cycle starts before it is due; a frame already due
  when an action starts is taken before the action runs, within the same
  ``max_action_wait_s``, or listed as ``timeout``.

What a collection hands over: one content-addressed PNG per captured frame
and one step-scoped index, ``frames.json`` (format ``browser-frames-2``),
listing every frame resolved since the last collection in the order they
were resolved — captured, superseded, or not captured with an explicit
outcome (``timeout`` / ``failed`` / ``no_tab``) and its ``detail``. Nothing a
frame does fails a collection. A collection first lets an
``after_observation`` frame that is still waiting finish (at most the rest of
the delay plus ``capture_timeout_s``), so the frame that follows step *t*'s
observation lands in step *t*'s batch; anything still in flight after that
goes, exactly once, into a later batch, its ``cycle`` naming the observation
it follows.

Labels only: the frames are never an observation fragment, never rendered to
a model, and never read by restoration checks, which compare only what the
agent saw. The pure readers at the bottom (:func:`read_frames`,
:func:`iter_frames`, :func:`read_frame_image`) take an ``ArchiveReader``.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.source import ArchiveEntry, ArchiveReader
from spectragent.core.capabilities.browser.archive.base import (
    ActionBoundary,
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    CommittedCycle,
)
from spectragent.schemas.observation.image import image_size

logger = logging.getLogger(__name__)

#: The archiver's registry key.
FRAMES_ARCHIVER_ID = "browser.frames"
#: The step-scoped index each non-empty collection hands over.
FRAMES_INDEX_NAME = "frames.json"
#: The index's format tag.
FRAMES_FORMAT = "browser-frames-2"
#: The capture command and its parameters: the observation's own (PNG, viewport).
CAPTURE_METHOD = "Page.captureScreenshot"
CAPTURE_PARAMS: dict[str, Any] = {"format": "png"}
#: Timeouts on one tab, with no successful capture between them, after which
#: actions on that tab stop waiting for their frame.
TIMEOUTS_BEFORE_NOT_WAITING = 2
#: Seconds the CDP client's own timeout exceeds a capture's, so the capture's
#: own bound is the one that fires (and is reported as ``timeout``).
CLIENT_TIMEOUT_MARGIN_S = 0.5
#: The longest a knob may make one screenshot or one action wait, in seconds.
MAX_WAIT_KNOB_S = 5.0

FrameKind = Literal["before_action", "after_observation"]
FrameOutcome = Literal["captured", "superseded", "timeout", "failed", "no_tab"]
FrameSuperseder = Literal["action", "observation"]


@dataclass(frozen=True)
class FrameRecord:
    """One entry of a ``frames.json`` index: a frame, captured or not.

    ``cycle`` is the observation cycle the frame follows. For a
    ``before_action`` frame, ``action_index`` is the ordinal of its
    ``env.act`` call since that cycle — the index into the step's
    ``StepRecord.action_spans``, not into ``StepRecord.actions`` (an action
    ``guarded_act`` refused never reaches the environment and is in
    neither) — and ``action`` the action's name; both are ``None`` for
    ``after_observation``. ``requested_at`` is the action's start, or the
    cycle's commit plus the delay; ``captured_at`` is when the capture's
    reply arrived (``None`` when nothing was captured); ``released_at`` is
    when this archiver let the action go (``before_action`` only, ``None``
    otherwise), so ``released_at - requested_at`` is what recording added to
    the action's span. ``name`` is the image's file name among the same
    batch's ``browser.frames`` files and ``sha256`` its digest; with
    ``width`` and ``height`` they are ``None`` unless the outcome is
    ``captured`` (``width`` / ``height`` also when the PNG header could not
    be read). ``superseded_by`` says what cancelled a ``superseded`` frame;
    ``detail`` says why a frame was not captured.
    """

    kind: FrameKind
    cycle: int
    action_index: int | None
    action: str | None
    tab: str
    requested_at: float
    captured_at: float | None
    released_at: float | None
    outcome: FrameOutcome
    superseded_by: FrameSuperseder | None
    name: str | None
    sha256: str | None
    width: int | None
    height: int | None
    detail: str | None


def frame_name(kind: FrameKind, cycle: int, action_index: int | None) -> str:
    """A frame's image file name: unique within a run (one per cycle and action)."""
    if kind == "before_action":
        return f"frame-{cycle}-before-{action_index}.png"
    return f"frame-{cycle}-after.png"


class FramesArchiver(BrowserArchiverBase):
    """Viewport screenshots before each action and shortly after each observation."""

    __registry_key__ = "browser.frames"
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset({"action_boundary"})

    def __init__(
        self,
        before_action: Annotated[
            bool,
            Field(
                description=(
                    "Capture the focused tab right before each action touches the page. "
                    "The action waits for it: usually one screenshot (about 30-50 ms at "
                    "1280x720), at most max_action_wait_s plus 0.5 s; after two timeouts on "
                    "a tab with no success between them it does not wait there at all until "
                    "a capture succeeds again. Each entry's released_at says when the "
                    "action was let go."
                ),
            ),
        ] = True,
        after_observation_delay_s: Annotated[
            float | None,
            Field(
                gt=0,
                description=(
                    "Seconds after each committed observation to capture the tab once "
                    "more, to judge whether the observed screen was still changing; "
                    "cancelled when an action or a new observation starts first. "
                    "null turns it off."
                ),
            ),
        ] = 1.0,
        capture_timeout_s: Annotated[
            float,
            Field(
                gt=0,
                le=MAX_WAIT_KNOB_S,
                description=(
                    "Seconds one screenshot may take (at most 5); a capture that times "
                    "out is listed with outcome timeout, never left out."
                ),
            ),
        ] = 3.0,
        max_action_wait_s: Annotated[
            float,
            Field(
                gt=0,
                le=MAX_WAIT_KNOB_S,
                description=(
                    "Most seconds one action waits for its frames (at most 5): its own "
                    "before_action frame and an after_observation frame that came due "
                    "before it began. Past it they are listed as timeout and the action "
                    "runs."
                ),
            ),
        ] = 1.0,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept recording a browser that did not start from a fresh profile "
                    "(logged-in pages). Screenshots carry no cookies or storage either way."
                ),
            ),
        ] = False,
    ) -> None:
        self.before_action = before_action
        self.after_observation_delay_s = after_observation_delay_s
        self.capture_timeout_s = capture_timeout_s
        self.max_action_wait_s = max_action_wait_s
        self.allow_session_state = allow_session_state

    def open(self, ctx: BrowserArchiveContext) -> "FramesSession":
        return FramesSession(self, ctx)


@dataclass(eq=False)
class _Scheduled:
    """An ``after_observation`` frame waiting for its time."""

    cycle: int
    tab: str
    requested_at: float
    task: "asyncio.Task[None] | None" = None
    #: Set when its time came and it started (a supersede is too late then).
    started: bool = False


@dataclass(frozen=True)
class _Shot:
    """What one screenshot attempt gave."""

    outcome: Literal["captured", "timeout", "failed", "no_tab"]
    data: bytes | None = None
    captured_at: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Resolved:
    """A frame whose outcome is known, waiting for the next collection."""

    record: FrameRecord
    data: bytes | None
    #: Wall-clock seconds at which the outcome became known.
    resolved_at: float


class FramesSession(BrowserArchiveSession):
    """One run's frames: captures on action boundaries and after committed cycles."""

    archiver: FramesArchiver

    def __init__(self, archiver: FramesArchiver, ctx: BrowserArchiveContext) -> None:
        super().__init__(archiver, ctx)
        self._resolved: list[_Resolved] = []
        #: The latest ``after_observation`` frame not resolved yet.
        self._scheduled: _Scheduled | None = None
        #: Every task still running: ``after_observation`` frames and probes.
        self._tasks: set[asyncio.Task[None]] = set()
        #: Timeouts per tab since its last successful capture.
        self._timeouts: dict[str, int] = {}
        #: The background capture probing a tab actions no longer wait on.
        self._probes: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    # ── what it wants ──────────────────────────────────────────────────

    @property
    def wants_action_boundaries(self) -> bool:
        return self.archiver.before_action or self.archiver.after_observation_delay_s is not None

    @property
    def action_boundary_budget_s(self) -> float:
        """The action's whole wait: a due ``after_observation`` frame and its own capture."""
        return self.archiver.max_action_wait_s

    # ── producers ──────────────────────────────────────────────────────

    def on_cycle_committed(self, cycle: CommittedCycle) -> None:
        """Schedule this cycle's ``after_observation`` frame; an earlier one still waiting is superseded."""
        delay = self.archiver.after_observation_delay_s
        if self._closed or delay is None:
            return
        previous = self._scheduled
        if previous is not None and not previous.started:
            self._supersede(previous, "observation")
        scheduled = _Scheduled(
            cycle=cycle.cycle, tab=cycle.tab, requested_at=cycle.committed_at + delay
        )
        scheduled.task = self._spawn(
            self._after_observation(scheduled), f"browser.frames after cycle {cycle.cycle}"
        )
        self._scheduled = scheduled

    async def on_action_boundary(self, boundary: ActionBoundary) -> None:
        """Resolve a waiting ``after_observation`` frame, then capture before the action.

        The whole wait is bounded by ``max_action_wait_s``. Cancelled (the
        archive run's budget ran out, or the run itself was cancelled), the
        frame is still listed — ``timeout``, with how long the action waited.
        """
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.archiver.max_action_wait_s
        owed = self.archiver.before_action
        try:
            await self._release_scheduled(boundary, deadline)
            if not owed:
                return
            shot = await self._before_action(boundary, deadline)
            owed = False
            self._resolve(self._before_record(boundary, shot), shot.data)
        except asyncio.CancelledError:
            if owed:
                waited = max(0.0, self.ctx.clock() - boundary.requested_at)
                shot = _Shot("timeout", detail=f"the action stopped waiting after {waited:.2f}s")
                self._resolve(self._before_record(boundary, shot), None)
            raise

    async def _release_scheduled(self, boundary: ActionBoundary, deadline: float) -> None:
        """Supersede the waiting frame, or take it first when it came due before the action."""
        scheduled = self._scheduled
        if scheduled is None:
            return
        if not scheduled.started:
            if self.ctx.cycle() != scheduled.cycle:
                self._supersede(scheduled, "observation")
                return
            if boundary.requested_at < scheduled.requested_at:
                self._supersede(scheduled, "action")
                return
        task = scheduled.task
        if task is None or task.done():
            return
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            if remaining > 0:
                await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            if not task.done():
                self._abandon(scheduled, "the action stopped waiting for it")
            raise
        if not task.done():
            self._abandon(
                scheduled,
                f"not captured within the action's wait ({self.archiver.max_action_wait_s:g}s)",
            )

    async def _before_action(self, boundary: ActionBoundary, deadline: float) -> _Shot:
        tab = boundary.target_id or ""
        if not boundary.tab_open:
            return _Shot(
                "no_tab",
                detail=(
                    f"the tab the action was for ({tab}) closed itself before it"
                    if tab
                    else "no tab was focused"
                ),
            )
        timeouts = self._timeouts.get(tab, 0)
        if timeouts >= TIMEOUTS_BEFORE_NOT_WAITING:
            self._probe(tab)
            return _Shot(
                "timeout",
                detail=(
                    f"not waited for: the last {timeouts} captures of this tab timed out; "
                    f"actions wait again after a capture succeeds"
                ),
            )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return _Shot(
                "timeout",
                detail=(
                    f"the action's wait ({self.archiver.max_action_wait_s:g}s) was spent on "
                    f"the after_observation frame"
                ),
            )
        if remaining >= self.archiver.capture_timeout_s:
            return await self._shoot(tab, self.archiver.capture_timeout_s)
        shot = await self._shoot(tab, remaining)
        if shot.outcome != "timeout":
            return shot
        return _Shot(
            "timeout",
            detail=f"no screenshot within the action's wait ({self.archiver.max_action_wait_s:g}s)",
        )

    async def _after_observation(self, scheduled: _Scheduled) -> None:
        """Wait until due, then capture. A canceller records what became of it."""
        try:
            wait = scheduled.requested_at - self.ctx.clock()
            if wait > 0:
                await asyncio.sleep(wait)
            scheduled.started = True
            if self.ctx.cycle() != scheduled.cycle:
                # A new observation began before this frame was due.
                self._resolve(self._superseded_record(scheduled, "observation"), None)
                return
            shot = await self._shoot(scheduled.tab, self.archiver.capture_timeout_s)
            self._resolve(self._after_record(scheduled, shot), shot.data)
        finally:
            if self._scheduled is scheduled:
                self._scheduled = None

    def _supersede(self, scheduled: _Scheduled, by: FrameSuperseder) -> None:
        """Cancel a frame that has not started and list it as superseded."""
        if scheduled.task is not None:
            scheduled.task.cancel()
        if self._scheduled is scheduled:
            self._scheduled = None
        self._resolve(self._superseded_record(scheduled, by), None)

    def _abandon(self, scheduled: _Scheduled, detail: str) -> None:
        """Cancel a frame an action could not wait for any longer and list it as ``timeout``."""
        if scheduled.task is not None:
            scheduled.task.cancel()
        if self._scheduled is scheduled:
            self._scheduled = None
        self._count_timeout(scheduled.tab)
        self._resolve(self._after_record(scheduled, _Shot("timeout", detail=detail)), None)

    def _probe(self, tab: str) -> None:
        """One background capture of a tab actions no longer wait on; a success resumes waiting."""
        running = self._probes.get(tab)
        if running is not None and not running.done():
            return

        async def probe() -> None:
            await self._shoot(tab, self.archiver.capture_timeout_s)

        self._probes[tab] = self._spawn(probe(), f"browser.frames probe of {tab}")

    def _spawn(self, coro: Any, name: str) -> "asyncio.Task[None]":
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── the capture ────────────────────────────────────────────────────

    async def _shoot(self, tab: str, timeout: float) -> _Shot:
        """One screenshot of *tab* on the recorder connection; the tab's timeouts are counted.

        Never raises for a failed capture: a tab the recorder has no session
        for, a timeout and any other failure each come back with that outcome
        and a ``detail``. Cancellation propagates.
        """
        shot = await self._capture(tab, timeout)
        if shot.outcome == "timeout":
            self._count_timeout(tab)
        elif shot.outcome == "captured":
            self._timeouts.pop(tab, None)
        return shot

    async def _capture(self, tab: str, timeout: float) -> _Shot:
        session = self.ctx.recorder.session_for_target(tab) if tab else None
        if session is None:
            return _Shot(
                "no_tab",
                detail=f"the recorder connection has no session for tab {tab or '(unknown)'}",
            )
        try:
            reply = await asyncio.wait_for(
                self.ctx.recorder.send(
                    CAPTURE_METHOD,
                    dict(CAPTURE_PARAMS),
                    session,
                    timeout=timeout + CLIENT_TIMEOUT_MARGIN_S,
                ),
                timeout,
            )
            data = _png_bytes(reply)
        except TimeoutError:
            return _Shot("timeout", detail=f"no screenshot within {timeout:g}s")
        except Exception as exc:
            return _Shot("failed", detail=f"{type(exc).__name__}: {exc}")
        return _Shot("captured", data=data, captured_at=self.ctx.clock())

    def _count_timeout(self, tab: str) -> None:
        self._timeouts[tab] = self._timeouts.get(tab, 0) + 1

    # ── index entries ──────────────────────────────────────────────────

    def _before_record(self, boundary: ActionBoundary, shot: _Shot) -> FrameRecord:
        return self._record(
            kind="before_action",
            cycle=boundary.cycle,
            action_index=boundary.action_index,
            action=boundary.action,
            tab=boundary.target_id or "",
            requested_at=boundary.requested_at,
            released_at=self.ctx.clock(),
            shot=shot,
        )

    def _after_record(self, scheduled: _Scheduled, shot: _Shot) -> FrameRecord:
        return self._record(
            kind="after_observation",
            cycle=scheduled.cycle,
            action_index=None,
            action=None,
            tab=scheduled.tab,
            requested_at=scheduled.requested_at,
            released_at=None,
            shot=shot,
        )

    def _record(
        self,
        *,
        kind: FrameKind,
        cycle: int,
        action_index: int | None,
        action: str | None,
        tab: str,
        requested_at: float,
        released_at: float | None,
        shot: _Shot,
    ) -> FrameRecord:
        data = shot.data
        width = height = None
        if data is not None:
            measured = image_size(data)
            if measured != (0, 0):
                width, height = measured
        return FrameRecord(
            kind=kind,
            cycle=cycle,
            action_index=action_index,
            action=action,
            tab=tab,
            requested_at=requested_at,
            captured_at=shot.captured_at,
            released_at=released_at,
            outcome=shot.outcome,
            superseded_by=None,
            name=frame_name(kind, cycle, action_index) if data is not None else None,
            sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
            width=width,
            height=height,
            detail=shot.detail,
        )

    def _superseded_record(self, scheduled: _Scheduled, by: FrameSuperseder) -> FrameRecord:
        return FrameRecord(
            kind="after_observation",
            cycle=scheduled.cycle,
            action_index=None,
            action=None,
            tab=scheduled.tab,
            requested_at=scheduled.requested_at,
            captured_at=None,
            released_at=None,
            outcome="superseded",
            superseded_by=by,
            name=None,
            sha256=None,
            width=None,
            height=None,
            detail=None,
        )

    def _resolve(self, record: FrameRecord, data: bytes | None) -> None:
        self._resolved.append(_Resolved(record, data, self.ctx.clock()))

    # ── hand-over ──────────────────────────────────────────────────────

    async def collect(self) -> list[ArchiveFile]:
        """Let a waiting ``after_observation`` frame finish, then hand over and forget."""
        await self._settle_scheduled()
        resolved, self._resolved = self._resolved, []
        if not resolved:
            return []
        files = [self._image(item) for item in resolved if item.data is not None]
        files.append(self._index(resolved))
        return files

    async def _settle_scheduled(self) -> None:
        """Wait for the waiting frame: at most the rest of its delay plus ``capture_timeout_s``."""
        scheduled = self._scheduled
        if scheduled is None or scheduled.task is None or scheduled.task.done():
            return
        if not scheduled.started and self.ctx.cycle() != scheduled.cycle:
            self._supersede(scheduled, "observation")
            return
        remaining = max(0.0, scheduled.requested_at - self.ctx.clock())
        await asyncio.wait({scheduled.task}, timeout=remaining + self.archiver.capture_timeout_s)

    def _image(self, item: _Resolved) -> ArchiveFile:
        record = item.record
        assert item.data is not None and record.name is not None
        assert record.captured_at is not None
        return ArchiveFile(
            archiver_id=self.archiver.archiver_id,
            name=record.name,
            media_type="image/png",
            data=item.data,
            meta=ArchiveProvenance(
                captured_at=record.captured_at,
                cycle=record.cycle,
                ids={"tab": record.tab, "kind": record.kind},
            ),
            content_addressed=True,
        )

    def _index(self, resolved: Sequence[_Resolved]) -> ArchiveFile:
        document = {"format": FRAMES_FORMAT, "frames": [asdict(item.record) for item in resolved]}
        return ArchiveFile(
            archiver_id=self.archiver.archiver_id,
            name=FRAMES_INDEX_NAME,
            media_type="application/json",
            data=json.dumps(
                document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8"),
            meta=ArchiveProvenance(captured_at=max(item.resolved_at for item in resolved)),
        )

    async def aclose(self) -> None:
        """Stop every pending frame and probe; nothing is captured after this. Idempotent."""
        self._closed = True
        self._scheduled = None
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(
                tasks, timeout=self.archiver.capture_timeout_s + CLIENT_TIMEOUT_MARGIN_S
            )


def _png_bytes(reply: Any) -> bytes:
    """The image a ``Page.captureScreenshot`` reply carries; raise when it carries none."""
    encoded = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError(f"{CAPTURE_METHOD} returned no image data")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"{CAPTURE_METHOD} returned undecodable image data: {exc}") from exc
    if not data:
        raise RuntimeError(f"{CAPTURE_METHOD} returned no image data")
    return data


# ── readers ───────────────────────────────────────────────────────────


class _FramesIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["browser-frames-2"]
    frames: list[FrameRecord]


def _parse_index(data: bytes, *, where: str) -> list[FrameRecord]:
    try:
        return _FramesIndex.model_validate_json(data).frames
    except ValidationError as exc:
        raise ValueError(f"{where} is not a {FRAMES_FORMAT} frames index: {exc}") from exc


def read_frames(reader: ArchiveReader, step: int) -> list[FrameRecord]:
    """The frames *step*'s commit took, in the order they were resolved; ``[]`` when none."""
    for entry in reader.archive_files(step, FRAMES_ARCHIVER_ID):
        if entry.name == FRAMES_INDEX_NAME:
            return _parse_index(reader.read_extra(entry.path), where=entry.path)
    return []


def iter_frames(
    reader: ArchiveReader, *, include_trailing: bool = True
) -> Iterator[tuple[int | None, FrameRecord]]:
    """``(step, frame)`` for every committed frame, in step order; trailing ones as ``None``."""
    for step, entry in reader.iter_archive(FRAMES_ARCHIVER_ID, include_trailing=include_trailing):
        if entry.name != FRAMES_INDEX_NAME:
            continue
        for record in _parse_index(reader.read_extra(entry.path), where=entry.path):
            yield step, record


def before_action_frames(
    reader: ArchiveReader,
    step: int,
    *,
    action_times: Sequence[tuple[float, float]] | None,
) -> list[tuple[int | None, FrameRecord]]:
    """Frames of the actual action calls, independent of which commit collected them.

    The caller supplies measured ``env.act`` spans in call order. A frame's
    own action ordinal and request time identify the call. If timing was not
    recorded, a uniquely recorded raw-DOM cycle can identify the observation
    instead; an unknown or ambiguous cycle is never guessed from a step number.
    """
    cycle: int | None = None
    if action_times is None:
        cycles = {
            entry.cycle
            for entry in reader.archive_files(step, "browser.dom_raw")
            if entry.cycle is not None
        }
        if len(cycles) != 1:
            return []
        cycle = cycles.pop()
    found = []
    for at, frame in iter_frames(reader, include_trailing=True):
        if frame.kind != "before_action":
            continue
        if action_times is not None:
            index = frame.action_index
            if index is None or not 0 <= index < len(action_times):
                continue
            started, ended = action_times[index]
            if not started <= frame.requested_at <= ended:
                continue
        elif frame.cycle != cycle:
            continue
        found.append((at, frame))
    return sorted(found, key=lambda item: (item[1].action_index or 0, item[1].requested_at))


def read_frame_image(reader: ArchiveReader, step: int | None, record: FrameRecord) -> bytes:
    """The PNG of a captured *record* listed by *step*'s index (``None``: a trailing batch).

    Raises:
        ValueError: *record* was not captured, so it has no image.
        KeyError: *step*'s ``browser.frames`` files hold no image of that name and digest.
    """
    if record.outcome != "captured" or record.name is None or record.sha256 is None:
        raise ValueError(
            f"the {record.kind} frame of cycle {record.cycle} has no image "
            f"(outcome {record.outcome})"
        )
    entries: Sequence[ArchiveEntry] = (
        reader.archive_files(step, FRAMES_ARCHIVER_ID)
        if step is not None
        else [
            entry
            for entry_step, entry in reader.iter_archive(FRAMES_ARCHIVER_ID, include_trailing=True)
            if entry_step is None
        ]
    )
    for entry in entries:
        if entry.name == record.name and entry.sha256 == record.sha256:
            return reader.read_extra(entry.path)
    where = f"step {step}" if step is not None else "the trailing batches"
    raise KeyError(f"{where} holds no browser.frames image {record.name!r} ({record.sha256})")
