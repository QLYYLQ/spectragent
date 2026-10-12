"""The recorder connection's CDP choreography, against a scripted browser.

``docs/architecture_design.md`` §4.14. The live counterpart is
``test_browser_recorder_live.py``; this module pins the rules that a real
Chrome cannot be made to break on demand: pipelining, resume and continue in
``finally``, session routing, the service-worker deferral, filtering and
dedupe, the fixed cleanup order, pushed liveness, and what the capability
does — and does not — send on each connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence
from typing import Any, ClassVar

import pytest
from agent_cdp import EventScope, ScopeGroup

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.capabilities import (
    CDP,
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
)
from spectragent.core.capabilities.browser.recorder import (
    ArchiveChannel,
    ArchiveChannelError,
    CDPCommand,
    PausedRequest,
    RecorderConnection,
    RecorderError,
    RecorderEvent,
    RecorderLostError,
    RecorderParticipantBase,
    RecorderTarget,
)
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.schemas.action import Action
from spectragent.schemas.provider_session import ProviderSession
from tests._helpers.fake_cdp import FakeBrowser, FakeClient

# ── helpers ───────────────────────────────────────────────────────────


class _Participant(RecorderParticipantBase):
    """Contributes fixed commands / patterns and records what reaches it."""

    def __init__(
        self,
        name: str,
        *,
        commands: Sequence[CDPCommand] = (),
        patterns: Sequence[Mapping[str, Any]] = (),
        on_paused: Any = None,
        events: Collection[str] = (),
    ) -> None:
        self.name = name
        self.commands = tuple(commands)
        self.patterns = tuple(patterns)
        self.declared = frozenset(events)
        self.paused: list[PausedRequest] = []
        self.events: list[RecorderEvent] = []
        #: (method, params, session) of every raw event offered for admission.
        self.offered: list[tuple[str, Mapping[str, Any], str | None]] = []
        self._on_paused = on_paused
        self.raise_in_commands = False

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        if self.raise_in_commands:
            raise RuntimeError(f"{self.name} cannot build commands")
        return self.commands

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        return self.patterns

    def event_methods(self) -> Collection[str]:
        return self.declared

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        """Admits its own small view: the params without ``body``, tagged with its name."""
        self.offered.append((method, params, session_id))
        slim = {key: value for key, value in params.items() if key != "body"}
        return RecorderEvent(session_id=session_id, method=method, params={**slim, "by": self.name})

    def on_event(self, event: RecorderEvent) -> None:
        self.events.append(event)

    async def on_paused(self, paused: PausedRequest) -> None:
        self.paused.append(paused)
        if self._on_paused is not None:
            await self._on_paused(paused)


async def _recorder(
    browser: FakeBrowser,
    participants: list[Any],
    *,
    channel: ArchiveChannel | None = None,
    lost: list[RecorderLostError] | None = None,
    **kwargs: Any,
) -> tuple[RecorderConnection, FakeClient]:
    client = browser.client("ws://fake/devtools/browser", max_frame_size=None)
    await client.start()
    recorder = RecorderConnection(
        client,
        participants=lambda: participants,
        channel=channel,
        on_lost=(lost.append if lost is not None else (lambda _e: None)),
        **kwargs,
    )
    return recorder, client


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


RESPONSE_STAGE = ({"urlPattern": "*", "requestStage": "Response"},)
#: The callbacks the recorder registers for itself, whatever the participants.
RECORDER_OWN = {"Target.attachedToTarget", "Target.detachedFromTarget", "Fetch.requestPaused"}


def _registered(client: FakeClient) -> set[str]:
    return {method for method, callbacks in client.callbacks.items() if callbacks}


# ── the per-target pipeline ───────────────────────────────────────────


class TestPipeline:
    async def test_batch_order_and_resume_last(self) -> None:
        browser = FakeBrowser()
        first = _Participant("plan", commands=(CDPCommand("Page.enable"), CDPCommand("X.first")))
        second = _Participant(
            "archiver", commands=(CDPCommand("X.second"),), patterns=RESPONSE_STAGE
        )
        recorder, client = await _recorder(browser, [first, second])
        await recorder.open()
        sessions = browser.open_target("pop-1", "page", url="http://x/")
        sid = sessions[client.id]
        await _settle()

        assert client.methods(session_id=sid) == [
            "Page.enable",
            "X.first",
            "X.second",
            "Fetch.enable",
            "Target.setAutoAttach",
            "Runtime.runIfWaitingForDebugger",
        ]
        fetch = next(s for s in client.sent if s.method == "Fetch.enable" and s.session_id == sid)
        assert fetch.params == {"patterns": [dict(RESPONSE_STAGE[0])]}
        auto = next(
            s for s in client.sent if s.method == "Target.setAutoAttach" and s.session_id == sid
        )
        assert auto.params == {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
        assert browser.is_running("pop-1")
        await recorder.close()

    async def test_the_batch_is_pipelined_not_awaited_one_by_one(self) -> None:
        """Every command is on the wire before the first reply is awaited."""
        browser = FakeBrowser()
        gate = browser.hold("X.slow")
        plan = _Participant("plan", commands=(CDPCommand("X.slow"), CDPCommand("X.after")))
        recorder, client = await _recorder(browser, [plan], command_timeout_s=2.0)
        task = asyncio.create_task(recorder.open())
        await _settle()
        # X.slow has not answered, and X.after (and the rest) were already sent.
        existing = [
            s
            for s in client.sent
            if s.method in ("X.slow", "X.after", "Target.setAutoAttach")
            and s.session_id is not None
        ]
        assert [s.method for s in existing] == ["X.slow", "X.after", "Target.setAutoAttach"]
        assert not task.done()
        gate.set()
        await task
        await recorder.close()

    async def test_resume_in_finally_when_a_command_fails(self) -> None:
        browser = FakeBrowser()
        browser.fail("X.broken", RuntimeError("CDP error: nope"))
        plan = _Participant("plan", commands=(CDPCommand("X.broken"), CDPCommand("X.ok")))
        recorder, client = await _recorder(browser, [plan])
        await recorder.open()
        sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        assert browser.is_running("pop-1")
        assert any("X.broken" in f for f in recorder.failures[sid])
        await recorder.close()

    async def test_resume_in_finally_when_a_participant_raises(self) -> None:
        browser = FakeBrowser()
        plan = _Participant("plan")
        recorder, client = await _recorder(browser, [plan])
        await recorder.open()
        plan.raise_in_commands = True
        sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        assert browser.is_running("pop-1")
        assert client.methods(session_id=sid) == ["Runtime.runIfWaitingForDebugger"]
        assert "cannot build commands" in recorder.failures[sid][0]
        await recorder.close()

    async def test_resume_is_routed_to_the_targets_own_session(self) -> None:
        """Mode B: never the session-less resume of agent-cdp's mode A."""
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [])
        await recorder.open()
        sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        resumes = [s for s in client.sent if s.method == "Runtime.runIfWaitingForDebugger"]
        assert [(s.session_id, s.params) for s in resumes] == [(sid, {})]
        assert browser.sessionless_resumes == 0
        await recorder.close()

    async def test_a_dedicated_worker_gets_no_fetch(self) -> None:
        browser = FakeBrowser()
        archiver = _Participant("archiver", patterns=RESPONSE_STAGE)
        recorder, client = await _recorder(browser, [archiver])
        await recorder.open()
        by_type = {
            kind: browser.open_target(f"t-{kind}", kind)[client.id]
            for kind in ("worker", "shared_worker", "service_worker", "iframe")
        }
        await _settle()
        assert "Fetch.enable" not in client.methods(session_id=by_type["worker"])
        for kind in ("shared_worker", "service_worker", "iframe"):
            assert "Fetch.enable" in client.methods(session_id=by_type[kind]), kind
        await recorder.close()

    async def test_service_worker_network_and_runtime_replies_are_awaited_after_resume(
        self,
    ) -> None:
        browser = FakeBrowser()
        plan = _Participant(
            "plan",
            commands=(
                CDPCommand("Runtime.evaluate", {"expression": "1"}),
                CDPCommand("Network.enable"),
            ),
        )
        recorder, client = await _recorder(browser, [plan], command_timeout_s=0.5)
        await recorder.open()
        sid = browser.open_target("sw-1", "service_worker")[client.id]
        await _settle()
        await asyncio.sleep(0.05)
        assert browser.is_running("sw-1")
        # The deferred replies arrived after the resume, so nothing failed.
        assert sid not in recorder.failures
        sent = client.methods(session_id=sid)
        assert sent.index("Runtime.runIfWaitingForDebugger") > sent.index("Network.enable")
        await recorder.close()

    async def test_internal_targets_are_resumed_and_detached(self) -> None:
        browser = FakeBrowser()
        plan = _Participant("plan", commands=(CDPCommand("X.prepare"),))
        recorder, client = await _recorder(browser, [plan])
        await recorder.open()
        ui = browser.open_target("ui-1", "browser_ui")[client.id]
        ext = browser.open_target("ext-1", "service_worker", url="chrome-extension://abc/sw.js")[
            client.id
        ]
        devtools = browser.open_target("dt-1", "page", url="devtools://devtools/x")[client.id]
        await _settle()
        for skipped in (ui, ext, devtools):
            assert "X.prepare" not in client.methods(session_id=skipped), skipped
            assert "Runtime.runIfWaitingForDebugger" in client.methods(session_id=skipped)
        detached = [
            s.params["sessionId"] for s in client.sent if s.method == "Target.detachFromTarget"
        ]
        assert set(detached) == {ui, ext, devtools}
        assert all(browser.is_running(t) for t in ("ui-1", "ext-1", "dt-1"))
        await recorder.close()

    async def test_a_second_session_of_one_target_is_prepared_too(self) -> None:
        """A service worker attaches at browser level *and* under its page.

        Chrome intercepts the worker's main script only on the session under
        the page (measured on Chrome 153; ``test_browser_recorder_live``), so
        neither session may be dropped: both are prepared, both resumed, and
        participants dedupe the records.
        """
        browser = FakeBrowser()
        archiver = _Participant(
            "archiver", commands=(CDPCommand("X.prepare"),), patterns=RESPONSE_STAGE
        )
        recorder, client = await _recorder(browser, [archiver])
        await recorder.open()
        first = browser.open_target("sw-1", "service_worker", url="http://x/sw.js")[client.id]
        await _settle()
        twin = browser.new_session("twin")
        browser.targets["sw-1"].sessions[99] = twin
        browser.targets["sw-1"].waiting.add(twin)
        client.fire(
            "Target.attachedToTarget",
            {
                "sessionId": twin,
                "targetInfo": browser.targets["sw-1"].info(),
                "waitingForDebugger": True,
            },
            "page-session",
        )
        await _settle()
        for sid in (first, twin):
            assert {"X.prepare", "Fetch.enable", "Runtime.runIfWaitingForDebugger"} <= set(
                client.methods(session_id=sid)
            ), sid
        assert not [s for s in client.sent if s.method == "Target.detachFromTarget"]
        assert recorder.session_for_target("sw-1") == first
        assert recorder.sessions_for_target("sw-1") == [first, twin]
        # Detaching the first hands the target over to the second.
        client.fire("Target.detachedFromTarget", {"sessionId": first}, None)
        assert recorder.session_for_target("sw-1") == twin
        await recorder.close()


