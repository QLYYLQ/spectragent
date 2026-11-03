"""Judging live through the environment: every task oracle, concurrently, each within its limit.

``docs/architecture_design.md`` §4.3 "判分的路由" and §4.9 ``TaskOracle``:
``verify_live`` gives each capability implementing the protocol whose oracle
judges the run (``oracle_identity()`` is not ``None``) a task of its own, runs
them concurrently, limits each to ``timeout_s`` and hands each the run's
``answer`` unchanged (§4.2: the text ``loop.done`` handed over, or ``None``). A
judgment becomes a ``judged`` live verdict under the oracle's identity, with
the start time and duration the environment measured around the call; a
timeout, an exception or a broken contract comes back as a ``not_judged``
verdict under the same identity; the verdicts come back in capability order,
and the call never raises ``Exception``. A cancellation cancels the unfinished
oracles and waits for them before it propagates. No oracle, no verdict.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import TaskOracle
from spectragent.core.environments import Environment, EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.verdict import Judgment, Verdict, VerifierIdentity
from tests._helpers.assembly import make_env


class _Plain(CapabilityBase):
    """A capability without a task oracle."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "oracle_route_plain"

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("scoring never executes an action")


class _Oracle(_Plain):
    """Replies with *reply*: a judgment, an object to return, or an exception to raise.

    It keeps every run answer it is handed in *given*. It waits for *wait_for*
    first, when given. A cancellation is noted, and the oracle takes a few loop
    turns to wind down before it lets it through — the cleanup ``verify_live``
    must wait out. *identity* ``None`` is a capability whose oracle judges
    nothing in this run.
    """

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "oracle_route_cap"

    def __init__(
        self, reply: Any = None, *, identity: VerifierIdentity | None = None, **kw: Any
    ) -> None:
        super().__init__(**kw)
        self.reply = reply
        self.identity = (
            VerifierIdentity(verifier_id=f"judge_{self.capability_id}", version="v1")
            if identity is None
            else identity
        )
        self.judges = True
        self.given: list[str | None] = []
        self.wait_for: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.replied = asyncio.Event()
        self.was_cancelled = False
        self.wound_down = False

    def oracle_identity(self) -> VerifierIdentity | None:
        return self.identity if self.judges else None

    async def read_score(self, *, answer: str | None) -> Judgment:
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


class _Second(_Oracle):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "oracle_route_second"


def _judgment(score: float = 1.0) -> Judgment:
    return Judgment(score=score, evidence={"task": "t-1"})


def _silent(oracle: _Oracle) -> _Oracle:
    """*oracle*, judging nothing in this run (its identity is ``None``)."""
    oracle.judges = False
    return oracle


def _env(**capabilities: CapabilityBase) -> EnvironmentBase:
    return make_env(EnvironmentBase, dict(capabilities))


