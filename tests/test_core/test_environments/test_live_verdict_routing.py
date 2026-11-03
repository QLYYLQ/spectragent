"""Judging live through the environment: every bound live verifier, concurrently, each within its limit.

``docs/architecture_design.md`` §4.3 "判分的路由" and §4.9 "基准组件由 capability
承载": ``bind_live_verifiers`` routes each live verifier to the capability it
names (one implementing ``LiveVerifierHost``), reading each identity once, and
``verify_live`` gives each bound verifier a task of its own through its host,
runs them concurrently, limits each to ``timeout_s`` and hands each the run's
``answer`` unchanged (§4.2: the text ``loop.done`` handed over, or ``None``).
A judgment becomes a ``judged`` live verdict under the verifier's identity,
with the start time and duration the environment measured around the call; a
timeout, an exception or a broken contract comes back as a ``not_judged``
verdict under the same identity; the verdicts come back in capability order,
then in the order the verifiers were listed, and the call never raises
``Exception``. A cancellation cancels the unfinished verifiers and waits for
them before it propagates. Nothing bound, no verdict.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Sequence
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import LiveVerifierHost
from spectragent.core.environments import Environment, EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.verdict import Judgment, Verdict, VerifierIdentity
from spectragent.verifiers.base import LiveVerifier, VerifierNeed, attachment_of
from tests._helpers.assembly import make_env


class _Verifier:
    """A live verifier hosted by *capability_id*; replies with *reply*: a judgment, an object, or an exception.

    It keeps every run answer it is handed in *given*. It waits for *wait_for*
    first, when given. A cancellation is noted, and it takes a few loop turns
    to wind down before it lets it through — the cleanup ``verify_live`` must
    wait out.
    """

    needs: frozenset[VerifierNeed] = frozenset({"live_environment", "answer"})

    def __init__(
        self, capability_id: str, reply: Any = None, *, verifier_id: str | None = None
    ) -> None:
        self.capability_id = capability_id
        self.verifier_id = verifier_id or f"judge_{capability_id}"
        self.reply = reply
        self.given: list[str | None] = []
        self.wait_for: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.replied = asyncio.Event()
        self.was_cancelled = False
        self.wound_down = False
        self.identities_read = 0
        self.settings: dict[str, Any] = {}

    def identity(self) -> VerifierIdentity:
        self.identities_read += 1
        return VerifierIdentity(
            verifier_id=self.verifier_id, version="v1", config_signature=self.settings
        )

    async def judge(self, answer: str | None) -> Any:
        self.given.append(answer)
        self.entered.set()
        try:
            if self.wait_for is not None:
                await self.wait_for.wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            for _ in range(3):
                await asyncio.sleep(0)
            self.wound_down = True
            raise
        self.replied.set()
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class _Plain(CapabilityBase):
    """A capability that hosts no live verifier."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "verdict_route_plain"

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("judging never executes an action")


