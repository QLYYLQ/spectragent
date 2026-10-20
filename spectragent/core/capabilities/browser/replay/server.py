"""The replay server: answers every request from the recorded archive (§4.14 "服务").

:class:`ReplayServer` is one recorder participant of a replaying run
(``network_source: archive``), built by ``ArchiveNetworkSource.serve`` —
pure construction — and owned by the capability from setup to teardown:

- **Preparation** — every prepared target gets ``Network.enable`` and
  ``Network.setCacheDisabled(true)``; every frame also
  ``Network.setBypassServiceWorker(true)``, ``Page.enable`` and the
  service-worker block (``shims.SERVICE_WORKER_BLOCK``): a service worker
  would answer navigations itself, as ``XHR`` on its own session, whatever
  the tier. Every target with a Fetch domain contributes the Request-stage
  pattern ``{"urlPattern": "*", "requestStage": "Request"}`` to the
  recorder's one ``Fetch.enable``. All of it before the target resumes.
- **Serving** — it owns the Request stage (``serves_requests``): each paused
  request reaches :meth:`ReplayServer.on_paused` in a task the recorder
  spawned. The live body is redacted with M29's ``redact_post_body`` before
  any key; the matcher selects and consumes a record in one synchronous call
  (for a consumed record without a body, possibly with an identical available
  record answering in its place — the rule's ``unavailable: serve_identical``);
  ``responder.py`` shapes the answer (a JSONP answer calls the callback name
  this request asks for, ``jsonp.py``); the miss policy answers what the
  archive cannot. The answer goes to the **pausing** session (a dedicated worker's
  request pauses on its page's; an OOPIF's document on its parent page's).
  ``answered`` is set only after the send succeeded; whatever is not
  answered, the recorder fails — **nothing is ever continued to the
  network**.
- **What Fetch never sees** — WebSocket handshakes (``Network.webSocketCreated``)
  and http(s) requests that announce themselves (``Network.requestWillBeSent``)
  and end (``loadingFailed`` / ``loadingFinished``) without ever pausing (a
  nested dedicated worker's script) are logged as misses with ``answer:
  none`` (``websocket`` / ``unservable``).
- **The log** — every decision becomes one :class:`~.replay_log.ReplayLogEntry`
  in the run's :class:`~.replay_log.ReplayLog`, which ``browser.replay_log``
  drains at each collection.

State is per recorder session where it is per anything: a target's detach
drops what the server kept for that session, so a tab closed mid-pause leaves
nothing behind. The cursors are the run's (one :class:`~.matcher.Matcher`).
"""

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from spectragent.core.capabilities.browser.archive.base import RecorderHandle
from spectragent.core.capabilities.browser.archive.network_model import post_body_of
from spectragent.core.capabilities.browser.archive.redaction import redact_post_body
from spectragent.core.capabilities.browser.recorder import (
    FETCH_TARGET_TYPES,
    FRAME_TARGET_TYPES,
    CDPCommand,
    PausedRequest,
    RecorderEvent,
    RecorderParticipantBase,
    RecorderTarget,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    RecordedArchive,
    ReplayArchiveError,
)
from spectragent.core.capabilities.browser.replay.matcher import (
    LiveRequest,
    Matcher,
    MatcherConfig,
    MatchResult,
)
from spectragent.core.capabilities.browser.replay.miss_policy import (
    MissDecision,
    MissPolicyProtocol,
    MissReason,
    ReplayMiss,
)
from spectragent.core.capabilities.browser.replay.replay_log import (
    Answer,
    EntryKind,
    ReplayCandidate,
    ReplayLog,
    ReplayLogEntry,
    short_key,
)
from spectragent.core.capabilities.browser.replay.responder import (
    Fulfilment,
    Refusal,
    ShiftDates,
    exceeds_cap,
    is_preflight,
    miss_response,
    serve_range,
    serve_record,
    synthetic_preflight,
)
from spectragent.core.capabilities.browser.replay.shims import SERVICE_WORKER_BLOCK

logger = logging.getLogger(__name__)

#: The one Fetch pattern a replay asks for: every request, before it is sent.
REQUEST_STAGE_PATTERN: Mapping[str, Any] = {"urlPattern": "*", "requestStage": "Request"}
#: The events the server consumes from the recorder (beyond target bookkeeping).
EVENT_METHODS = frozenset(
    {
        "Network.webSocketCreated",
        "Network.requestWillBeSent",
        "Network.loadingFailed",
        "Network.loadingFinished",
    }
)
_HTTP = ("http://", "https://")