class TestOpen:
    async def test_ready_barrier_waits_for_existing_targets(self) -> None:
        browser = FakeBrowser()
        gate = browser.hold("X.slow")
        plan = _Participant("plan", commands=(CDPCommand("X.slow"),))
        recorder, client = await _recorder(browser, [plan], command_timeout_s=5.0)
        task = asyncio.create_task(recorder.open())
        await _settle()
        assert not task.done()
        gate.set()
        await asyncio.wait_for(task, 1.0)
        assert recorder.session_for_target("page-1") is not None
        # Existing targets are not waiting: nothing to resume.
        assert "Runtime.runIfWaitingForDebugger" not in client.methods()
        await recorder.close()

    async def test_an_unprepared_existing_target_fails_open(self) -> None:
        browser = FakeBrowser()
        browser.hold("X.slow")
        plan = _Participant("plan", commands=(CDPCommand("X.slow"),))
        recorder, _client = await _recorder(
            browser, [plan], command_timeout_s=5.0, ready_timeout_s=0.05
        )
        with pytest.raises(RecorderError, match="not prepared"):
            await recorder.open()
        await recorder.close()

    async def test_browser_version_is_read_once(self) -> None:
        browser = FakeBrowser(user_agent="UA/7")
        recorder, client = await _recorder(browser, [])
        assert (await recorder.browser_version())["userAgent"] == "UA/7"
        await recorder.browser_version()
        assert client.methods(prefix="Browser.getVersion") == ["Browser.getVersion"]
        await recorder.close()


