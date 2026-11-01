"""Scoring through the environment: every task oracle, concurrently, each within its limit.

``docs/architecture_design.md`` §4.3 "判分的路由" and §4.9 ``TaskOracle``:
``read_oracle_scores`` gives each capability implementing the protocol a task
of its own, runs them concurrently and limits each to ``timeout_s``. A ``None``
is left out; a timeout, an exception or a broken contract comes back as a
``not_scored`` entry; the entries come back in capability order, and the call
never raises ``Exception``. A cancellation cancels the unfinished oracles and
waits for them before it propagates. No oracle, no entry.
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
from spectragent.schemas.oracle_score import OracleScore
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
    """Answers with *answer*: a score, ``None``, an object to return, or an exception to raise.

    It waits for *wait_for* first, when given. A cancellation is noted, and the
    oracle takes a few loop turns to wind down before it lets it through — the
    cleanup ``read_oracle_scores`` must wait out.
    """

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "oracle_route_cap"

    def __init__(self, answer: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.answer = answer
        self.wait_for: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.answered = asyncio.Event()
        self.was_cancelled = False
        self.wound_down = False

    async def read_score(self) -> OracleScore | None:
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
        self.answered.set()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _Second(_Oracle):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "oracle_route_second"


def _scored(capability_id: str, score: float = 1.0) -> OracleScore:
    return OracleScore(
        capability_id=capability_id,
        status="scored",
        score=score,
        oracle="test_oracle",
        oracle_version="v1",
        provenance={"task": "t-1"},
        started_at=time.time(),
        duration_ms=1,
    )


def _env(**capabilities: CapabilityBase) -> EnvironmentBase:
    return make_env(EnvironmentBase, dict(capabilities))


class TestTheContract:
    def test_the_protocol_is_structural(self) -> None:
        assert isinstance(_Oracle(), TaskOracle)
        assert not isinstance(_Plain(), TaskOracle)

    def test_the_environment_protocol_declares_it_and_the_base_implements_it(self) -> None:
        assert hasattr(Environment, "read_oracle_scores")
        for owner in (Environment, EnvironmentBase):
            parameters = inspect.signature(owner.read_oracle_scores).parameters
            assert list(parameters) == ["self", "timeout_s"]
            assert parameters["timeout_s"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_capability_base_gives_no_default(self) -> None:
        assert not hasattr(CapabilityBase, "read_score")


class TestNoOracle:
    async def test_no_oracle_is_an_empty_list(self) -> None:
        assert await _env(plain=_Plain()).read_oracle_scores(timeout_s=1) == []
        assert await _env().read_oracle_scores(timeout_s=1) == []

    async def test_an_oracle_with_nothing_to_judge_is_left_out(self) -> None:
        nothing, scoring = _Oracle(None), _Second(_scored("oracle_route_second"))
        scores = await _env(a=nothing, b=scoring).read_oracle_scores(timeout_s=1)
        assert scores == [scoring.answer]


class TestScoring:
    async def test_a_score_comes_back_as_the_oracle_gave_it(self) -> None:
        score = _scored("oracle_route_cap", 0.5)
        assert await _env(a=_Oracle(score)).read_oracle_scores(timeout_s=1) == [score]

    async def test_the_oracles_run_concurrently(self) -> None:
        """Each waits until the other has started: only possible when both run at once."""
        first = _Oracle(_scored("oracle_route_cap"))
        second = _Second(_scored("oracle_route_second"))
        first.wait_for, second.wait_for = second.entered, first.entered

        scores = await _env(a=first, b=second).read_oracle_scores(timeout_s=5)

        assert [(s.capability_id, s.status) for s in scores] == [
            ("oracle_route_cap", "scored"),
            ("oracle_route_second", "scored"),
        ]

    async def test_the_entries_come_back_in_capability_order(self) -> None:
        """Not in the order the oracles finished: the first one here finishes last."""
        later = _Oracle(_scored("oracle_route_cap"))
        sooner = _Second(_scored("oracle_route_second"))
        later.wait_for = sooner.answered

        scores = await _env(b=later, plain=_Plain(), a=sooner).read_oracle_scores(timeout_s=5)

        assert [s.capability_id for s in scores] == ["oracle_route_cap", "oracle_route_second"]


class TestAFailedOracleIsNotScored:
    async def test_one_past_its_limit_is_cancelled_and_not_scored_by_timeout(self) -> None:
        hung = _Oracle(_scored("oracle_route_cap"))
        hung.wait_for = asyncio.Event()  # never set
        healthy = _Second(_scored("oracle_route_second"))
        before = time.time()

        hung_entry, healthy_entry = await _env(a=hung, b=healthy).read_oracle_scores(timeout_s=0.05)

        assert hung_entry.model_dump(exclude={"started_at", "duration_ms"}) == {
            "capability_id": "oracle_route_cap",
            "status": "not_scored",
            "score": None,
            "oracle": None,
            "oracle_version": None,
            "provenance": {},
            "reason": "timeout",
            "detail": "no score within 0.05 s (oracle_timeout)",
        }
        assert before <= hung_entry.started_at <= time.time()
        assert hung_entry.duration_ms >= 40  # the wait it was cut off from, give or take a tick
        # Its limit cancelled it and waited for it; the other oracle still scored.
        assert hung.was_cancelled and hung.wound_down
        assert healthy_entry == healthy.answer

    async def test_one_that_raises_is_not_scored_by_error(self) -> None:
        broken = _Oracle(RuntimeError("adb went away"))
        (entry,) = await _env(a=broken).read_oracle_scores(timeout_s=1)
        assert (entry.capability_id, entry.status, entry.reason, entry.detail) == (
            "oracle_route_cap",
            "not_scored",
            "error",
            "RuntimeError: adb went away",
        )
        assert entry.score is None and entry.oracle is None

    async def test_its_own_timeout_error_is_an_error_not_the_limit(self) -> None:
        """The limit is the environment's clock: an oracle's own ``TimeoutError`` is what it raised."""
        (entry,) = await _env(a=_Oracle(TimeoutError("server took too long"))).read_oracle_scores(
            timeout_s=5
        )
        assert (entry.reason, entry.detail) == ("error", "TimeoutError: server took too long")

    @pytest.mark.parametrize(
        ("answer", "detail"),
        [
            (
                {"status": "scored"},
                "TypeError: read_score() returned dict, not an OracleScore or None",
            ),
            (
                _scored("somebody_else"),
                "ValueError: read_score() returned a score under capability 'somebody_else'",
            ),
        ],
        ids=["not a score", "another capability's score"],
    )
    async def test_a_broken_contract_is_not_scored_by_error(self, answer: Any, detail: str) -> None:
        (entry,) = await _env(a=_Oracle(answer)).read_oracle_scores(timeout_s=1)
        assert (entry.capability_id, entry.reason, entry.detail) == (
            "oracle_route_cap",
            "error",
            detail,
        )


class TestCancellation:
    async def test_cancelling_the_caller_cancels_and_awaits_every_unfinished_oracle(self) -> None:
        done = _Oracle(_scored("oracle_route_cap"))
        hung = _Second(_scored("oracle_route_second"))
        hung.wait_for = asyncio.Event()  # never set
        task = asyncio.ensure_future(_env(a=done, b=hung).read_oracle_scores(timeout_s=30))
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
        cancelled = _Oracle(asyncio.CancelledError())
        hung = _Second(_scored("oracle_route_second"))
        hung.wait_for = asyncio.Event()  # never set
        cancelled.wait_for = hung.entered

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                _env(a=cancelled, b=hung).read_oracle_scores(timeout_s=30), timeout=5
            )

        assert hung.was_cancelled and hung.wound_down
