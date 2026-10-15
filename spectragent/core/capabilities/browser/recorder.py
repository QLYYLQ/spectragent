"""The recorder connection: a second, browser-level CDP client (§4.14).

``BrowserCapability`` talks to Chrome over its *control* connection: tab
attach, observation, actions, viewport. Everything that must happen **before
a new target runs its first request or script** — the ``replayability``
shim and overrides, Fetch interception for the network archiver — happens
here instead, on a separate :class:`CDPClient` that sets browser-level
``Target.setAutoAttach{waitForDebuggerOnStart: true}`` and prepares every
target while it is paused. Why a second connection, and the Chrome facts
every rule below rests on, are in ``docs/architecture_design.md`` §4.14
(measured on Chrome 153).

This module is the CDP choreography and nothing else. It does not know what
any participant records or pins; it asks each :class:`RecorderParticipant`
what to send to a target and who owns a paused request:

- **Pause-bearing events** (``Target.attachedToTarget`` of a waiting target,
  ``Fetch.requestPaused``) are handled in tasks spawned straight from the
  client callback — never through an agent-cdp scope queue, whose bounded
  admission may reject an event and a rejected pause wedges the page. Every waiting
  target is resumed and every paused request settled in ``finally``, by
  this module, whatever the participant did: continued while recording;
  failed, **never continued**, at the Request stage while a participant
  serves requests itself (M30's replay server,
  :attr:`RecorderParticipant.serves_requests`) — a replay sends nothing to
  the network, whoever timed out or raised.
- **Metadata events** go, with their session id, through a local dispatcher
  into the browser-level archive scope as :class:`RecorderEvent`; the
  :class:`ArchiveChannel` counts what it emitted against what was handled,
  so a dropped event fails the next collection instead of passing silently.
  Which CDP events those are is each participant's own declaration
  (:meth:`RecorderParticipant.event_methods`); the recorder subscribes to
  their union and knows no event of its own beyond target bookkeeping. There
  is no channel in shim-only mode, so no metadata handler at all.
- **Bytes never travel in events** (archive scope retains no history): the
  dispatcher hands each raw event to the participants that declared it
  (:meth:`RecorderParticipant.admit_event`, synchronous, in the receive loop)
  and emits only what each one admits — small metadata, addressed to that
  participant; bodies and frame payloads go to the participant's own side
  store, and values it never stores (credentials) never enter the scope.

- **A waiting target is resumed without waiting for its replies.** Its whole
  preparation goes on the wire in order, then ``Runtime.runIfWaitingForDebugger``,
  and only then are the replies awaited — the resume's last. A target Chrome
  builds without a page process (a tab its opener cannot reach, a waiting
  service worker) answers its page-side commands only once it runs and its
  first document commits, yet applies them, queued in order, before that
  document's first script (measured, Chrome 155, arch §4.14). Waiting for the
  replies before the resume would hold such a tab for the whole timeout and
  record every page-side command as failed. A reply that never arrives is
  recorded on the target, never raised.

Resume uses agent-cdp ``PausedTarget`` mode B so preparation commands are
written before the session-routed resume. The callback awaits acknowledgement
before clearing the waiting marker. In agent-cdp 0.5 mode A also routes the
command correctly; the previous browser-level command left this target paused.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from agent_cdp import BaseEvent, EventScope, PausedTarget, RequestContract
from pydantic import Field

logger = logging.getLogger(__name__)

#: Target types the recorder prepares. Anything else (``browser_ui``,
#: ``background_page``, ``tab``, an extension's ``service_worker`` …) is
#: resumed and detached: browser-level auto-attach attaches Chrome's own
#: targets too, and leaving one waiting wedges it.
PREPARED_TARGET_TYPES = frozenset({"page", "iframe", "worker", "shared_worker", "service_worker"})
#: Targets with a document: where frame commands (``Page.*``, init scripts,
#: ``Emulation.*``) go.
FRAME_TARGET_TYPES = frozenset({"page", "iframe"})
#: Worker targets: no ``Page`` domain; the shim is a ``Runtime.evaluate``.
WORKER_TARGET_TYPES = frozenset({"worker", "shared_worker", "service_worker"})
#: Targets with a ``Fetch`` domain. A dedicated worker has none: its requests
#: pause on the parent page's session.
FETCH_TARGET_TYPES = frozenset({"page", "iframe", "shared_worker", "service_worker"})
#: URLs of Chrome-internal targets, prepared as never.
INTERNAL_URL_PREFIXES = ("chrome:", "chrome-extension:", "devtools:")
#: The resume of a waiting target.
RESUME_METHOD = "Runtime.runIfWaitingForDebugger"
#: Seconds a resumed target's replies may take beyond the longest the recorder
#: itself may hold a paused request. A target without a page process answers
#: once its first document commits, and that waits for the network and for
#: the Response-stage pause of that very document (its owner's budget plus
#: :data:`PAUSE_OWNER_GRACE_S`, or the recorder's default); measured up to
#: 3.75 s after the resume on a live site (Chrome 155, arch §4.14).
RESUMED_REPLY_MARGIN_S = 15.0

#: The recorder's auto-attach, browser-level and on every prepared target.
AUTO_ATTACH_PARAMS: Mapping[str, Any] = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
}
#: Seconds a pause's owner gets beyond its own declared budget
#: (:attr:`RecorderParticipant.pause_budget_s`) before the recorder settles
#: the request without it.
PAUSE_OWNER_GRACE_S = 1.0
#: How the recorder settles a Request-stage pause nobody answered while a
#: participant serves requests: ``Failed``, the reason that keeps Chrome's
#: http fallback alive (a 404 or ``BlockedByClient`` cuts it off — measured, arch §4.14).
UNANSWERED_ERROR_REASON = "Failed"

#: The first step of cleanup: stop pausing new targets.
RELEASE_AUTO_ATTACH_PARAMS: Mapping[str, Any] = {
    "autoAttach": False,
    "waitForDebuggerOnStart": False,
    "flatten": True,
}

#: The recorder's own target bookkeeping: emitted by the recorder itself, in
#: order with each target's preparation, to every participant. A participant
#: never declares these (:meth:`RecorderParticipant.event_methods`).
TARGET_BOOKKEEPING = frozenset({"Target.attachedToTarget", "Target.detachedFromTarget"})


class RecorderLostError(RuntimeError):
    """The recorder connection ended while the run still needed it.

    Reported the moment
    the loss is pushed: archive collection must report the gap. Execution decides separately
    whether this connection also enforced replay or fixed target configuration.
    """


class RecorderError(RuntimeError):
    """The recorder could not be brought up (open() failed its ready barrier)."""


class ArchiveChannelError(RuntimeError):
    """The archive scope lost metadata: a collection must not pretend completeness."""


# ── Value types ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CDPCommand:
    """One command a participant wants sent to a target."""

    method: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecorderTarget:
    """A target as the recorder saw it attach (``Target.attachedToTarget``)."""

    session_id: str
    target_id: str
    type: str
    url: str = ""
    opener_id: str | None = None
    waiting: bool = False
    #: The session it was auto-attached under; ``None`` at browser level.
    parent_session_id: str | None = None
    #: Chrome's storage/target partition, independent of any run's session ids.
    browser_context_id: str | None = None

    @classmethod
    def from_attached(cls, params: Mapping[str, Any], parent: str | None) -> "RecorderTarget":
        info = params.get("targetInfo") or {}
        return cls(
            session_id=str(params.get("sessionId") or ""),
            target_id=str(info.get("targetId") or ""),
            type=str(info.get("type") or ""),
            url=str(info.get("url") or ""),
            opener_id=info.get("openerId"),
            waiting=bool(params.get("waitingForDebugger")),
            parent_session_id=parent,
            browser_context_id=info.get("browserContextId"),
        )

    @property
    def internal(self) -> bool:
        """Chrome's own target: resumed and detached, never prepared."""
        return self.type not in PREPARED_TARGET_TYPES or self.url.startswith(INTERNAL_URL_PREFIXES)