# ── paused requests ───────────────────────────────────────────────────


def _pause(
    client: FakeClient,
    sid: str,
    request_id: str,
    *,
    status: int | None = 200,
    url: str = "http://x/",
) -> None:
    params: dict[str, Any] = {"requestId": request_id, "request": {"url": url, "method": "GET"}}
    if status is not None:
        params["responseStatusCode"] = status
        params["networkId"] = f"net-{request_id}"
    client.fire("Fetch.requestPaused", params, sid)


def _continues(client: FakeClient) -> list[tuple[str, str, str | None]]:
    return [
        (s.method, s.params["requestId"], s.session_id)
        for s in client.sent
        if s.method in ("Fetch.continueResponse", "Fetch.continueRequest")
    ]


class TestPausedRequests:
    async def _ready(self, participants: list[Any], **kwargs: Any):
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, participants, **kwargs)
        await recorder.open()
        page_sid = recorder.session_for_target("page-1")
        assert page_sid is not None
        return browser, recorder, client, page_sid

    async def test_the_stage_owner_sees_it_then_it_is_continued_on_the_pausing_session(
        self,
    ) -> None:
        archiver = _Participant("archiver", patterns=RESPONSE_STAGE)
        _browser, recorder, client, sid = await self._ready([_Participant("plan"), archiver])
        _pause(client, sid, "r1")
        await _settle()
        assert [p.request_id for p in archiver.paused] == ["r1"]
        assert archiver.paused[0].stage == "Response" and archiver.paused[0].network_id == "net-r1"
        assert _continues(client) == [("Fetch.continueResponse", "r1", sid)]
        await recorder.close()

    async def test_continued_even_when_the_owner_raises(self) -> None:
        async def _boom(_p: PausedRequest) -> None:
            raise RuntimeError("owner failed")

        archiver = _Participant("archiver", patterns=RESPONSE_STAGE, on_paused=_boom)
        _browser, recorder, client, sid = await self._ready([archiver])
        _pause(client, sid, "r1")
        await _settle()
        assert _continues(client) == [("Fetch.continueResponse", "r1", sid)]
        await recorder.close()

    async def test_continued_even_when_the_owner_times_out(self) -> None:
        async def _forever(_p: PausedRequest) -> None:
            await asyncio.Event().wait()

        archiver = _Participant("archiver", patterns=RESPONSE_STAGE, on_paused=_forever)
        _browser, recorder, client, sid = await self._ready([archiver], pause_owner_timeout_s=0.05)
        _pause(client, sid, "r1")
        await asyncio.sleep(0.15)
        assert _continues(client) == [("Fetch.continueResponse", "r1", sid)]
        await recorder.close()

    async def test_an_owner_gets_its_own_declared_budget(self) -> None:
        """A budget the owner's knobs set (a long body read) is honoured, not the default."""
        finished: list[str] = []

        async def _slow(paused: PausedRequest) -> None:
            await asyncio.sleep(0.2)
            finished.append(paused.request_id)

        class _Budgeted(_Participant):
            @property
            def pause_budget_s(self) -> float:
                return 0.4

        archiver = _Budgeted("archiver", patterns=RESPONSE_STAGE, on_paused=_slow)
        _browser, recorder, client, sid = await self._ready([archiver], pause_owner_timeout_s=0.05)
        _pause(client, sid, "r1")
        await asyncio.sleep(0.1)
        assert _continues(client) == [], "still within the owner's own budget"
        await asyncio.sleep(0.2)
        assert finished == ["r1"]
        assert _continues(client) == [("Fetch.continueResponse", "r1", sid)]
        await recorder.close()

    async def test_a_pause_without_owner_is_continued_at_once(self) -> None:
        plan = _Participant("plan")  # asks for no stage at all
        _browser, recorder, client, sid = await self._ready([plan])
        _pause(client, sid, "r1", status=None)
        await _settle()
        assert plan.paused == []
        assert _continues(client) == [("Fetch.continueRequest", "r1", sid)]
        await recorder.close()

    async def test_request_stage_is_continued_with_continue_request(self) -> None:
        owner = _Participant("m30", patterns=({"urlPattern": "*", "requestStage": "Request"},))
        _browser, recorder, client, sid = await self._ready([owner])
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert [p.stage for p in owner.paused] == ["Request"]
        assert _continues(client) == [("Fetch.continueRequest", "q1", sid)]
        await recorder.close()

    async def test_an_answered_pause_is_not_continued_again(self) -> None:
        async def _fulfil(paused: PausedRequest) -> None:
            paused.answered = True

        owner = _Participant("m30", patterns=RESPONSE_STAGE, on_paused=_fulfil)
        _browser, recorder, client, sid = await self._ready([owner])
        _pause(client, sid, "r1")
        await _settle()
        assert _continues(client) == []
        await recorder.close()

    async def test_worker_requests_are_continued_on_the_page_session_that_paused_them(
        self,
    ) -> None:
        archiver = _Participant("archiver", patterns=RESPONSE_STAGE)
        browser, recorder, client, page_sid = await self._ready([archiver])
        browser.open_target("w-1", "worker")
        await _settle()
        # A dedicated worker's fetch pauses on the parent page's session.
        _pause(client, page_sid, "rw", url="http://x/worker-data.json")
        await _settle()
        assert _continues(client) == [("Fetch.continueResponse", "rw", page_sid)]
        await recorder.close()


# ── a participant that serves requests (M30's replay server) ─────────

REQUEST_STAGE = ({"urlPattern": "*", "requestStage": "Request"},)
#: Every Fetch command that settles or answers a paused request.
_ANSWERS = (
    "Fetch.continueRequest",
    "Fetch.continueResponse",
    "Fetch.failRequest",
    "Fetch.fulfillRequest",
)


