"""The recorder connection: a second, browser-level CDP client (§4.14).

``BrowserCapability`` talks to Chrome over its *control* connection: tab
attach, observation, actions, viewport. Everything that must happen **before
a new target runs its first request or script** — the ``replayability``
shim and overrides, Fetch interception for the network archiver — happens
here instead, on a separate :class:`CDPClient` that sets browser-level
``Target.setAutoAttach{waitForDebuggerOnStart: true}`` and prepares every
target while it is paused. Why a second connection, and the Chrome facts
every rule below rests on, are in ``docs/architecture_design.md`` §4.14
(measured in the data-flywheel S1 / S2a probes, Chrome 153).

This module is the CDP choreography and nothing else. It does not know what
any participant records or pins; it asks each :class:`RecorderParticipant`
what to send to a target and who owns a paused request:

- **Pause-bearing events** (``Target.attachedToTarget`` of a waiting target,
  ``Fetch.requestPaused``) are handled in tasks spawned straight from the
  client callback — never through an agent-cdp scope queue, which drops the
  newest event at 1024 and a dropped pause wedges the page. Every waiting
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
- **Bytes never travel in events** (scope history keeps the last 1000): the
  dispatcher hands each raw event to the participants that declared it
  (:meth:`RecorderParticipant.admit_event`, synchronous, in the receive loop)
  and emits only what each one admits — small metadata, addressed to that
  participant; bodies and frame payloads go to the participant's own side
  store, and values it never stores (credentials) never enter the scope.

Resume is agent-cdp ``PausedTarget`` mode B: our own callback sends
``Runtime.runIfWaitingForDebugger`` on the target's session. Mode A sends it
without a session on af's flattened client and silently resumes the wrong
target (S1).
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from agent_cdp import BaseEvent, ConnectionType, EventScope, PausedTarget
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
#: On a *waiting* service worker these domains answer only after resume
#: (S2a). Their replies are awaited after ``Runtime.runIfWaitingForDebugger``.
DEFERRED_ON_SERVICE_WORKER = ("Network.", "Runtime.")

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
#: http fallback alive (a 404 or ``BlockedByClient`` cuts it off — S2a).
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

    Raised (and used to fail the control connection's waiters) the moment
    the loss is pushed: a recording never continues unrecorded, and a
    browser whose recorder vanished mid-pause is wedged anyway (§4.14).
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
    #: Claimed by whoever settles it first (task or cleanup).
    settled: bool = False

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


class RecorderEvent(BaseEvent[None]):
    """A metadata event from the recorder connection, with its session id.

    Small by contract — the archive scope's history keeps its last 1000
    events: what a participant admitted (:meth:`RecorderParticipant.admit_event`),
    never a body, a frame payload or a credential.
    """

    session_id: str | None = None
    method: str
    params: dict[str, Any] = {}
    #: The participant that admitted it, and the only one it is delivered to;
    #: ``None`` for the recorder's own target bookkeeping, which every
    #: participant receives.
    consumer: Any = Field(default=None, exclude=True, repr=False)


class ArchiveBarrierEvent(BaseEvent[None]):
    """Queued behind every metadata event emitted before it (FIFO)."""

    emitted_before: int = 0


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
        before anything is emitted. The scope's history keeps its last 1000
        events, so the returned event carries small metadata only: bytes
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
    """What the recorder hands the archive-scope side.

    Exists only when archivers are bound. ``emitted`` counts metadata events
    put into the scope, ``handled`` what the scope's consumer
    (``ArchiveWatchdog``) actually received; :meth:`barrier` waits for every
    event emitted before it and raises if the counts disagree — the queue
    dropped something, and a collection must then fail rather than look
    complete.
    """

    def __init__(self, scope: EventScope, participants: ParticipantsAccessor) -> None:
        self.scope = scope
        self._participants = participants
        self.emitted = 0
        self.handled = 0
        #: Participant callbacks that raised while admitting or handling an event.
        self.errors = 0
        # The barrier's own queued no-op: it is what makes ``await barrier``
        # wait behind every event already in the (FIFO) scope queue, whoever
        # else listens on the scope.
        scope.connect(ArchiveBarrierEvent, self._on_barrier, mode=ConnectionType.QUEUED)

    @staticmethod
    async def _on_barrier(_event: ArchiveBarrierEvent) -> None:
        return None

    def participants(self) -> tuple[RecorderParticipant, ...]:
        return tuple(self._participants())

    def emit(self, event: RecorderEvent) -> None:
        self.emitted += 1
        self.scope.emit(event)

    def mark_handled(self) -> None:
        self.handled += 1

    async def barrier(self, timeout: float = 5.0) -> None:
        """Wait until everything emitted so far was handled, or raise."""
        target = self.emitted
        event = ArchiveBarrierEvent(emitted_before=target, event_timeout=timeout)
        self.scope.emit(event)
        try:
            await event
        except TimeoutError as exc:
            raise ArchiveChannelError(
                f"archive scope barrier did not complete within {timeout:g}s "
                f"({self.handled} of {target} metadata events handled)"
            ) from exc
        if self.handled < target:
            raise ArchiveChannelError(
                f"archive scope dropped {target - self.handled} of {target} metadata "
                f"events (agent-cdp's scope queue is bounded); this collection would "
                f"be incomplete"
            )


# ── The connection ────────────────────────────────────────────────────


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
    ) -> None:
        self._client = client
        self._participants = participants
        self._channel = channel
        self._on_lost = on_lost
        self._command_timeout_s = command_timeout_s
        self._pause_owner_timeout_s = pause_owner_timeout_s
        self._ready_timeout_s = ready_timeout_s

        #: Prepared targets by session.
        self._targets: dict[str, RecorderTarget] = {}
        #: target id → its first prepared session.
        self._prepared_target_ids: dict[str, str] = {}
        #: Every session attached to this client, prepared or not.
        self._sessions: set[str] = set()
        #: Sessions attached waiting and not yet resumed.
        self._waiting: set[str] = set()
        #: Sessions a ``Fetch.enable`` was sent to.
        self._fetch_sessions: set[str] = set()
        #: Failed commands, per session (diagnostics; never fatal by itself).
        self.failures: dict[str, list[str]] = {}

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

    async def close(self) -> None:
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
        (S1), so nothing here may be skipped because an earlier step failed.
        """
        if self._closed:
            return
        self._closed = True
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
            self._waiting.discard(sid)
            await self._quietly("Runtime.runIfWaitingForDebugger", None, sid, timeout=t)

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
            await asyncio.gather(*tasks, return_exceptions=True)

        for method, callback in self._registrations:
            with contextlib.suppress(Exception):
                self._client.off_event(method, callback)
        self._registrations.clear()

        try:
            await self._client.stop()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("recorder: client stop failed: %s", exc)

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
        """Prepare one target while it is paused; resume it in ``finally``."""
        sid = target.session_id
        deferred: list[tuple[CDPCommand, asyncio.Future[Any]]] = []
        detach = False
        paused = (
            PausedTarget(resume=lambda: self._resume(sid))
            if target.waiting
            else contextlib.nullcontext()
        )
        try:
            async with paused:
                # Closing, or Chrome's own target: resumed and detached, never
                # prepared. A *second* session of one target (a service worker
                # attaches at browser level and again under the page that
                # registered it) is prepared like the first: Chrome intercepts
                # the worker's main script only on the session under the page
                # (measured, Chrome 153), so detaching it loses that request.
                # Its traffic can then pause once per session; participants
                # dedupe records by target and request, never by dropping a
                # session (S1: "SW may attach twice → dedupe records").
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
                    deferred = await self._pipeline(target)
        except Exception as exc:
            self._fail(target, f"preparation: {exc!r}")
        finally:
            if deferred:
                results = await asyncio.gather(*(f for _, f in deferred), return_exceptions=True)
                self._record(target, [c for c, _ in deferred], results)
            if detach:
                await self._quietly(
                    "Target.detachFromTarget",
                    {"sessionId": sid},
                    None,
                    timeout=self._command_timeout_s,
                )
                self._sessions.discard(sid)

    async def _pipeline(
        self, target: RecorderTarget
    ) -> list[tuple[CDPCommand, asyncio.Future[Any]]]:
        """Send the whole batch at once; await all but the SW-deferred replies."""
        commands = self._batch(target)
        sent = [
            (command, asyncio.ensure_future(self._command(target.session_id, command)))
            for command in commands
        ]
        now: list[tuple[CDPCommand, asyncio.Future[Any]]] = []
        later: list[tuple[CDPCommand, asyncio.Future[Any]]] = []
        for command, future in sent:
            if target.type == "service_worker" and command.method.startswith(
                DEFERRED_ON_SERVICE_WORKER
            ):
                later.append((command, future))
            else:
                now.append((command, future))
        try:
            results = await asyncio.gather(*(f for _, f in now), return_exceptions=True)
        except BaseException:
            for _, future in sent:
                future.cancel()
            raise
        self._record(target, [c for c, _ in now], results)
        return later

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

    async def _command(self, session_id: str, command: CDPCommand) -> Any:
        return await self._client.send(
            command.method,
            dict(command.params),
            session_id,
            timeout=self._command_timeout_s,
        )

    def _record(
        self, target: RecorderTarget, commands: Sequence[CDPCommand], results: Sequence[Any]
    ) -> None:
        for command, result in zip(commands, results, strict=True):
            if isinstance(result, BaseException):
                self._fail(target, f"{command.method}: {result}")

    def _fail(self, target: RecorderTarget, detail: str) -> None:
        self.failures.setdefault(target.session_id, []).append(detail)
        logger.warning(
            "recorder: %s target %s (%s): %s",
            target.type,
            target.target_id,
            target.session_id,
            detail,
        )

    async def _resume(self, session_id: str) -> None:
        if session_id not in self._waiting:
            return
        self._waiting.discard(session_id)
        await self._client.send(
            "Runtime.runIfWaitingForDebugger",
            None,
            session_id,
            timeout=self._command_timeout_s,
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
        self._held.pop((paused.session_id, paused.request_id), None)
        if paused.settled or paused.answered:
            return
        paused.settled = True
        if paused.stage == "Request" and self._serving:
            await self._quietly(
                "Fetch.failRequest",
                {"requestId": paused.request_id, "errorReason": UNANSWERED_ERROR_REASON},
                paused.session_id,
                timeout=self._command_timeout_s,
            )
            return
        method = (
            "Fetch.continueResponse"
            if paused.stage == "Response" and "responseStatusCode" in paused.params
            else "Fetch.continueRequest"
        )
        await self._quietly(
            method,
            {"requestId": paused.request_id},
            paused.session_id,
            timeout=self._command_timeout_s,
        )

    async def _quietly(
        self,
        method: str,
        params: dict[str, Any] | None,
        session_id: str | None,
        *,
        timeout: float,
    ) -> None:
        if not self._client.is_connected:
            return
        try:
            await self._client.send(method, params, session_id, timeout=timeout)
        except Exception as exc:
            logger.debug("recorder: %s on %s failed: %s", method, session_id, exc)