@dataclass(slots=True)
class PausedRequest:
    """One ``Fetch.requestPaused``, handed to the participant owning its stage.

    ``params`` is the raw event (the POST body may be in it twice; it never
    goes into an event). The recorder settles the request on the pausing
    session in ``finally`` (:meth:`RecorderConnection._settle`); an owner that
    answers the request itself (``Fetch.fulfillRequest`` / ``failRequest`` —
    M30's replay) sets ``answered`` **after its send succeeded**, so the
    recorder neither answers it again nor leaves a failed send unanswered.
    """

    session_id: str
    request_id: str
    stage: Literal["Request", "Response"]
    params: dict[str, Any]
    target: RecorderTarget | None = None
    answered: bool = False
    #: The response/failure/continuation was acknowledged by Chrome.
    settled: bool = False
    _settlement_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def network_id(self) -> str | None:
        value = self.params.get("networkId")
        return str(value) if value is not None else None

    @property
    def url(self) -> str:
        return str((self.params.get("request") or {}).get("url") or "")


@dataclass(frozen=True, slots=True)
class CycleRequest:
    """An observation cycle of one tab, for participants that want cycles.

    Delivered from the tab's observation scope through the archive scope,
    inside the cycle (the observation waits for it).
    """

    #: The tab's target id — the recorder's session for it is found by this.
    target_id: str | None
    #: The control connection's session for the tab.
    control_session_id: str
    #: The fragments this cycle negotiated.
    fragments: frozenset[str] = frozenset()
    #: Observation request identity, retained across asynchronous self-capture.
    request_id: str = ""