class _Server(_Participant):
    """A Request-stage owner that answers requests itself, like ``ReplayServer``."""

    def __init__(self, *args: Any, frame_types: frozenset[str] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        #: Target types it asks Fetch for (``None``: every target).
        self.frame_types = frame_types

    @property
    def serves_requests(self) -> bool:
        return True

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        if self.frame_types is not None and target.type not in self.frame_types:
            return ()
        return self.patterns


def _answers(client: FakeClient) -> list[tuple[str, str, str | None, Any]]:
    """``(method, requestId, session, errorReason)`` of every answer sent."""
    return [
        (s.method, s.params["requestId"], s.session_id, s.params.get("errorReason"))
        for s in client.sent
        if s.method in _ANSWERS
    ]


class TestServingParticipant:
    """While a participant serves requests, an unanswered Request pause fails — never continues.

    §4.14 "事件": the replay server answers on the pausing session and sets
    ``answered`` only after its send succeeded; the recorder settles
    everything else with ``Fetch.failRequest{errorReason: Failed}`` — owner
    timeout, owner exception, no owner, no prepared target, held at close.
    """

    async def _ready(self, participants: list[Any], **kwargs: Any):
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, participants, **kwargs)
        await recorder.open()
        page_sid = recorder.session_for_target("page-1")
        assert page_sid is not None
        return browser, recorder, client, page_sid

    @staticmethod
    def _failed(request_id: str, sid: str | None) -> tuple[str, str, str | None, str]:
        return ("Fetch.failRequest", request_id, sid, "Failed")

    async def test_an_answered_request_is_not_answered_again(self) -> None:
        recorder_box: list[RecorderConnection] = []

        async def _fulfil(paused: PausedRequest) -> None:
            await recorder_box[0].send(
                "Fetch.fulfillRequest",
                {"requestId": paused.request_id, "responseCode": 200},
                paused.session_id,
            )
            paused.answered = True

        server = _Server("server", patterns=REQUEST_STAGE, on_paused=_fulfil)
        _browser, recorder, client, sid = await self._ready([server])
        recorder_box.append(recorder)
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert _answers(client) == [("Fetch.fulfillRequest", "q1", sid, None)]
        await recorder.close()
        assert _answers(client) == [("Fetch.fulfillRequest", "q1", sid, None)]

    async def test_the_owner_raising_fails_the_request(self) -> None:
        async def _boom(_p: PausedRequest) -> None:
            raise RuntimeError("the server broke")

        server = _Server("server", patterns=REQUEST_STAGE, on_paused=_boom)
        _browser, recorder, client, sid = await self._ready([server])
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert _answers(client) == [self._failed("q1", sid)]
        await recorder.close()

    async def test_the_owner_timing_out_fails_the_request(self) -> None:
        async def _forever(_p: PausedRequest) -> None:
            await asyncio.Event().wait()

        class _Budgeted(_Server):
            @property
            def pause_budget_s(self) -> float:
                return 0.01

        server = _Budgeted("server", patterns=REQUEST_STAGE, on_paused=_forever)
        _browser, recorder, client, sid = await self._ready([server])
        _pause(client, sid, "q1", status=None)
        await asyncio.sleep(1.2)  # the budget + PAUSE_OWNER_GRACE_S
        assert _answers(client) == [self._failed("q1", sid)]
        await recorder.close()

    async def test_a_failed_send_is_failed_by_the_recorder(self) -> None:
        """``answered`` follows a *successful* send: a fulfil Chrome refused is still settled."""
        recorder_box: list[RecorderConnection] = []

        async def _fulfil(paused: PausedRequest) -> None:
            await recorder_box[0].send(
                "Fetch.fulfillRequest", {"requestId": paused.request_id}, paused.session_id
            )
            paused.answered = True

        server = _Server("server", patterns=REQUEST_STAGE, on_paused=_fulfil)
        browser, recorder, client, sid = await self._ready([server])
        recorder_box.append(recorder)
        browser.fail("Fetch.fulfillRequest", RuntimeError("CDP error: Invalid header"))
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert _answers(client) == [
            ("Fetch.fulfillRequest", "q1", sid, None),
            self._failed("q1", sid),
        ]
        await recorder.close()

    async def test_no_owner_for_the_target_fails_the_request(self) -> None:
        """A pause on a target no participant asked Fetch for still fails."""
        server = _Server("server", patterns=REQUEST_STAGE, frame_types=frozenset({"iframe"}))
        _browser, recorder, client, sid = await self._ready([server])
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert server.paused == []
        assert _answers(client) == [self._failed("q1", sid)]
        await recorder.close()

    async def test_a_pause_on_an_unprepared_session_fails(self) -> None:
        """``target is None``: a session the recorder never prepared (or already dropped)."""
        server = _Server("server", patterns=REQUEST_STAGE)
        _browser, recorder, client, _sid = await self._ready([server])
        _pause(client, "unknown-session", "q1", status=None)
        await _settle()
        assert server.paused == []
        assert _answers(client) == [self._failed("q1", "unknown-session")]
        await recorder.close()

    async def test_a_request_held_at_close_is_failed(self) -> None:
        async def _hold(_p: PausedRequest) -> None:
            await asyncio.Event().wait()

        server = _Server("server", patterns=REQUEST_STAGE, on_paused=_hold)
        _browser, recorder, client, sid = await self._ready([server], pause_owner_timeout_s=60)
        _pause(client, sid, "held", status=None)
        await _settle()
        assert _answers(client) == []
        mark = len(client.sent)
        await recorder.close()
        tail = [(s.method, s.session_id) for s in client.sent[mark:]]
        assert tail[:2] == [("Target.setAutoAttach", None), ("Fetch.failRequest", sid)]
        assert _answers(client) == [self._failed("held", sid)]

    async def test_serving_is_decided_by_the_participants_at_open(self) -> None:
        """A non-serving owner of the Request stage still continues (recording mode)."""
        owner = _Participant("m29", patterns=REQUEST_STAGE)
        _browser, recorder, client, sid = await self._ready([owner])
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert _answers(client) == [("Fetch.continueRequest", "q1", sid, None)]
        await recorder.close()

    async def test_a_serving_participant_beside_an_archiver_still_never_continues(self) -> None:
        """Whoever owns the stage, the Request pause is the server's to settle."""
        archiver = _Participant("archiver")  # a replay-log session: no patterns
        server = _Server("server", patterns=REQUEST_STAGE)  # answers nothing
        _browser, recorder, client, sid = await self._ready([archiver, server])
        _pause(client, sid, "q1", status=None)
        await _settle()
        assert [p.request_id for p in server.paused] == ["q1"]
        assert _answers(client) == [self._failed("q1", sid)]
        await recorder.close()


# ── cleanup and liveness ──────────────────────────────────────────────


