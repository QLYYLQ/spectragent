"""``RunEnded``: how a Loop cut off mid-recording hands the runtime its outcome.

``core/loops/errors.py``. A Loop records the step its run ended on before
the outcome leaves it; a cancellation landing in that hand-over is never
the Loop's to swallow, so the outcome rides on it as context
(``carry_outcome``) and ``AgentRuntime.execute`` reads it back — only for
the run's own timeout (``carried_outcome``;
``test_run_lifecycle.py::TestAnOutcomeOutranksTheRunsOwnTimeout``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spectragent.core.loops.errors import RunAbort, RunEnded, carried_outcome, carry_outcome
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.agent_result import AgentResult
from tests._helpers.trace_builder import agent_result


def _veto() -> RunAbort:
    return RunAbort("risk-control page detected", source="risk_guard")


def _failed() -> AgentResult:
    return agent_result(
        env_status=EnvironmentStatus.FAILED,
        agent_done=AgentDoneStatus.ERROR,
        agent_reported_success=None,
        error="observe failed at step 1: ConnectionError: CDP websocket closed",
    )


class TestCarryOutcome:
    @pytest.mark.parametrize("outcome", [_veto(), _failed()], ids=["veto", "result"])
    def test_the_cancellation_carries_the_outcome_and_stays_itself(self, outcome: Any) -> None:
        cancelled = asyncio.CancelledError()
        carry_outcome(cancelled, outcome)

        assert carried_outcome(cancelled) is outcome
        assert type(cancelled) is asyncio.CancelledError
        assert isinstance(cancelled.__context__, RunEnded)
        assert cancelled.__context__.outcome is outcome

    def test_the_context_the_cancellation_had_moves_behind_the_carrier(self) -> None:
        cancelled = asyncio.CancelledError()
        earlier = KeyError("being handled when the cancellation arrived")
        cancelled.__context__ = earlier
        carry_outcome(cancelled, _veto())

        assert isinstance(cancelled.__context__, RunEnded)
        assert cancelled.__context__.__context__ is earlier

    def test_a_veto_already_in_flight_is_not_carried_twice(self) -> None:
        veto = _veto()
        cancelled = asyncio.CancelledError()
        cancelled.__context__ = veto  # landed while an ``except RunAbort`` block was awaiting
        carry_outcome(cancelled, veto)

        assert cancelled.__context__ is veto

    def test_the_outcome_carried_last_is_the_one_read_back(self) -> None:
        cancelled = asyncio.CancelledError()
        carry_outcome(cancelled, _veto())
        result = _failed()
        carry_outcome(cancelled, result)

        assert carried_outcome(cancelled) is result

    def test_the_carrier_names_what_the_run_had_ended_in(self) -> None:
        assert str(RunEnded(_veto())) == (
            "the run had already ended: RunAbort: risk_guard: risk-control page detected"
        )
        assert str(RunEnded(_failed())) == (
            "the run had already ended: "
            "observe failed at step 1: ConnectionError: CDP websocket closed"
        )


class TestCarriedOutcome:
    def test_a_cancellation_that_carries_nothing(self) -> None:
        cancelled = asyncio.CancelledError()
        cancelled.__context__ = KeyError("unrelated")
        assert carried_outcome(asyncio.CancelledError()) is None
        assert carried_outcome(cancelled) is None

    async def test_a_veto_the_cancellation_landed_in_the_handling_of_counts(self) -> None:
        """No Loop carried it: awaiting inside ``except RunAbort`` is enough."""
        veto = _veto()
        handling = asyncio.Event()

        async def inline_abort_shape() -> None:
            try:
                raise veto
            except RunAbort:
                handling.set()
                await asyncio.sleep(100)  # recording the vetoed step
                raise

        task = asyncio.create_task(inline_abort_shape())
        await handling.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        assert carried_outcome(caught.value) is veto

    def test_a_veto_named_only_as_a_cause_is_not_in_flight(self) -> None:
        """An error raised *from* a veto is a different failure, not the veto."""
        failure = ConnectionError("observe failed")
        failure.__cause__ = _veto()
        cancelled = asyncio.CancelledError()
        cancelled.__context__ = failure

        assert carried_outcome(cancelled) is None

    def test_a_context_cycle_ends_the_walk(self) -> None:
        first, second = KeyError("a"), ValueError("b")
        first.__context__, second.__context__ = second, first
        cancelled = asyncio.CancelledError()
        cancelled.__context__ = first

        assert carried_outcome(cancelled) is None


class TestTheRunsOwnTimeoutKeepsIt:
    """What ``AgentRuntime.execute`` relies on: ``wait_for`` keeps the carrying cancellation.

    ``asyncio.wait_for`` raises its ``TimeoutError`` from the cancellation it
    delivered into the awaited coroutine — the very object the Loop made
    carry its outcome. If a Python release stops doing that, this fails here,
    naming the assumption, rather than as a lost veto further up.
    """

    async def test_wait_for_raises_its_timeout_from_the_carrying_cancellation(self) -> None:
        veto = _veto()

        async def recording_the_vetoed_step() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError as cancelled:
                carry_outcome(cancelled, veto)
                raise

        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(recording_the_vetoed_step(), timeout=0.01)

        cancelled = caught.value.__cause__
        assert isinstance(cancelled, asyncio.CancelledError)
        assert carried_outcome(cancelled) is veto