class RecorderEvent(BaseEvent[None]):
    """A metadata event from the recorder connection, with its session id.

    Small by contract — the archive scope keeps no event history: what a participant admitted (:meth:`RecorderParticipant.admit_event`),
    never a body, a frame payload or a credential.
    """

    session_id: str | None = None
    method: str
    params: dict[str, Any] = {}
    #: The participant that admitted it, and the only one it is delivered to;
    #: ``None`` for the recorder's own target bookkeeping, which every
    #: participant receives.
    consumer: Any = Field(default=None, exclude=True, repr=False)


# ── Participants ──────────────────────────────────────────────────────


@runtime_checkable
class RecorderParticipant(Protocol):
    """Whoever contributes to a target's preparation or consumes its traffic.

    The ``replayability`` plan of the run is one participant; every bound
    archiver's run-time session is another. They do not know each other and
    the recorder does not branch on what they are.
    """

    @property
    def wants_cycles(self) -> bool:
        """Whether :meth:`on_cycle` should be called for observation cycles."""
        ...

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        """Commands for ``target`` before it resumes, in order."""
        ...

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        """``Fetch.RequestPattern``s; merged into one ``Fetch.enable``."""
        ...

    def event_methods(self) -> Collection[str]:
        """The CDP events (``Domain.event``) this participant consumes.

        The recorder subscribes to the union over the participants present at
        :meth:`RecorderConnection.open` — like the Fetch patterns, what is
        captured is fixed when the recorder opens. Never one of
        :data:`TARGET_BOOKKEEPING`, which every participant receives anyway.
        """
        ...

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        """What of one raw CDP event enters the archive scope for this participant.

        Called by the recorder's local dispatcher for every event in
        :meth:`event_methods` — synchronously, in the client's receive loop,
        before anything is emitted. Archive scopes retain no event history, so the returned event carries small metadata only: bytes
        (bodies, frame payloads) go to this participant's own side store,
        keyed by the returned event's ``event_id``, and values it never
        stores (credentials) are left out. ``None`` admits nothing. *params*
        is shared with the other participants: never modify it.
        """
        ...

    def on_event(self, event: RecorderEvent) -> None:
        """An event it admitted, or target bookkeeping, on the archive scope's loop. Synchronous."""
        ...

    @property
    def pause_budget_s(self) -> float | None:
        """Most seconds :meth:`on_paused` may take, or ``None`` for the recorder's default.

        The recorder waits this long plus :data:`PAUSE_OWNER_GRACE_S` before it
        gives up on the owner and settles the request anyway — so a budget
        the owner's own knobs set (a body read's timeout) is the one honoured.
        """
        ...

    @property
    def serves_requests(self) -> bool:
        """Whether this participant answers Request-stage pauses itself (M30's replay server).

        While any participant present at :meth:`RecorderConnection.open`
        serves requests, the recorder settles every Request-stage pause that
        was not answered — the owner timed out or raised, no owner, no
        prepared target, still held at :meth:`RecorderConnection.close` — with
        ``Fetch.failRequest``, never ``continueRequest``: a replay never
        sends a request to the network.
        """
        ...

    async def on_paused(self, paused: PausedRequest) -> None:
        """A paused request at a stage this participant's patterns asked for."""
        ...

    async def on_cycle(self, cycle: CycleRequest) -> None:
        """An observation cycle (only when :attr:`wants_cycles`)."""
        ...


class RecorderParticipantBase:
    """No-op defaults for every :class:`RecorderParticipant` member."""

    @property
    def wants_cycles(self) -> bool:
        return False

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        return ()

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        return ()

    def event_methods(self) -> Collection[str]:
        return ()

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        return None

    def on_event(self, event: RecorderEvent) -> None:
        return None

    @property
    def pause_budget_s(self) -> float | None:
        return None

    @property
    def serves_requests(self) -> bool:
        return False

    async def on_paused(self, paused: PausedRequest) -> None:
        return None

    async def on_cycle(self, cycle: CycleRequest) -> None:
        return None


ParticipantsAccessor = Callable[[], Sequence[RecorderParticipant]]