class _Host(_Plain):
    """Hosts live verifiers: judges with each by handing it the run's answer."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "verdict_route_host"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.bound: list[Sequence[LiveVerifier]] = []
        self.checked: list[Sequence[LiveVerifier]] = []

    def check_setup(
        self,
        context: Any,
        *,
        negotiation: Any = None,
        archivers: Any = (),
        setup: Any = None,
        live_verifiers: Sequence[LiveVerifier] = (),
    ) -> None:
        self.checked.append(tuple(live_verifiers))

    def bind_setup(self, setup: Any) -> None:
        return None

    def bind_live_verifiers(self, verifiers: Sequence[LiveVerifier]) -> None:
        self.bound.append(tuple(verifiers))

    async def verify_live(self, verifier: LiveVerifier, *, answer: str | None) -> Judgment:
        assert isinstance(verifier, _Verifier)
        return await verifier.judge(answer)


class _SecondHost(_Host):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "verdict_route_second"


HOST, SECOND, PLAIN = "verdict_route_host", "verdict_route_second", "verdict_route_plain"


def _judgment(score: float = 1.0) -> Judgment:
    return Judgment(score=score, evidence={"task": "t-1"})


def _env(*verifiers: _Verifier, **capabilities: CapabilityBase) -> EnvironmentBase:
    env = make_env(EnvironmentBase, dict(capabilities))
    env.bind_live_verifiers(verifiers)
    return env


class TestTheContract:
    def test_the_protocols_are_structural(self) -> None:
        assert isinstance(_Host(), LiveVerifierHost)
        assert not isinstance(_Plain(), LiveVerifierHost)
        verifier = _Verifier(HOST)
        assert isinstance(verifier, LiveVerifier)
        assert attachment_of(verifier) == "live"

    def test_the_environment_protocol_declares_it_and_the_base_implements_it(self) -> None:
        for owner in (Environment, EnvironmentBase):
            parameters = inspect.signature(owner.verify_live).parameters
            assert list(parameters) == ["self", "timeout_s", "answer"]
            assert all(
                parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
                and parameters[name].default is inspect.Parameter.empty
                for name in ("timeout_s", "answer")
            ), "both named, both required: no caller can leave the answer out by accident"
            assert list(inspect.signature(owner.bind_live_verifiers).parameters) == [
                "self",
                "verifiers",
            ]

    def test_a_host_takes_the_verifier_and_the_answer_by_name(self) -> None:
        parameters = inspect.signature(LiveVerifierHost.verify_live).parameters
        assert list(parameters) == ["self", "verifier", "answer"]
        assert parameters["answer"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["answer"].default is inspect.Parameter.empty

    def test_the_capability_base_gives_no_default(self) -> None:
        assert not hasattr(CapabilityBase, "verify_live")
        assert not hasattr(CapabilityBase, "bind_live_verifiers")


class TestRouting:
    def test_each_verifier_goes_to_the_capability_it_names(self) -> None:
        first, second, third = _Verifier(HOST), _Verifier(SECOND), _Verifier(HOST, verifier_id="x")
        host, other = _Host(), _SecondHost()
        make_env(EnvironmentBase, {"a": host, "b": other, "p": _Plain()}).bind_live_verifiers(
            [first, second, third]
        )
        assert host.bound == [(first, third)] and other.bound == [(second,)]

    def test_a_host_that_gets_none_is_bound_to_none(self) -> None:
        host, other = _Host(), _SecondHost()
        make_env(EnvironmentBase, {"a": host, "b": other}).bind_live_verifiers([_Verifier(HOST)])
        assert other.bound == [()]

    def test_check_setup_routes_them_the_same_way_and_binds_nothing(self) -> None:
        verifier = _Verifier(HOST)
        host = _Host()
        make_env(EnvironmentBase, {"a": host, "b": _SecondHost()}).check_setup(
            {}, live_verifiers=[verifier]
        )
        assert host.checked == [(verifier,)] and host.bound == []

    def test_one_whose_capability_is_not_here_is_refused_and_nothing_is_bound(self) -> None:
        host = _SecondHost()
        env = make_env(EnvironmentBase, {"s": host, "p": _Plain()})
        with pytest.raises(ValueError) as refused:
            env.bind_live_verifiers([_Verifier(SECOND), _Verifier(HOST)])
        assert str(refused.value) == (
            "the live verifier 'judge_verdict_route_host' is hosted by the "
            "'verdict_route_host' capability, and this environment has none (it has "
            "verdict_route_plain, verdict_route_second)"
        )
        assert host.bound == [], "validated before anything was bound"

    def test_an_environment_with_no_capability_says_so(self) -> None:
        with pytest.raises(ValueError, match=r"has none \(it has no capability\)"):
            make_env(EnvironmentBase, {}).bind_live_verifiers([_Verifier(HOST)])

    def test_one_whose_capability_hosts_no_verifier_is_refused(self) -> None:
        env = make_env(EnvironmentBase, {"p": _Plain()})
        with pytest.raises(ValueError, match="which hosts no live verifier"):
            env.bind_live_verifiers([_Verifier(PLAIN)])

    def test_two_that_would_write_one_verdict_file_are_refused(self) -> None:
        env = make_env(EnvironmentBase, {"a": _Host()})
        with pytest.raises(ValueError, match="would all write the verdict 'judge_verdict_route"):
            env.bind_live_verifiers([_Verifier(HOST), _Verifier(HOST)])

    async def test_the_same_kind_with_other_settings_writes_its_own_file(self) -> None:
        tuned = _Verifier(HOST, _judgment(0.5))
        tuned.settings = {"strict": True}
        env = _env(_Verifier(HOST, _judgment()), tuned, a=_Host())
        verdicts = await env.verify_live(timeout_s=1, answer=None)
        assert len({v.slug for v in verdicts}) == 2

    async def test_each_identity_is_read_once_when_bound(self) -> None:
        verifier = _Verifier(HOST, _judgment())
        env = _env(verifier, a=_Host())
        await env.verify_live(timeout_s=1, answer=None)
        await env.verify_live(timeout_s=1, answer=None)
        assert verifier.identities_read == 1

    async def test_binding_again_replaces_what_was_bound(self) -> None:
        env = _env(_Verifier(HOST, _judgment()), a=_Host())
        env.bind_live_verifiers([])
        assert await env.verify_live(timeout_s=1, answer=None) == []


class TestTheAnswer:
    @pytest.mark.parametrize("answer", ["Mall", None])
    async def test_every_verifier_is_handed_the_runs_answer_unchanged(
        self, answer: str | None
    ) -> None:
        first, second = _Verifier(HOST, _judgment()), _Verifier(SECOND, _judgment())
        env = _env(first, second, a=_Host(), plain=_Plain(), b=_SecondHost())
        await env.verify_live(timeout_s=1, answer=answer)
        assert first.given == second.given == [answer]

    async def test_the_answer_is_not_copied_into_the_verdict(self) -> None:
        """It is in ``result.json``; a verifier that judges it says so in its own evidence."""
        (verdict,) = await _env(_Verifier(HOST, _judgment()), a=_Host()).verify_live(
            timeout_s=1, answer="Mall"
        )
        assert "Mall" not in verdict.model_dump_json()


class TestNothingBound:
    async def test_nothing_bound_is_an_empty_list(self) -> None:
        assert await _env(plain=_Plain()).verify_live(timeout_s=1, answer=None) == []
        assert await _env().verify_live(timeout_s=1, answer=None) == []
        host = _Host()
        assert (
            await make_env(EnvironmentBase, {"a": host}).verify_live(timeout_s=1, answer=None) == []
        )


class TestJudging:
    async def test_a_judgment_is_a_live_verdict_under_the_verifiers_identity(self) -> None:
        before = time.time()
        (verdict,) = await _env(_Verifier(HOST, _judgment(0.5)), a=_Host()).verify_live(
            timeout_s=1, answer=None
        )
        assert verdict.model_dump(exclude={"started_at", "duration_ms"}) == {
            "schema_version": "1.0",
            "verifier": {
                "verifier_id": "judge_verdict_route_host",
                "version": "v1",
                "model": None,
                "config_signature": {},
            },
            "attachment": "live",
            "status": "judged",
            "score": 0.5,
            "label": None,
            "rationale": None,
            "evidence": {"task": "t-1"},
            "usage": None,
            "reason": None,
            "detail": None,
        }
        assert before <= verdict.started_at <= time.time()

    async def test_the_recorded_duration_covers_a_slow_verifiers_time(self) -> None:
        """Measured around the call: a verifier cannot under-report how long it took."""

        class _Slow(_Verifier):
            async def judge(self, answer: str | None) -> Any:
                await asyncio.sleep(0.2)
                return await super().judge(answer)

        (verdict,) = await _env(_Slow(HOST, _judgment()), a=_Host()).verify_live(
            timeout_s=30, answer=None
        )
        assert verdict.status == "judged"
        assert verdict.duration_ms >= 190  # its 200 ms, give or take the timer's tick

    async def test_the_verifiers_run_concurrently(self) -> None:
        """Each waits until the other has started: only possible when both run at once."""
        first, second = _Verifier(HOST, _judgment()), _Verifier(SECOND, _judgment())
        first.wait_for, second.wait_for = second.entered, first.entered

        verdicts = await _env(first, second, a=_Host(), b=_SecondHost()).verify_live(
            timeout_s=5, answer=None
        )

        assert [(v.verifier.verifier_id, v.status) for v in verdicts] == [
            ("judge_verdict_route_host", "judged"),
            ("judge_verdict_route_second", "judged"),
        ]

    async def test_the_verdicts_come_back_in_capability_order_then_listed_order(self) -> None:
        """Not in the order the verifiers finished: the first one here finishes last."""
        later = _Verifier(SECOND, _judgment())
        sooner = _Verifier(HOST, _judgment())
        listed_second = _Verifier(HOST, _judgment(), verifier_id="second_on_host")
        later.wait_for = sooner.replied

        verdicts = await _env(
            later, listed_second, sooner, b=_SecondHost(), plain=_Plain(), a=_Host()
        ).verify_live(timeout_s=5, answer=None)

        assert [v.verifier.verifier_id for v in verdicts] == [
            "judge_verdict_route_second",
            "second_on_host",
            "judge_verdict_route_host",
        ]


class TestAFailedVerifierJudgesNothing:
    async def test_one_past_its_limit_is_cancelled_and_not_judged_by_timeout(self) -> None:
        hung, healthy = _Verifier(HOST, _judgment()), _Verifier(SECOND, _judgment())
        hung.wait_for = asyncio.Event()  # never set
        before = time.time()

        hung_verdict, healthy_verdict = await _env(
            hung, healthy, a=_Host(), b=_SecondHost()
        ).verify_live(timeout_s=0.05, answer=None)

        assert hung_verdict.model_dump(
            include={"verifier", "attachment", "status", "score", "label", "reason", "detail"}
        ) == {
            "verifier": {
                "verifier_id": "judge_verdict_route_host",
                "version": "v1",
                "model": None,
                "config_signature": {},
            },
            "attachment": "live",
            "status": "not_judged",
            "score": None,
            "label": None,
            "reason": "timeout",
            "detail": "no verdict within 0.05 s (verify_timeout)",
        }
        assert before <= hung_verdict.started_at <= time.time()
        assert hung_verdict.duration_ms >= 40  # the wait it was cut off from, give or take a tick
        # Its limit cancelled it and waited for it; the other verifier still judged.
        assert hung.was_cancelled and hung.wound_down
        assert (healthy_verdict.verifier.verifier_id, healthy_verdict.status) == (
            "judge_verdict_route_second",
            "judged",
        )

    async def test_one_that_raises_is_not_judged_by_error_under_its_identity(self) -> None:
        broken = _Verifier(HOST, RuntimeError("adb went away"))
        (verdict,) = await _env(broken, a=_Host()).verify_live(timeout_s=1, answer=None)
        assert (verdict.slug, verdict.status, verdict.reason, verdict.detail) == (
            broken.identity().slug,
            "not_judged",
            "error",
            "RuntimeError: adb went away",
        )
        assert verdict.score is None and verdict.label is None

    async def test_its_own_timeout_error_is_an_error_not_the_limit(self) -> None:
        """The limit is the environment's clock: a verifier's own ``TimeoutError`` is what it raised."""
        verifier = _Verifier(HOST, TimeoutError("server took too long"))
        (verdict,) = await _env(verifier, a=_Host()).verify_live(timeout_s=5, answer=None)
        assert (verdict.reason, verdict.detail) == ("error", "TimeoutError: server took too long")

    @pytest.mark.parametrize(
        ("reply", "returned"),
        [
            ({"score": 1.0}, "dict"),
            (None, "NoneType"),
            (
                Verdict(
                    verifier=VerifierIdentity(verifier_id="judge_verdict_route_host"),
                    attachment="live",
                    status="judged",
                    score=1.0,
                    started_at=0.0,
                    duration_ms=0,
                ),
                "Verdict",
            ),
        ],
        ids=["a mapping", "nothing", "the record instead of a judgment"],
    )
    async def test_anything_but_a_judgment_is_not_judged_by_error(
        self, reply: Any, returned: str
    ) -> None:
        (verdict,) = await _env(_Verifier(HOST, reply), a=_Host()).verify_live(
            timeout_s=1, answer=None
        )
        assert (verdict.status, verdict.reason, verdict.detail) == (
            "not_judged",
            "error",
            f"TypeError: verify_live() returned {returned}, not a Judgment",
        )


class TestCancellation:
    async def test_cancelling_the_caller_cancels_and_awaits_every_unfinished_verifier(
        self,
    ) -> None:
        done, hung = _Verifier(HOST, _judgment()), _Verifier(SECOND, _judgment())
        hung.wait_for = asyncio.Event()  # never set
        env = _env(done, hung, a=_Host(), b=_SecondHost())
        task = asyncio.ensure_future(env.verify_live(timeout_s=30, answer=None))
        await asyncio.wait_for(hung.entered.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Wound down before the cancellation reached the caller: nothing outlives the call.
        assert hung.was_cancelled and hung.wound_down
        assert not done.was_cancelled
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    async def test_a_verifier_that_is_cancelled_itself_leaves_no_sibling_running(self) -> None:
        """``gather`` would let the sibling of a cancelled child run on, unobserved."""
        cancelled = _Verifier(HOST, asyncio.CancelledError())
        hung = _Verifier(SECOND, _judgment())
        hung.wait_for = asyncio.Event()  # never set
        cancelled.wait_for = hung.entered

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                _env(cancelled, hung, a=_Host(), b=_SecondHost()).verify_live(
                    timeout_s=30, answer=None
                ),
                timeout=5,
            )

        assert hung.was_cancelled and hung.wound_down