class TestCleanup:
    async def test_fixed_order(self) -> None:
        async def _hold(_p: PausedRequest) -> None:
            await asyncio.Event().wait()

        browser = FakeBrowser()
        archiver = _Participant("archiver", patterns=RESPONSE_STAGE, on_paused=_hold)
        recorder, client = await _recorder(browser, [archiver], pause_owner_timeout_s=60)
        await recorder.open()
        page_sid = recorder.session_for_target("page-1")
        assert page_sid is not None
        _pause(client, page_sid, "held")
        await _settle()
        mark = len(client.sent)

        await recorder.close()
        tail = [(s.method, s.session_id) for s in client.sent[mark:]]
        assert tail == [
            ("Target.setAutoAttach", None),
            ("Fetch.continueResponse", page_sid),
            ("Fetch.disable", page_sid),
            ("Target.detachFromTarget", None),
        ]
        assert client.sent[mark].params == {
            "autoAttach": False,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        }
        assert client.stopped
        # The held pause's task was cancelled only after its request was continued.
        assert _continues(client) == [("Fetch.continueResponse", "held", page_sid)]

    async def test_still_waiting_targets_are_resumed_before_detaching(self) -> None:
        browser = FakeBrowser()
        gate = browser.hold("X.slow")
        plan = _Participant("plan", commands=(CDPCommand("X.slow"),))
        recorder, client = await _recorder(browser, [plan], command_timeout_s=30)
        task = asyncio.create_task(recorder.open())
        await _settle()
        gate.set()
        await task
        browser.hold("X.slow")  # the popup's preparation hangs
        sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        assert not browser.is_running("pop-1")
        mark = len(client.sent)
        await recorder.close()
        tail = [(s.method, s.session_id) for s in client.sent[mark:]]
        assert tail.index(("Runtime.runIfWaitingForDebugger", sid)) < tail.index(
            ("Target.detachFromTarget", None)
        )
        assert browser.is_running("pop-1")

    async def test_a_resume_that_fails_does_not_stop_the_cleanup(self) -> None:
        """A waiting target that is gone (or a browser too busy to answer) at close."""
        browser = FakeBrowser()
        gate = browser.hold("X.slow")
        plan = _Participant("plan", commands=(CDPCommand("X.slow"),), patterns=RESPONSE_STAGE)
        recorder, client = await _recorder(browser, [plan], command_timeout_s=30)
        task = asyncio.create_task(recorder.open())
        await _settle()
        gate.set()
        await task
        page_sid = recorder.session_for_target("page-1")
        browser.hold("X.slow")  # the popup's preparation hangs: still waiting at close
        sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        browser.fail("Runtime.runIfWaitingForDebugger", TimeoutError("no reply"), session_id=sid)
        tasks = [*recorder._attach_tasks, *recorder._pause_tasks]
        assert tasks, "the popup's preparation is still running"
        mark = len(client.sent)

        await recorder.close()  # does not raise

        tail = [(s.method, s.session_id) for s in client.sent[mark:]]
        assert ("Runtime.runIfWaitingForDebugger", sid) in tail
        assert {("Fetch.disable", page_sid), ("Fetch.disable", sid)} <= set(tail)
        detached = {
            s.params["sessionId"]
            for s in client.sent[mark:]
            if s.method == "Target.detachFromTarget"
        }
        assert {page_sid, sid} <= detached
        assert all(t.done() for t in tasks)
        assert not any(client.callbacks.values())
        assert client.stopped

    async def test_close_is_idempotent(self) -> None:
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [])
        await recorder.open()
        await recorder.close()
        sent = len(client.sent)
        await recorder.close()
        assert len(client.sent) == sent

    async def test_close_on_a_half_built_instance_only_stops_the_client(self) -> None:
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [])
        await recorder.close()
        assert client.sent == [] and client.stopped

    async def test_close_releases_its_event_callbacks(self) -> None:
        browser = FakeBrowser()
        scope = EventScope("archive")
        channel = ArchiveChannel(scope, lambda: [])
        recorder, client = await _recorder(browser, [], channel=channel)
        await recorder.open()
        assert client.callbacks["Fetch.requestPaused"]
        await recorder.close()
        assert not any(client.callbacks.values())


class TestLiveness:
    async def test_a_lost_connection_is_pushed_once_with_a_named_error(self) -> None:
        browser = FakeBrowser()
        lost: list[RecorderLostError] = []
        recorder, client = await _recorder(browser, [], lost=lost)
        await recorder.open()
        client.lose(ConnectionError("1006 abnormal closure"))
        assert len(lost) == 1
        assert isinstance(lost[0], RecorderLostError)
        assert "recorder connection lost" in str(lost[0]) and "1006" in str(lost[0])
        assert recorder.lost is lost[0]
        await recorder.close()  # tolerant of the dead client
        assert len(lost) == 1

    async def test_our_own_stop_is_not_a_loss(self) -> None:
        browser = FakeBrowser()
        lost: list[RecorderLostError] = []
        recorder, _client = await _recorder(browser, [], lost=lost)
        await recorder.open()
        await recorder.close()
        assert lost == [] and recorder.lost is None


# ── metadata into the archive scope ───────────────────────────────────