@dataclass(frozen=True)
class ReplayContext:
    """The run-time handles of one replay, assembled by the capability at setup.

    ``recorder`` sends on the recorder connection (the pausing session's
    answer); ``cycle`` is the environment's observation counter; ``clock``
    wall-clock seconds (the log's ``captured_at``, ``shift_dates``' now).
    """

    recorder: RecorderHandle
    cycle: Callable[[], int]
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class _Announced:
    """An http(s) request ``Network.requestWillBeSent`` announced and Fetch has not paused (yet)."""

    url: str
    method: str
    resource_type: str | None
    frame_id: str | None
    session_id: str | None


class ReplayServer(RecorderParticipantBase):
    """One replaying run's server: a recorder participant that answers every request."""

    def __init__(
        self,
        recorded: RecordedArchive,
        *,
        ctx: ReplayContext,
        matcher: MatcherConfig,
        policy: MissPolicyProtocol,
        shift_dates: ShiftDates,
        max_fulfill_body_bytes: int,
        fulfill_timeout_s: float,
        log_repeat_limit: int,
        log_mode: Literal["full", "summary"],
        source_fingerprint: Mapping[str, Any],
    ) -> None:
        self.recorded = recorded
        self.ctx = ctx
        self.matcher = Matcher(recorded.index, matcher, recorded.rules)
        self.policy = policy
        self.shift_dates: ShiftDates = shift_dates
        self.max_fulfill_body_bytes = max_fulfill_body_bytes
        self.fulfill_timeout_s = fulfill_timeout_s
        self.log = ReplayLog(mode=log_mode, repeat_limit=log_repeat_limit)
        #: Mismatches the run tolerates but records (Chrome version, UA, redaction policy).
        self.warnings: list[str] = []
        self._source_fingerprint = dict(source_fingerprint)
        #: Announced http(s) requests not paused yet, by network id.
        self._announced: dict[str, _Announced] = {}
        #: Pauses whose announcement was not handled yet, counted per network id
        #: (a redirect chain pauses once per hop under one id; the archive scope
        #: may be behind the pause tasks by more than one hop).
        self._paused_ahead: dict[str, int] = {}
        self._closed = False

    # ── recorder participant ─────────────────────────────────────────

    @property
    def serves_requests(self) -> bool:
        return True

    @property
    def pause_budget_s(self) -> float:
        return self.fulfill_timeout_s

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        commands = [
            CDPCommand("Network.enable"),
            CDPCommand("Network.setCacheDisabled", {"cacheDisabled": True}),
        ]
        if target.type in FRAME_TARGET_TYPES:
            commands.extend(
                (
                    CDPCommand("Network.setBypassServiceWorker", {"bypass": True}),
                    CDPCommand("Page.enable"),
                    CDPCommand(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": SERVICE_WORKER_BLOCK, "runImmediately": True},
                    ),
                )
            )
        return commands

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        return (REQUEST_STAGE_PATTERN,) if target.type in FETCH_TARGET_TYPES else ()

    def event_methods(self) -> Collection[str]:
        return EVENT_METHODS

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        """The few facts the server needs — never a body or a header."""
        if self._closed:
            return None
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            if not url.startswith(_HTTP):
                return None
            slim: dict[str, Any] = {
                "requestId": params.get("requestId"),
                "url": url,
                "method": request.get("method"),
                "type": params.get("type"),
                "frameId": params.get("frameId"),
            }
        elif method == "Network.webSocketCreated":
            slim = {"requestId": params.get("requestId"), "url": params.get("url")}
        elif method in ("Network.loadingFailed", "Network.loadingFinished"):
            slim = {"requestId": params.get("requestId")}
        else:
            return None
        return RecorderEvent(session_id=session_id, method=method, params=slim)

    def on_event(self, event: RecorderEvent) -> None:
        if self._closed:
            return
        params = event.params
        network_id = str(params.get("requestId") or "")
        if event.method == "Network.requestWillBeSent":
            ahead = self._paused_ahead.get(network_id, 0)
            if ahead > 1:
                self._paused_ahead[network_id] = ahead - 1
            elif ahead == 1:
                del self._paused_ahead[network_id]
            elif network_id:
                self._announced[network_id] = _Announced(
                    url=str(params.get("url") or ""),
                    method=str(params.get("method") or "GET"),
                    resource_type=params.get("type"),
                    frame_id=params.get("frameId"),
                    session_id=event.session_id,
                )
        elif event.method in ("Network.loadingFailed", "Network.loadingFinished"):
            self._paused_ahead.pop(network_id, None)
            announced = self._announced.pop(network_id, None)
            if announced is not None:
                self._log_unanswered(announced, "unservable", event.session_id)
        elif event.method == "Network.webSocketCreated":
            self._log_unanswered(
                _Announced(
                    url=str(params.get("url") or ""),
                    method="GET",
                    resource_type="WebSocket",
                    frame_id=None,
                    session_id=event.session_id,
                ),
                "websocket",
                event.session_id,
            )
        elif event.method == "Target.detachedFromTarget":
            self._forget_session(event.session_id)

    async def on_paused(self, paused: PausedRequest) -> None:
        """Answer one paused request on the session that paused it."""
        if self._closed or paused.stage != "Request":
            return
        network_id = paused.network_id
        if network_id is not None and self._announced.pop(network_id, None) is None:
            self._paused_ahead[network_id] = self._paused_ahead.get(network_id, 0) + 1
        try:
            answer, entry = self._decide(paused)
        except Exception as exc:  # a bug here must fail the next collection, not pass silently
            self.log.fail(f"deciding {paused.url}: {exc!r}")
            raise
        outcome: Answer = "failed"
        try:
            await self._send(paused, answer)
            paused.answered = True
            outcome = "fulfilled" if isinstance(answer, Fulfilment) else "failed"
        except Exception as exc:  # the recorder fails what was not answered
            logger.warning("replay: answering %s %s failed: %r", paused.stage, paused.url, exc)
        finally:
            # Logged whatever happened, a cancellation (owner timeout) included.
            self.log.append(entry.model_copy(update={"answer": outcome}))

    # ── deciding ─────────────────────────────────────────────────────

    def _decide(self, paused: PausedRequest) -> tuple[Fulfilment | MissDecision, ReplayLogEntry]:
        """What answers *paused*, and the log entry that says so. Synchronous: no await."""
        params = paused.params
        request = params.get("request") or {}
        method = str(request.get("method") or "GET").upper()
        url = str(request.get("url") or "")
        headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
        resource_type = params.get("resourceType")
        frame_id = params.get("frameId")
        is_document = resource_type == "Document"
        frame = self._frame(paused.target, frame_id) if is_document else None
        found = post_body_of(request)
        raw_body = found[0] if found is not None else None
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), None)
        redacted = redact_post_body(raw_body, content_type)[0] if raw_body else None
        live = LiveRequest(
            method=method,
            url=url,
            headers=headers,
            body=redacted,
            is_document=is_document,
            resource_type=resource_type,
        )
        base = {
            "captured_at": self.ctx.clock(),
            "cycle": self.ctx.cycle(),
            "method": method,
            "url": url,
            "resource_type": resource_type,
            "frame_id": frame_id,
            "frame": frame,
            "target_id": paused.target.target_id if paused.target is not None else None,
            "session_id": paused.session_id,
        }

        # The matcher selects and consumes in this one synchronous call.
        result = self.matcher.match(live)
        base["match_key"] = short_key(result.keys.normalized)
        base["body_key"] = short_key(result.keys.body_key)
        base["rule_id"] = result.rule_id
        base["exhaustion_policy"] = result.exhaustion_policy
        base["unavailable_policy"] = result.unavailable_policy
        record = result.record
        # A consumed record without a body: how its source request ended.
        base["source_outcome"] = record.source_outcome if record is not None else None
        reason: MissReason | None = result.reason
        if reason is None:
            served = self._serve(result, live, raw_body)
            if isinstance(served, Fulfilment):
                substitute = result.substitute
                kind: EntryKind = (
                    "substituted"
                    if substitute is not None
                    else "exhausted"
                    if result.exhausted
                    else "hit"
                )
                return served, ReplayLogEntry(
                    kind=kind,
                    record_id=record.record_id if record is not None else None,
                    run_id=record.run_id if record is not None else None,
                    served_record_id=substitute.record_id if substitute is not None else None,
                    level=result.level,
                    fuzzy_score=result.fuzzy_score,
                    range=served.header("content-range") if result.range is not None else None,
                    status=served.status,
                    callback_rewrite=served.callback_rewrite,
                    **base,
                )
            reason = served.reason
        if reason == "no_match" and is_preflight(live):
            served = synthetic_preflight(live)
            return served, ReplayLogEntry(
                kind="synthetic", reason=reason, status=served.status, **base
            )

        rule = self.recorded.rules.for_request(method, url)
        miss = ReplayMiss(
            method=method,
            url=url,
            resource_type=resource_type,
            is_document=is_document,
            frame=frame,
            content_type=content_type,
            reason=reason,
            site_benign=rule is not None and rule.synthesizes_204(url),
        )
        decision = self.policy.decide(miss)
        entry = ReplayLogEntry(
            kind="miss",
            record_id=record.record_id if record is not None else None,
            run_id=record.run_id if record is not None else None,
            request_class=decision.request_class,
            level=result.level,
            fuzzy_score=result.fuzzy_score,
            reason=reason,
            status=decision.status if decision.answer == "fulfill" else None,
            candidates=[
                ReplayCandidate(
                    record_id=c.record_id,
                    method=c.method,
                    url=c.url,
                    score=c.score,
                    consumed=c.consumed,
                )
                for c in result.candidates
            ],
            diverged=decision.diverged,
            # repeat_last repeated a record that could not be served (no available one).
            repeated=True if result.exhausted and record is not None else None,
            **base,
        )
        if decision.answer == "fulfill":
            return miss_response(decision), entry
        return decision, entry

    def _serve(
        self, result: MatchResult, live: LiveRequest, raw_body: bytes | None
    ) -> Fulfilment | Refusal:
        """The recorded answer of a match, or why it cannot be served after all."""
        cap = self.max_fulfill_body_bytes
        now = self.ctx.clock()
        try:
            if result.range is not None:
                cover = result.range.clipped(cap)
                return serve_range(
                    cover,
                    self.recorded.range_bytes(cover),
                    shift_dates=self.shift_dates,
                    now=now,
                )
            record = result.served
            if record is None:
                return Refusal("no_match")
            if exceeds_cap(record.payload_length, cap):
                return Refusal("too_large")
            body = self.recorded.body(record)
        except ReplayArchiveError as exc:
            logger.warning("replay: %s", exc)
            return Refusal("archive_unreadable")
        return serve_record(
            record,
            body,
            request=live,
            raw_body=raw_body,
            rule=self.recorded.rules.for_request(live.method, live.url),
            shift_dates=self.shift_dates,
            now=now,
            cap=cap,
        )

    @staticmethod
    def _frame(target: RecorderTarget | None, frame_id: Any) -> Literal["main", "sub"]:
        """A document's frame: the page target's own frame is ``main``, any other ``sub``.

        An OOPIF's document pauses on its parent page's session with the
        iframe's frame id; a popup's first document pauses on its own page
        session with the popup's own id (measured, arch §4.14).
        """
        if target is not None and target.type == "page" and frame_id == target.target_id:
            return "main"
        return "sub"

    async def _send(self, paused: PausedRequest, answer: Fulfilment | MissDecision) -> None:
        if isinstance(answer, Fulfilment):
            method, params = "Fetch.fulfillRequest", answer.command_params(paused.request_id)
        else:
            method = "Fetch.failRequest"
            params = {
                "requestId": paused.request_id,
                "errorReason": answer.error_reason or "Failed",
            }
        await self.ctx.recorder.send(
            method, params, paused.session_id, timeout=self.fulfill_timeout_s
        )

    # ── what Fetch never paused ──────────────────────────────────────

    def _log_unanswered(
        self, announced: _Announced, reason: MissReason, session_id: str | None
    ) -> None:
        target = self.ctx.recorder.target(session_id) if session_id else None
        self.log.append(
            ReplayLogEntry(
                kind="miss",
                captured_at=self.ctx.clock(),
                cycle=self.ctx.cycle(),
                method=announced.method.upper(),
                url=announced.url,
                resource_type=announced.resource_type,
                frame_id=announced.frame_id,
                target_id=target.target_id if target is not None else None,
                session_id=announced.session_id,
                reason=reason,
                answer="none",
            )
        )

    def _forget_session(self, session_id: str | None) -> None:
        """Drop what was kept for a detached session (its requests will never end)."""
        if session_id is None:
            return
        for network_id in [
            nid for nid, item in self._announced.items() if item.session_id == session_id
        ]:
            del self._announced[network_id]

    def pending_sessions(self) -> set[str | None]:
        """The sessions the server still keeps state for (diagnostics and tests)."""
        return {item.session_id for item in self._announced.values()}

    # ── the run ──────────────────────────────────────────────────────

    def warn(self, message: str) -> None:
        """Record a mismatch the replay tolerates (it goes into the replay fingerprint)."""
        logger.warning("replay: %s", message)
        if message not in self.warnings:
            self.warnings.append(message)

    def fingerprint(self) -> dict[str, Any]:
        """The ``network_source`` block of this run's environment fingerprint."""
        return {
            **self._source_fingerprint,
            "source_run_id": self.recorded.run_id,
            "source_fingerprint_sha256": self.recorded.fingerprint_sha256,
            "site_rules_sha256": self.recorded.rules.sha256,
            "site_rules_snapshot": self.recorded.rules.snapshot().model_dump(mode="json"),
            "warnings": list(self.warnings),
        }

    def close(self) -> None:
        """Stop answering (the recorder fails what is still held) and drop the run's state."""
        self._closed = True
        self._announced.clear()
        self._paused_ahead.clear()