class ArchiveChannel:
    """The archive scope's public completion and completeness boundary.

    Native flush fixes the admitted prefix without occupying a queue slot.
    Delivery outcomes distinguish refusal, cancellation and handler failure;
    AF additionally counts metadata and participant failures.
    """

    def __init__(self, scope: EventScope, participants: ParticipantsAccessor) -> None:
        self.scope = scope
        self._participants = participants
        self.emitted = 0
        self.handled = 0
        self.errors = 0

    def participants(self) -> tuple[RecorderParticipant, ...]:
        return tuple(self._participants())

    def emit(self, event: RecorderEvent) -> None:
        self.emitted += 1
        event.request_contract = RequestContract(min_receivers=1)
        try:
            self.scope.emit(event)
        except Exception:
            self.errors += 1
            raise

    def mark_handled(self) -> None:
        self.handled += 1

    async def flush(self, timeout: float = 5.0) -> None:
        """Settle the accepted prefix and report every missing delivery."""
        try:
            prefix = await self.scope.flush(timeout=timeout)
        except TimeoutError as exc:
            stats = self.scope.stats
            raise ArchiveChannelError(
                f"archive scope flush did not complete within {timeout:g}s "
                f"(queued={stats.queued}, running={stats.running}, "
                f"rejected={stats.rejected}, cancelled={stats.cancelled})"
            ) from exc
        if prefix.failures:
            details = "; ".join(
                f"{delivery.event_type}/{delivery.connection_id}: {delivery.status}"
                + (f" ({delivery.error_message})" if delivery.error_message else "")
                for delivery in prefix.failures
            )
            raise ArchiveChannelError(f"archive deliveries incomplete: {details}")


# ── The connection ────────────────────────────────────────────────────


class PendingCommand(Protocol):
    """A command already on the wire (``cdp_client.PendingReply``)."""

    async def wait(self, timeout: float | None = None) -> Any: ...

    def abandon(self) -> None: ...


class RecorderClient(Protocol):
    """The slice of :class:`CDPClient` the recorder uses."""

    @property
    def is_connected(self) -> bool: ...

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...

    async def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> PendingCommand: ...

    def on_event(self, method: str, callback: Callable[..., Any]) -> None: ...

    def off_event(self, method: str, callback: Callable[..., Any]) -> None: ...

    def add_close_callback(self, callback: Callable[[BaseException | None], None]) -> None: ...

    async def stop(self) -> None: ...