class TestArchiveChannel:
    async def test_shim_only_registers_no_metadata_handler(self) -> None:
        browser = FakeBrowser()
        declares = _Participant("would-be", events={"Network.requestWillBeSent"})
        recorder, client = await _recorder(browser, [declares], channel=None)
        await recorder.open()
        assert _registered(client) == RECORDER_OWN
        await recorder.close()

    async def test_metadata_reaches_the_scope_with_its_session(self) -> None:
        group = ScopeGroup("t")
        scope = await group.create_scope("__archive__")
        seen: list[RecorderEvent] = []
        network = _Participant("network", events={"Network.requestWillBeSent"})
        channel = ArchiveChannel(scope, lambda: [network])

        async def _consume(event: RecorderEvent) -> None:
            channel.mark_handled()
            seen.append(event)

        from agent_cdp import ConnectionType

        scope.connect(RecorderEvent, _consume, mode=ConnectionType.QUEUED)
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [network], channel=channel)
        await recorder.open()
        client.fire("Network.requestWillBeSent", {"requestId": "n1"}, "sess-9")
        await channel.barrier(timeout=1.0)
        admitted = [e for e in seen if e.method == "Network.requestWillBeSent"]
        assert [(e.session_id, e.params, e.consumer) for e in admitted] == [
            ("sess-9", {"requestId": "n1", "by": "network"}, network)
        ]
        # Target bookkeeping goes the same way, in order with preparation, to everyone.
        assert any(e.method == "Target.attachedToTarget" and e.consumer is None for e in seen)
        await recorder.close()
        await group.close_all()

    async def test_only_declared_events_are_subscribed(self) -> None:
        """Which events reach the archive is each participant's own declaration."""
        scope = EventScope("archive")
        first = _Participant("a", events={"Network.requestWillBeSent"})
        second = _Participant("b", events={"Network.loadingFinished", "Target.detachedFromTarget"})
        channel = ArchiveChannel(scope, lambda: [first, second])
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [first, second], channel=channel)
        await recorder.open()
        assert _registered(client) == RECORDER_OWN | {
            "Network.requestWillBeSent",
            "Network.loadingFinished",
        }
        # Target bookkeeping is the recorder's own, declared or not: one callback.
        assert len(client.callbacks["Target.detachedFromTarget"]) == 1
        await recorder.close()

    async def test_each_consumer_gets_only_what_it_admitted(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        group = ScopeGroup("t")
        scope = await group.create_scope("__archive__")
        first = _Participant("a", events={"Network.requestWillBeSent"})
        second = _Participant("b", events={"Network.requestWillBeSent"})
        bystander = _Participant("c")
        channel = ArchiveChannel(scope, lambda: [first, second, bystander])
        watchdog = ArchiveWatchdog(channel)
        watchdog.attach(EventScope("global-unused"))
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [first, second, bystander], channel=channel)
        await recorder.open()
        raw = {"requestId": "n1", "body": "x" * 1000}
        client.fire("Network.requestWillBeSent", raw, "sess-1")
        await channel.barrier(timeout=1.0)

        def _network(p: _Participant) -> list[dict[str, Any]]:
            return [e.params for e in p.events if e.method == "Network.requestWillBeSent"]

        assert _network(first) == [{"requestId": "n1", "by": "a"}]
        assert _network(second) == [{"requestId": "n1", "by": "b"}]
        assert _network(bystander) == [] and bystander.offered == []
        assert raw == {"requestId": "n1", "body": "x" * 1000}, "the raw event is not modified"
        history = [e for e in scope.event_history if isinstance(e, RecorderEvent)]
        assert all("body" not in e.params for e in history), "only what was admitted travels"
        watchdog.detach()
        await recorder.close()
        await group.close_all()

    async def test_a_failing_admission_is_counted_and_the_others_still_admit(self) -> None:
        class _Broken(_Participant):
            def admit_event(self, method: str, params: Any, session_id: Any) -> Any:
                raise RuntimeError("cannot admit")

        scope = EventScope("archive")
        broken = _Broken("broken", events={"Network.loadingFailed"})
        good = _Participant("good", events={"Network.loadingFailed"})
        channel = ArchiveChannel(scope, lambda: [broken, good])
        browser = FakeBrowser()
        recorder, client = await _recorder(browser, [broken, good], channel=channel)
        await recorder.open()
        before = channel.emitted
        client.fire("Network.loadingFailed", {"requestId": "n1"}, "sess-1")
        assert channel.errors == 1, "the next collection fails instead of looking complete"
        assert channel.emitted == before + 1 and len(good.offered) == 1
        await recorder.close()

    async def test_the_barrier_detects_dropped_events(self) -> None:
        group = ScopeGroup("t")
        scope = await group.create_scope("__archive__")
        channel = ArchiveChannel(scope, lambda: [])

        async def _consume(_event: Any) -> None:
            channel.mark_handled()

        from agent_cdp import ConnectionType

        scope.connect(RecorderEvent, _consume, mode=ConnectionType.QUEUED)
        # The scope queue holds 1024; emitting more without yielding drops the newest.
        for i in range(1100):
            channel.emit(
                RecorderEvent(session_id="s", method="Network.loadingFinished", params={"i": i})
            )
        with pytest.raises(ArchiveChannelError, match="dropped"):
            await channel.barrier(timeout=2.0)
        await group.close_all()

    async def test_the_barrier_passes_when_everything_was_handled(self) -> None:
        group = ScopeGroup("t")
        scope = await group.create_scope("__archive__")
        channel = ArchiveChannel(scope, lambda: [])

        async def _consume(_event: Any) -> None:
            channel.mark_handled()

        from agent_cdp import ConnectionType

        scope.connect(RecorderEvent, _consume, mode=ConnectionType.QUEUED)
        for i in range(10):
            channel.emit(
                RecorderEvent(session_id="s", method="Network.loadingFinished", params={"i": i})
            )
        await channel.barrier(timeout=2.0)
        assert channel.handled == channel.emitted == 10
        await group.close_all()


# ── the capability, end to end, with a fake browser ───────────────────


class _OwnBrowser:
    """A provider that launches a browser the capability owns."""

    __capabilities__: ClassVar[set[str]] = {CDP, NETWORK_CAPTURE, LAUNCH_ARGS}
    provider_id = "fake_local"

    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.closed = 0

    async def open(self, **kwargs: Any) -> ProviderSession:
        self.open_calls.append(kwargs)
        return ProviderSession(
            session_id="s",
            provider_id=self.provider_id,
            cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
        )

    async def close(self, session: ProviderSession | None = None) -> None:
        self.closed += 1

    def launch_arg_conflicts(self, launch_args: Any) -> list[str]:
        return []

    def planned_launch_flags(self, launch_args: Any) -> list[str]:
        return list(launch_args)

    def persistent_profile(self) -> str | None:
        return None


#: Overrides with one owner each (§4.14 ownership table).
RECORDER_OWNED = (
    "Emulation.setTimezoneOverride",
    "Emulation.setLocaleOverride",
    "Emulation.setUserAgentOverride",
    "Emulation.setEmulatedMedia",
    "Page.addScriptToEvaluateOnNewDocument",
    "Network.setCacheDisabled",
    "Network.setBypassServiceWorker",
    "Network.setUserAgentOverride",
)
CONTROL_OWNED = ("Emulation.setDeviceMetricsOverride",)


async def _capability(
    replayability: Any = None,
) -> tuple[BrowserCapability, FakeBrowser, _OwnBrowser]:
    browser = FakeBrowser()
    cap = BrowserCapability()
    backend = _OwnBrowser()
    cap.backend = backend  # type: ignore[assignment]
    if replayability is not None:
        cap.replayability = replayability
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    return cap, browser, backend