class TestTheContract:
    def test_the_protocol_is_structural(self) -> None:
        assert isinstance(_Oracle(), TaskOracle)
        assert not isinstance(_Plain(), TaskOracle)

    def test_the_environment_protocol_declares_it_and_the_base_implements_it(self) -> None:
        assert hasattr(Environment, "verify_live")
        for owner in (Environment, EnvironmentBase):
            parameters = inspect.signature(owner.verify_live).parameters
            assert list(parameters) == ["self", "timeout_s", "answer"]
            assert all(
                parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
                and parameters[name].default is inspect.Parameter.empty
                for name in ("timeout_s", "answer")
            ), "both named, both required: no caller can leave the answer out by accident"

    def test_an_oracle_takes_the_answer_by_name(self) -> None:
        parameters = inspect.signature(TaskOracle.read_score).parameters
        assert list(parameters) == ["self", "answer"]
        assert parameters["answer"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["answer"].default is inspect.Parameter.empty

    def test_the_capability_base_gives_no_default(self) -> None:
        assert not hasattr(CapabilityBase, "read_score")
        assert not hasattr(CapabilityBase, "oracle_identity")


class TestTheAnswer:
    @pytest.mark.parametrize("answer", ["Mall", None])
    async def test_every_oracle_is_handed_the_runs_answer_unchanged(
        self, answer: str | None
    ) -> None:
        first, second = _Oracle(_judgment()), _Second(_judgment())
        await _env(a=first, plain=_Plain(), b=second).verify_live(timeout_s=1, answer=answer)
        assert first.given == second.given == [answer]

    async def test_the_answer_is_not_copied_into_the_verdict(self) -> None:
        """It is in ``result.json``; an oracle that judges it says so in its own evidence."""
        (verdict,) = await _env(a=_Oracle(_judgment())).verify_live(timeout_s=1, answer="Mall")
        assert "Mall" not in verdict.model_dump_json()


class TestNoOracle:
    async def test_no_oracle_is_an_empty_list(self) -> None:
        assert await _env(plain=_Plain()).verify_live(timeout_s=1, answer=None) == []
        assert await _env().verify_live(timeout_s=1, answer=None) == []

    async def test_an_oracle_that_judges_nothing_in_this_run_is_not_asked(self) -> None:
        silent = _silent(_Oracle(_judgment()))
        verdicts = await _env(a=silent, b=_Second(_judgment())).verify_live(
            timeout_s=1, answer=None
        )
        assert silent.given == []
        assert [(v.verifier.verifier_id, v.status, v.score) for v in verdicts] == [
            ("judge_oracle_route_second", "judged", 1.0)
        ]


class TestJudging:
    async def test_a_judgment_is_a_live_verdict_under_the_oracles_identity(self) -> None:
        before = time.time()
        (verdict,) = await _env(a=_Oracle(_judgment(0.5))).verify_live(timeout_s=1, answer=None)
        assert verdict.model_dump(exclude={"started_at", "duration_ms"}) == {
            "schema_version": "1.0",
            "verifier": {
                "verifier_id": "judge_oracle_route_cap",
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

    async def test_the_recorded_duration_covers_a_slow_oracles_time(self) -> None:
        """Measured around the call: an oracle cannot under-report how long it took."""

        class _Slow(_Oracle):
            __abstract__ = True  # Directly assembled fake; never a YAML implementation.

            async def read_score(self, *, answer: str | None) -> Judgment:
                await asyncio.sleep(0.2)
                return await super().read_score(answer=answer)

        (verdict,) = await _env(a=_Slow(_judgment())).verify_live(timeout_s=30, answer=None)
        assert verdict.status == "judged"
        assert verdict.duration_ms >= 190  # its 200 ms, give or take the timer's tick

    async def test_the_oracles_run_concurrently(self) -> None:
        """Each waits until the other has started: only possible when both run at once."""
        first, second = _Oracle(_judgment()), _Second(_judgment())
        first.wait_for, second.wait_for = second.entered, first.entered

        verdicts = await _env(a=first, b=second).verify_live(timeout_s=5, answer=None)

        assert [(v.verifier.verifier_id, v.status) for v in verdicts] == [
            ("judge_oracle_route_cap", "judged"),
            ("judge_oracle_route_second", "judged"),
        ]

    async def test_the_verdicts_come_back_in_capability_order(self) -> None:
        """Not in the order the oracles finished: the first one here finishes last."""
        later, sooner = _Oracle(_judgment()), _Second(_judgment())
        later.wait_for = sooner.replied

        verdicts = await _env(b=later, plain=_Plain(), a=sooner).verify_live(
            timeout_s=5, answer=None
        )

        assert [v.verifier.verifier_id for v in verdicts] == [
            "judge_oracle_route_cap",
            "judge_oracle_route_second",
        ]


class TestAFailedOracleJudgesNothing:
    async def test_one_past_its_limit_is_cancelled_and_not_judged_by_timeout(self) -> None:
        hung, healthy = _Oracle(_judgment()), _Second(_judgment())
        hung.wait_for = asyncio.Event()  # never set
        before = time.time()

        hung_verdict, healthy_verdict = await _env(a=hung, b=healthy).verify_live(
            timeout_s=0.05, answer=None
        )

        assert hung_verdict.model_dump(
            include={"verifier", "attachment", "status", "score", "label", "reason", "detail"}
        ) == {
            "verifier": {
                "verifier_id": "judge_oracle_route_cap",
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
        # Its limit cancelled it and waited for it; the other oracle still judged.
        assert hung.was_cancelled and hung.wound_down
        assert (healthy_verdict.verifier.verifier_id, healthy_verdict.status) == (
            "judge_oracle_route_second",
            "judged",
        )

    async def test_one_that_raises_is_not_judged_by_error_under_its_identity(self) -> None:
        broken = _Oracle(RuntimeError("adb went away"))
        (verdict,) = await _env(a=broken).verify_live(timeout_s=1, answer=None)
        assert (verdict.slug, verdict.status, verdict.reason, verdict.detail) == (
            broken.identity.slug,
            "not_judged",
            "error",
            "RuntimeError: adb went away",
        )
        assert verdict.score is None and verdict.label is None

    async def test_its_own_timeout_error_is_an_error_not_the_limit(self) -> None:
        """The limit is the environment's clock: an oracle's own ``TimeoutError`` is what it raised."""
        (verdict,) = await _env(a=_Oracle(TimeoutError("server took too long"))).verify_live(
            timeout_s=5, answer=None
        )
        assert (verdict.reason, verdict.detail) == ("error", "TimeoutError: server took too long")

    @pytest.mark.parametrize(
        ("reply", "returned"),
        [
            ({"score": 1.0}, "dict"),
            (None, "NoneType"),
            (
                Verdict(
                    verifier=VerifierIdentity(verifier_id="judge_oracle_route_cap"),
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
        (verdict,) = await _env(a=_Oracle(reply)).verify_live(timeout_s=1, answer=None)
        assert (verdict.status, verdict.reason, verdict.detail) == (
            "not_judged",
            "error",
            f"TypeError: read_score() returned {returned}, not a Judgment",
        )


class TestCancellation:
    async def test_cancelling_the_caller_cancels_and_awaits_every_unfinished_oracle(self) -> None:
        done, hung = _Oracle(_judgment()), _Second(_judgment())
        hung.wait_for = asyncio.Event()  # never set
        task = asyncio.ensure_future(_env(a=done, b=hung).verify_live(timeout_s=30, answer=None))
        await asyncio.wait_for(hung.entered.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Wound down before the cancellation reached the caller: nothing outlives the call.
        assert hung.was_cancelled and hung.wound_down
        assert not done.was_cancelled
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    async def test_an_oracle_that_is_cancelled_itself_leaves_no_sibling_running(self) -> None:
        """``gather`` would let the sibling of a cancelled child run on, unobserved."""
        cancelled, hung = _Oracle(asyncio.CancelledError()), _Second(_judgment())
        hung.wait_for = asyncio.Event()  # never set
        cancelled.wait_for = hung.entered

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                _env(a=cancelled, b=hung).verify_live(timeout_s=30, answer=None), timeout=5
            )

        assert hung.was_cancelled and hung.wound_down