class RecorderConnection:
    """Browser-level early attach on its own client; see the module docstring.

    Lifecycle: construct around a started client → :meth:`open` (auto-attach
    + ready barrier) → … → :meth:`close` (fixed-order cleanup, idempotent,
    safe on a half-built instance). ``on_lost`` is called once if the client
    dies on its own; :meth:`close` stopping it is not a loss.
    """

    def __init__(
        self,
        client: RecorderClient,
        *,
        participants: ParticipantsAccessor,
        channel: ArchiveChannel | None,
        on_lost: Callable[[RecorderLostError], None],
        command_timeout_s: float = 1.0,
        pause_owner_timeout_s: float = 15.0,
        ready_timeout_s: float = 10.0,
        resumed_reply_margin_s: float = RESUMED_REPLY_MARGIN_S,
    ) -> None:
        self._client = client
        self._participants = participants
        self._channel = channel
        self._on_lost = on_lost
        self._command_timeout_s = command_timeout_s
        self._pause_owner_timeout_s = pause_owner_timeout_s
        self._ready_timeout_s = ready_timeout_s
        self._resumed_reply_margin_s = resumed_reply_margin_s

        #: Prepared targets by session.
        self._targets: dict[str, RecorderTarget] = {}
        #: target id → its first prepared session.
        self._prepared_target_ids: dict[str, str] = {}
        #: Every session attached to this client, prepared or not.
        self._sessions: set[str] = set()
        #: Sessions attached waiting and not yet resumed.
        self._waiting: set[str] = set()
        self._cleanup_failures: list[str] = []
        self._close_task: asyncio.Task[None] | None = None
        #: Sessions a ``Fetch.enable`` was sent to.
        self._fetch_sessions: set[str] = set()
        #: Failed commands, per session (diagnostics; never fatal by itself).
        self.failures: dict[str, list[str]] = {}
        #: target id → set once its first session's preparation has every
        #: reply in (or given up on): :meth:`wait_prepared`.
        self._prepared: dict[str, asyncio.Event] = {}

        self._attach_tasks: set[asyncio.Task[None]] = set()
        self._pause_tasks: set[asyncio.Task[None]] = set()
        self._held: dict[tuple[str, str], PausedRequest] = {}
        self._registrations: list[tuple[str, Callable[..., Any]]] = []
        self._auto_attach_set = False
        self._opened = False
        #: A participant serves requests (fixed at :meth:`open`, like the
        #: Fetch patterns): unanswered Request-stage pauses fail, never continue.
        self._serving = False
        self._closing = False
        self._closed = False
        self._lost: RecorderLostError | None = None
        self._browser_version: dict[str, Any] | None = None

        client.add_close_callback(self._on_client_closed)

    # ── public surface ────────────────────────────────────────────────

    @property
    def lost(self) -> RecorderLostError | None:
        return self._lost

    @property
    def channel(self) -> ArchiveChannel | None:
        return self._channel

    def targets(self) -> dict[str, RecorderTarget]:
        """Prepared targets by recorder session id (a copy)."""
        return dict(self._targets)

    def target(self, session_id: str) -> RecorderTarget | None:
        """The prepared target attached as ``session_id``, if any (no copy)."""
        return self._targets.get(session_id)

    def session_for_target(self, target_id: str) -> str | None:
        """The first prepared session of ``target_id`` (see :meth:`sessions_for_target`)."""
        return self._prepared_target_ids.get(target_id)

    def sessions_for_target(self, target_id: str) -> list[str]:
        """Every prepared session of ``target_id``, in attach order."""
        return [sid for sid, target in self._targets.items() if target.target_id == target_id]

    async def wait_prepared(self, target_id: str, timeout: float) -> bool:
        """Wait until ``target_id`` is prepared: attached, its batch answered, resumed.

        For a target the capability created itself (``create_tab``): its
        ``Target.createTarget`` reply on the control connection may arrive
        before this connection even saw the target attach, and a navigation
        started before its ``Fetch.enable`` took effect would escape it.
        ``False`` when the preparation did not finish within ``timeout``.
        """
        event = self._prepared.setdefault(target_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send on the recorder client (archivers' collect-time reads)."""
        return await self._client.send(
            method,
            params,
            session_id,
            timeout=self._command_timeout_s if timeout is None else timeout,
        )

    async def browser_version(self) -> dict[str, Any]:
        """``Browser.getVersion`` on the recorder client, cached."""
        if self._browser_version is None:
            reply = await self._client.send(
                "Browser.getVersion", None, None, timeout=self._command_timeout_s
            )
            self._browser_version = dict(reply or {})
        return dict(self._browser_version)

    async def open(self) -> None:
        """Browser-level auto-attach, then wait for the existing targets.

        Chrome attaches every existing target (not waiting) before it
        answers ``setAutoAttach``, so the attach tasks spawned by then are
        exactly the existing targets; :meth:`open` returns once each of them
        is prepared (the ready barrier) or raises.
        """
        if self._opened:
            raise RuntimeError("RecorderConnection.open() called twice")
        self._opened = True
        self._serving = any(participant.serves_requests for participant in self._participants())
        self._register_callbacks()
        await self._client.send(
            "Target.setAutoAttach",
            dict(AUTO_ATTACH_PARAMS),
            None,
            timeout=self._command_timeout_s,
        )
        self._auto_attach_set = True
        existing = list(self._attach_tasks)
        if not existing:
            return
        _done, pending = await asyncio.wait(existing, timeout=self._ready_timeout_s)
        if pending:
            raise RecorderError(
                f"recorder: {len(pending)} existing target(s) were not prepared within "
                f"{self._ready_timeout_s:g}s"
            )

    @property
    def cleanup_failures(self) -> tuple[str, ...]:
        """Unconfirmed cleanup operations, without claiming they rolled back."""
        return tuple(self._cleanup_failures)

    async def close(self) -> None:
        """Finish owned cleanup before propagating cancellation of its caller."""
        if self._closed:
            return
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close_resources())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError as cancellation:
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                self._close_task.result()
            except BaseException as failure:
                cancellation.add_note(f"Recorder cleanup also failed: {failure!r}")
            raise

    async def _close_resources(self) -> None:
        """Release the browser in a fixed order; idempotent; tolerant of each step.

        1. stop pausing new targets (``setAutoAttach{false, false}``);
        2. settle every request still held (continued — or failed, at the
           Request stage, while a participant serves requests), resume every
           target still waiting;
        3. ``Fetch.disable`` on every session it was enabled on;
        4. detach every session;
        5. cancel and await the remaining tasks — only now, after every held
           request was settled;
        6. stop the client.

        An unclean disconnect while holding browser-level
        ``waitForDebuggerOnStart`` wedges every new target in the browser
        (measured, arch §4.14), so nothing here may be skipped because an earlier step failed.
        """
        if self._closed:
            return
        self._closing = True
        t = self._command_timeout_s

        if self._auto_attach_set:
            await self._quietly(
                "Target.setAutoAttach", dict(RELEASE_AUTO_ATTACH_PARAMS), None, timeout=t
            )

        for paused in list(self._held.values()):
            await self._settle(paused)
        for sid in list(self._waiting):
            # Tolerant like every other step: a target that went away, or a
            # browser too busy to answer in time, must not leave the rest of
            # the browser paused, Fetch-enabled and attached.
            if await self._quietly(RESUME_METHOD, None, sid, timeout=t):
                self._waiting.discard(sid)

        for sid in list(self._fetch_sessions):
            await self._quietly("Fetch.disable", {}, sid, timeout=t)
        self._fetch_sessions.clear()

        for sid in list(self._sessions):
            await self._quietly("Target.detachFromTarget", {"sessionId": sid}, None, timeout=t)
        self._sessions.clear()

        tasks = [*self._attach_tasks, *self._pause_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=t)

        for method, callback in self._registrations:
            try:
                self._client.off_event(method, callback)
            except Exception as exc:
                self._cleanup_failures.append(f"unsubscribe {method}: {exc}")
        self._registrations.clear()

        try:
            await self._client.stop()
        except Exception as exc:  # pragma: no cover — defensive
            self._cleanup_failures.append(f"client stop: {exc}")
            raise RecorderError("recorder client cleanup failed") from exc
        finally:
            if self._cleanup_failures:
                logger.warning(
                    "recorder cleanup unconfirmed: %s", "; ".join(self._cleanup_failures)
                )
        pending = [task for task in tasks if not task.done()]
        if pending:
            _, pending_set = await asyncio.wait(pending, timeout=t)
            if pending_set:
                raise RecorderError(
                    f"recorder cleanup has {len(pending_set)} unfinished task(s) after cancellation"
                )
        self._closed = True

    # ── callbacks (sync: they run in the client's receive loop) ───────

    def _register_callbacks(self) -> None:
        self._listen("Target.attachedToTarget", self._on_attached)
        self._listen("Target.detachedFromTarget", self._on_detached)
        self._listen("Fetch.requestPaused", self._on_paused)
        if self._channel is not None:
            declared = {
                method
                for participant in self._participants()
                for method in participant.event_methods()
            }
            for method in sorted(declared - TARGET_BOOKKEEPING):
                self._listen(method, self._forwarder(method))

    def _listen(self, method: str, callback: Callable[..., Any]) -> None:
        self._client.on_event(method, callback)
        self._registrations.append((method, callback))

    def _forwarder(self, method: str) -> Callable[[dict[str, Any], str | None], None]:
        """The local dispatcher of one declared event: admit per participant, then emit."""

        def _forward(params: dict[str, Any], session_id: str | None = None) -> None:
            channel = self._channel
            if channel is None:  # pragma: no cover — registered only with a channel
                return
            for participant in self._participants():
                if method not in participant.event_methods():
                    continue
                try:
                    admitted = participant.admit_event(method, params, session_id)
                except Exception:
                    # Counted like a failed handler: the next collection fails
                    # rather than look complete without this event.
                    channel.errors += 1
                    logger.exception(
                        "recorder: %s failed to admit %s", type(participant).__name__, method
                    )
                    continue
                if admitted is not None:
                    admitted.consumer = participant
                    self._emit(admitted)

        return _forward

    def _emit(self, event: RecorderEvent) -> None:
        if self._channel is None:
            return
        try:
            self._channel.emit(event)
        except Exception as exc:
            logger.warning(
                "recorder: could not emit %s into the archive scope: %s", event.method, exc
            )

    def _on_attached(self, params: dict[str, Any], parent: str | None = None) -> None:
        target = RecorderTarget.from_attached(params, parent)
        if not target.session_id:
            return
        self._sessions.add(target.session_id)
        if target.waiting:
            self._waiting.add(target.session_id)
        self._spawn(self._attach(target, params), self._attach_tasks)

    def _on_detached(self, params: dict[str, Any], _parent: str | None = None) -> None:
        sid = str(params.get("sessionId") or "")
        self._sessions.discard(sid)
        self._waiting.discard(sid)
        self._fetch_sessions.discard(sid)
        for key in tuple(self._held):
            if key[0] == sid:
                self._held.pop(key, None)
        target = self._targets.pop(sid, None)
        if target is None:
            return
        if self._prepared_target_ids.get(target.target_id) == sid:
            del self._prepared_target_ids[target.target_id]
            # Another session of the same target, if one is still attached.
            for other, known in self._targets.items():
                if known.target_id == target.target_id:
                    self._prepared_target_ids[target.target_id] = other
                    break
            else:
                self._prepared.pop(target.target_id, None)
        self._emit(RecorderEvent(session_id=sid, method="Target.detachedFromTarget", params=params))

    def _on_paused(self, params: dict[str, Any], session_id: str | None = None) -> None:
        if not session_id or "requestId" not in params:
            logger.warning("recorder: Fetch.requestPaused without session or id: %s", params)
            return
        stage: Literal["Request", "Response"] = (
            "Response"
            if "responseStatusCode" in params or "responseErrorReason" in params
            else "Request"
        )
        paused = PausedRequest(
            session_id=session_id,
            request_id=str(params["requestId"]),
            stage=stage,
            params=params,
            target=self._targets.get(session_id),
        )
        self._held[(session_id, paused.request_id)] = paused
        self._spawn(self._handle_paused(paused), self._pause_tasks)

    def _on_client_closed(self, reason: BaseException | None) -> None:
        if reason is None or self._closing:
            return  # our own stop(): not a loss
        self._lost = RecorderLostError(
            f"browser recorder connection lost ({reason}); the run cannot continue "
            f"unrecorded — docs/architecture_design.md §4.14"
        )
        self._closing = True
        try:
            self._on_lost(self._lost)
        except Exception:  # pragma: no cover — defensive
            logger.exception("recorder: on_lost callback raised")

    # ── tasks ─────────────────────────────────────────────────────────

    @staticmethod
    def _spawn(coro: Any, bucket: set[asyncio.Task[None]]) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        bucket.add(task)
        task.add_done_callback(bucket.discard)

    async def _attach(self, target: RecorderTarget, params: dict[str, Any]) -> None:
        """Prepare one target: write its batch, resume it, then wait for every reply.

        On a waiting target the batch and then ``Runtime.runIfWaitingForDebugger``
        go on the wire in that order, one write after the other from this task,
        before any reply is awaited (module docstring). The resume is written by
        ``PausedTarget`` mode B on the way out of the block, so a batch that
        cannot be built or written still resumes. The replies — the resume's
        included — are awaited afterwards: within the command timeout on a
        target that was not waiting (an existing one, behind the ready barrier),
        and for longer than the recorder may itself hold the target's first
        document on one that was (:meth:`_resumed_reply_timeout`). A failed or
        missing reply is recorded on the target, never raised. Cancelled (by
        :meth:`close`), the replies are abandoned unread.
        """
        sid = target.session_id
        written: list[tuple[str, PendingCommand]] = []
        detach = False

        async def resume() -> None:
            reply = await self._write_resume(sid)
            if reply is not None:
                try:
                    await reply.wait(self._resumed_reply_timeout())
                except BaseException:
                    reply.abandon()
                    raise
                self._waiting.discard(sid)

        paused = (
            PausedTarget(resume=resume, resume_timeout=self._resumed_reply_timeout())
            if target.waiting
            else contextlib.nullcontext()
        )
        try:
            try:
                async with paused:
                    # Closing, or Chrome's own target: resumed and detached,
                    # never prepared. A *second* session of one target (a
                    # service worker attaches at browser level and again under
                    # the page that registered it) is prepared like the first:
                    # Chrome intercepts the worker's main script only on the
                    # session under the page (measured, Chrome 153), so
                    # detaching it loses that request. Its traffic can then
                    # pause once per session; participants dedupe records by
                    # target and request, never by dropping a session (a
                    # service worker may attach twice, measured, arch §4.14).
                    if self._closing or target.internal:
                        detach = True
                    else:
                        self._targets[sid] = target
                        self._prepared_target_ids.setdefault(target.target_id, sid)
                        self._emit(
                            RecorderEvent(
                                session_id=sid, method="Target.attachedToTarget", params=params
                            )
                        )
                        for command in self._batch(target):
                            reply = await self._client.post(
                                command.method, dict(command.params), sid
                            )
                            written.append((command.method, reply))
            except Exception as exc:
                self._fail(target, f"preparation: {exc!r}")
            timeout = self._resumed_reply_timeout() if target.waiting else self._command_timeout_s
            await self._await_replies(target, written, timeout)
        except BaseException:
            for _method, reply in written:
                reply.abandon()
            raise
        finally:
            if detach:
                await self._quietly(
                    "Target.detachFromTarget",
                    {"sessionId": sid},
                    None,
                    timeout=self._command_timeout_s,
                )
                self._sessions.discard(sid)
        if not detach and self._prepared_target_ids.get(target.target_id) == sid:
            self._prepared.setdefault(target.target_id, asyncio.Event()).set()

    async def _write_resume(self, session_id: str) -> PendingCommand | None:
        """Write the resume of a still-waiting session; ``None`` when it was resumed already."""
        if session_id not in self._waiting:
            return None
        return await self._client.post(RESUME_METHOD, None, session_id)

    def _resumed_reply_timeout(self) -> float:
        """How long a resumed target's replies may take (:data:`RESUMED_REPLY_MARGIN_S`).

        At least as long as the recorder may itself hold the target's first
        document — the longest owner budget plus :data:`PAUSE_OWNER_GRACE_S`,
        or the default pause timeout — plus the margin for the network: the
        replies of a target without a page process come after that document
        commits.
        """
        holds = [self._pause_owner_timeout_s]
        for participant in self._participants():
            budget = participant.pause_budget_s
            if budget is not None:
                holds.append(budget + PAUSE_OWNER_GRACE_S)
        return max(holds) + self._resumed_reply_margin_s

    async def _await_replies(
        self, target: RecorderTarget, written: Sequence[tuple[str, PendingCommand]], timeout: float
    ) -> None:
        """Every reply of *written*, each within *timeout*; a failure is recorded, never raised."""
        if not written:
            return
        results = await asyncio.gather(
            *(reply.wait(timeout) for _method, reply in written), return_exceptions=True
        )
        for (method, _reply), result in zip(written, results, strict=True):
            if isinstance(result, BaseException):
                self._fail(target, f"{method}: {result}")

    def _batch(self, target: RecorderTarget) -> list[CDPCommand]:
        participants = list(self._participants())
        commands: list[CDPCommand] = []
        for participant in participants:
            commands.extend(participant.target_commands(target))
        if target.type in FETCH_TARGET_TYPES:
            patterns = [
                dict(pattern)
                for participant in participants
                for pattern in participant.fetch_patterns(target)
            ]
            if patterns:
                self._fetch_sessions.add(target.session_id)
                commands.append(CDPCommand("Fetch.enable", {"patterns": patterns}))
        commands.append(CDPCommand("Target.setAutoAttach", dict(AUTO_ATTACH_PARAMS)))
        return commands

    def _fail(self, target: RecorderTarget, detail: str) -> None:
        self.failures.setdefault(target.session_id, []).append(detail)
        logger.warning(
            "recorder: %s target %s (%s): %s",
            target.type,
            target.target_id,
            target.session_id,
            detail,
        )

    async def _handle_paused(self, paused: PausedRequest) -> None:
        """Route to the stage owner, then settle on the pausing session."""
        try:
            if not self._closing:
                owner = self._owner(paused)
                if owner is not None:
                    budget = owner.pause_budget_s
                    timeout = (
                        self._pause_owner_timeout_s
                        if budget is None
                        else budget + PAUSE_OWNER_GRACE_S
                    )
                    await asyncio.wait_for(owner.on_paused(paused), timeout)
        except Exception as exc:
            logger.warning(
                "recorder: the owner of paused %s %s failed: %r",
                paused.stage,
                paused.url,
                exc,
            )
        finally:
            await self._settle(paused)

    def _owner(self, paused: PausedRequest) -> RecorderParticipant | None:
        if paused.target is None:
            return None
        for participant in self._participants():
            for pattern in participant.fetch_patterns(paused.target):
                if pattern.get("requestStage", "Request") == paused.stage:
                    return participant
        return None

    async def _settle(self, paused: PausedRequest) -> None:
        """Settle exactly once, on the pausing session, what the owner did not answer.

        Answered by its owner → nothing. A Request-stage pause while a
        participant serves requests → ``Fetch.failRequest`` (never a
        continue: the request would go to the network). Anything else →
        continued, as a recording always does.
        """
        key = (paused.session_id, paused.request_id)
        async with paused._settlement_lock:
            if paused.settled or paused.answered:
                paused.settled = True
                self._held.pop(key, None)
                return
            if paused.stage == "Request" and self._serving:
                method = "Fetch.failRequest"
                params = {"requestId": paused.request_id, "errorReason": UNANSWERED_ERROR_REASON}
            else:
                method = (
                    "Fetch.continueResponse"
                    if paused.stage == "Response" and "responseStatusCode" in paused.params
                    else "Fetch.continueRequest"
                )
                params = {"requestId": paused.request_id}
            confirmed = await self._quietly(
                method, params, paused.session_id, timeout=self._command_timeout_s
            )
            if confirmed:
                paused.settled = True
                self._held.pop(key, None)

    async def _quietly(
        self,
        method: str,
        params: dict[str, Any] | None,
        session_id: str | None,
        *,
        timeout: float,
    ) -> bool:
        if not self._client.is_connected:
            return False
        try:
            await self._client.send(method, params, session_id, timeout=timeout)
        except Exception as exc:
            logger.debug("recorder: %s on %s failed: %s", method, session_id, exc)
            if self._closing:
                self._cleanup_failures.append(f"{method} on {session_id}: {exc}")
            return False
        return True