class TestZeroOverhead:
    async def test_nothing_bound_off_tier_opens_nothing(self) -> None:
        cap, browser, backend = await _capability()
        try:
            assert len(browser.clients) == 1, "off + no archiver: no second client"
            assert backend.open_calls == [{}], "off passes no launch switches"
            assert cap._recorder is None
            assert cap._scope_group is not None
            assert "__archive__" not in cap._scope_group.scope_ids
            assert not [w for w in cap._shared_watchdogs if type(w).__name__ == "ArchiveWatchdog"]
            control = browser.control
            assert control.options == {
                "additional_headers": None,
                "proxy": None,
                "compression": None,
            }
            assert not [m for m in control.methods() if m in RECORDER_OWNED]
        finally:
            await cap.teardown()

    async def test_pinned_alone_is_shim_only(self) -> None:
        pinned = PinnedReplayability(random_seed=4, timezone="UTC", locale="en-US")
        cap, browser, backend = await _capability(pinned)
        try:
            assert backend.open_calls == [{"launch_args": pinned.launch_args()}]
            assert len(browser.clients) == 2
            recorder_client = browser.recorder
            assert recorder_client.options == {
                "additional_headers": None,
                "max_frame_size": None,
                "compression": None,
                "proxy": None,
            }
            assert cap._recorder is not None and cap._recorder.channel is None
            # Shim-only: no Fetch, no metadata handler, no archive scope.
            assert "Fetch.enable" not in recorder_client.methods()
            assert _registered(recorder_client) == RECORDER_OWN
            assert cap._scope_group is not None
            assert "__archive__" not in cap._scope_group.scope_ids
            # The plan reached the existing tab before the control client used it.
            assert "Page.addScriptToEvaluateOnNewDocument" in recorder_client.methods()
        finally:
            await cap.teardown()

    async def test_the_control_connection_sends_the_same_commands_on_and_off(self) -> None:
        off_cap, off_browser, _ = await _capability()
        await off_cap.execute_action(Action(name="browser.wait", params={"seconds": 0}))
        await off_cap.teardown()
        pinned_cap, pinned_browser, _ = await _capability(
            PinnedReplayability(device_scale_factor=1.0)
        )
        await pinned_cap.execute_action(Action(name="browser.wait", params={"seconds": 0}))
        await pinned_cap.teardown()

        def _shape(client: FakeClient) -> list[str]:
            return [s.method for s in client.sent]

        assert _shape(off_browser.control) == _shape(pinned_browser.control)


class TestOverrideOwnership:
    async def test_no_override_is_ever_sent_on_both_clients(self) -> None:
        cap, browser, _ = await _capability(PinnedReplayability())
        try:
            browser.open_target("pop-1", "page")
            browser.open_target("w-1", "worker")
            await _settle()
            control = set(browser.control.methods())
            recorder = set(browser.recorder.methods())
            assert not control & set(RECORDER_OWNED)
            assert not recorder & set(CONTROL_OWNED)
            assert "Emulation.setVirtualTimePolicy" not in control | recorder
            assert {
                "Emulation.setTimezoneOverride",
                "Page.addScriptToEvaluateOnNewDocument",
            } <= recorder
            assert "Emulation.setDeviceMetricsOverride" in control
        finally:
            await cap.teardown()

    async def test_the_viewport_dpr_is_the_tiers(self) -> None:
        cap, browser, _ = await _capability(PinnedReplayability(device_scale_factor=2.0))
        try:
            (metrics,) = [
                s for s in browser.control.sent if s.method == "Emulation.setDeviceMetricsOverride"
            ]
            assert metrics.params["deviceScaleFactor"] == 2.0
        finally:
            await cap.teardown()
        off_cap, off_browser, _ = await _capability()
        try:
            (metrics,) = [
                s
                for s in off_browser.control.sent
                if s.method == "Emulation.setDeviceMetricsOverride"
            ]
            assert metrics.params["deviceScaleFactor"] == 0
        finally:
            await off_cap.teardown()

    async def test_the_control_client_never_waits_for_a_debugger(self) -> None:
        cap, browser, _ = await _capability(PinnedReplayability())
        try:
            for sent in browser.control.sent:
                if sent.method == "Target.setAutoAttach":
                    assert sent.params.get("waitForDebuggerOnStart") is not True
        finally:
            await cap.teardown()


class TestCapabilityLiveness:
    async def test_recorder_loss_fails_closed(self) -> None:
        cap, browser, _ = await _capability(PinnedReplayability())
        browser.hold("Page.captureScreenshot")
        waiting = asyncio.create_task(
            browser.control.send("Page.captureScreenshot", {}, "some-session")
        )
        await _settle()
        browser.recorder.lose(ConnectionError("recorder socket dropped"))
        # Bounded: a capability that forgot to fail the control client's
        # waiters would otherwise leave this test (and the run) hanging.
        with pytest.raises(RecorderLostError):
            await asyncio.wait_for(waiting, 2.0)
        assert cap.is_alive is False
        with pytest.raises(RecorderLostError, match="recorder connection lost"):
            await cap.begin_observation(1)
        with pytest.raises(RecorderLostError):
            await cap.execute_action(Action(name="browser.wait", params={"seconds": 0}))
        await cap.teardown()

    async def test_an_action_that_completes_after_the_loss_is_not_a_success(self) -> None:
        """§4.14: an action that finishes while the recorder was lost acted unrecorded."""
        cap, browser, _ = await _capability(PinnedReplayability())
        try:
            acting = asyncio.create_task(
                cap.execute_action(Action(name="browser.wait", params={"seconds": 0.2}))
            )
            await _settle()
            assert not acting.done(), "the action is still in flight"
            browser.recorder.lose(ConnectionError("recorder socket dropped"))
            with pytest.raises(RecorderLostError, match="recorder connection lost"):
                await asyncio.wait_for(acting, 2.0)
        finally:
            await cap.teardown()

    async def test_a_capture_cut_by_recorder_loss_reports_the_loss(self) -> None:
        cap, browser, _ = await _capability(PinnedReplayability())
        try:
            cap.apply_negotiation({"browser.browser_meta"})
            await cap.begin_observation(1)
            browser.hold("Page.getNavigationHistory")
            reading = asyncio.create_task(cap.resolve_fragment("browser.browser_meta"))
            await _settle()
            browser.recorder.lose(ConnectionError("recorder socket dropped"))
            with pytest.raises(RecorderLostError):
                await asyncio.wait_for(reading, 5.0)
        finally:
            await cap.teardown()

    async def test_teardown_releases_the_recorder_before_the_control_client(self) -> None:
        cap, browser, backend = await _capability(PinnedReplayability())
        await cap.teardown()
        assert browser.recorder.stopped and browser.control.stopped
        assert backend.closed == 1
        recorder_tail = [s.method for s in browser.recorder.sent][-2:]
        assert recorder_tail[-1] == "Target.detachFromTarget"
        assert cap._recorder is None
        await cap.teardown()  # a second teardown does nothing


# ── ArchiveWatchdog: installed by binding, drains the archive scope ───


class _CycleParticipant(_Participant):
    @property
    def wants_cycles(self) -> bool:
        return True

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.cycles: list[Any] = []

    async def on_cycle(self, cycle: Any) -> None:
        self.cycles.append(cycle)


class TestArchiveWatchdog:
    async def _channel(self, participants: list[Any]) -> tuple[ScopeGroup, ArchiveChannel]:
        group = ScopeGroup("t")
        scope = await group.create_scope("__archive__")
        return group, ArchiveChannel(scope, lambda: participants)

    def test_installed_only_with_an_archive_channel(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        cap = BrowserCapability()
        cap._cdp = FakeBrowser().client("ws://x")  # type: ignore[assignment]
        assert ArchiveWatchdog.should_install(cap._watchdog_context(None)) is False
        assert not [
            w
            for w in cap._create_watchdogs_for_scope(level="shared")
            if isinstance(w, ArchiveWatchdog)
        ]
        # Built from a recorder-less context it is inert.
        inert = ArchiveWatchdog.build(cap._watchdog_context("sid1"))
        inert.attach(EventScope("global"))
        inert.bind_tab_scopes(EventScope("o"), EventScope("j"), session_id="sid1")
        inert.unbind_tab("sid1")
        inert.detach()

    async def test_installed_when_the_recorder_carries_a_channel(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        group, channel = await self._channel([])

        class _Recorder:
            def __init__(self, ch: ArchiveChannel) -> None:
                self.channel = ch

        cap = BrowserCapability()
        cap._cdp = FakeBrowser().client("ws://x")  # type: ignore[assignment]
        cap._recorder = _Recorder(channel)  # type: ignore[assignment]
        ctx = cap._watchdog_context(None)
        assert ctx.archive is channel and ArchiveWatchdog.should_install(ctx) is True
        shared = cap._create_watchdogs_for_scope(level="shared")
        assert [type(w) for w in shared if isinstance(w, ArchiveWatchdog)] == [ArchiveWatchdog]
        assert ArchiveWatchdog.provides == frozenset() and ArchiveWatchdog.level == "shared"
        cap._recorder = None
        await group.close_all()

    async def test_events_fan_out_to_every_participant_and_are_counted(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        first, second = _Participant("a"), _Participant("b")
        group, channel = await self._channel([first, second])
        watchdog = ArchiveWatchdog(channel)
        watchdog.attach(EventScope("global-unused"))
        channel.emit(RecorderEvent(session_id="s1", method="Network.loadingFinished", params={}))
        await channel.barrier(timeout=1.0)
        assert [e.method for e in first.events] == ["Network.loadingFinished"]
        assert [e.session_id for e in second.events] == ["s1"]
        assert channel.handled == channel.emitted == 1
        watchdog.detach()
        await group.close_all()

    async def test_an_admitted_event_reaches_only_its_consumer(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        first, second = _Participant("a"), _Participant("b")
        group, channel = await self._channel([first, second])
        watchdog = ArchiveWatchdog(channel)
        watchdog.attach(EventScope("global-unused"))
        channel.emit(
            RecorderEvent(
                session_id="s1", method="Network.loadingFinished", params={}, consumer=second
            )
        )
        await channel.barrier(timeout=1.0)
        assert first.events == [] and [e.method for e in second.events] == [
            "Network.loadingFinished"
        ]
        assert channel.handled == channel.emitted == 1
        watchdog.detach()
        await group.close_all()

    async def test_a_failing_participant_is_counted_not_fatal(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog

        class _Bad(_Participant):
            def on_event(self, event: RecorderEvent) -> None:
                raise RuntimeError("broken session")

        good = _Participant("good")
        group, channel = await self._channel([_Bad("bad"), good])
        watchdog = ArchiveWatchdog(channel)
        watchdog.attach(EventScope("g"))
        channel.emit(RecorderEvent(session_id="s1", method="Network.loadingFailed", params={}))
        await channel.barrier(timeout=1.0)
        assert channel.errors == 1 and len(good.events) == 1
        watchdog.detach()
        await group.close_all()

    async def test_cycles_reach_only_participants_that_want_them(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog
        from spectragent.core.capabilities.browser.watchdogs.events import (
            BrowserStateRequestEvent,
        )

        cycler, plain = _CycleParticipant("dom_raw"), _Participant("network")
        group, channel = await self._channel([cycler, plain])
        obs = await group.create_scope("T1", target_id="T1")
        js = await group.create_scope("T1:js", target_id="T1")
        watchdog = ArchiveWatchdog(channel)
        watchdog.bind_tab_scopes(obs, js, session_id="ctl-1")

        event = BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        obs.emit(event)
        await event
        (cycle,) = cycler.cycles
        assert (cycle.target_id, cycle.control_session_id, cycle.fragments) == (
            "T1",
            "ctl-1",
            frozenset({"browser.screenshot"}),
        )
        # The archive side contributes no summary to the observation.
        assert all(r.result is None for r in event.event_results.values())

        watchdog.unbind_tab("ctl-1")
        again = BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        obs.emit(again)
        await again
        assert len(cycler.cycles) == 1
        await group.close_all()

    async def test_no_cycle_connection_when_nobody_wants_cycles(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import ArchiveWatchdog
        from spectragent.core.capabilities.browser.watchdogs.events import (
            BrowserStateRequestEvent,
        )

        group, channel = await self._channel([_Participant("network")])
        obs = await group.create_scope("T1", target_id="T1")
        js = await group.create_scope("T1:js", target_id="T1")
        watchdog = ArchiveWatchdog(channel)
        watchdog.bind_tab_scopes(obs, js, session_id="ctl-1")
        event = BrowserStateRequestEvent(fragments=frozenset({"browser.dom"}))
        obs.emit(event)
        await event
        assert event.event_results == {}
        await group.close_all()
