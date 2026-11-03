"""The scoring point's verdicts: kept in the run's verdict store before ``result.json``.

``docs/architecture_design.md`` §5.3 "判定统一存放": ``TraceWriter`` keeps what
the scoring point's ``verify_live`` returned (``after``, role ``env``, only
when there is an environment) and writes each verdict into the run's store,
``verdicts/<slug>.json``, outside the manifest, in ``on_run_end`` before
``result.json``; the recording plan's channels do not apply; a write failure
is a recording problem naming ``verdicts`` and never changes the outcome: a
verdict judged against the live environment cannot be had again.
``RunTrace.verdicts()`` reads them back with the offline ones.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace import RunTrace, WALWriter
from spectragent.core.trace import verdicts as verdict_store
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.verdict import Verdict, VerifierIdentity
from tests._helpers.trace_builder import CAPTURED_AT, agent_result


def _verdicts() -> list[Verdict]:
    """A judged live verdict and a not-judged one, the same on every call."""
    return [
        Verdict(
            verifier=VerifierIdentity(verifier_id="androidworld", version="abc123"),
            attachment="live",
            status="judged",
            score=0.5,
            evidence={"task": "ContactsAddContact", "params_sha256": "01"},
            started_at=CAPTURED_AT,
            duration_ms=812,
        ),
        Verdict(
            verifier=VerifierIdentity(verifier_id="second_oracle"),
            attachment="live",
            status="not_judged",
            started_at=CAPTURED_AT,
            duration_ms=60_000,
            reason="timeout",
            detail="no verdict within 60 s (verify_timeout)",
        ),
    ]


async def _closed_run(
    tmp_path: Path, verdicts: Any, *, recording: RecordingPlan | None = None
) -> TraceWriter:
    """One run through ``TraceWriter`` alone: started, judged live, closed out."""
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"), recording=recording)
    await writer.on_run_start(agent=None, env=None, task="t", config={})
    await writer.after_verify_live(verdicts, args=(), kwargs={"timeout_s": 60.0, "answer": None})
    await writer.on_run_end(result=agent_result())
    return writer


class TestTheTraceWriter:
    def test_the_interception_is_declared_only_with_an_environment(self) -> None:
        writer = TraceWriter()
        declared = {(i.role, i.handler_name) for i in TraceWriter.__intercepts__}
        assert ("env", "after_verify_live") in declared
        writer.intercept_environment(False)
        assert all(i.method != "verify_live" for i in writer.__intercepts__)
        writer.intercept_environment(True)
        assert writer.__intercepts__ == TraceWriter.__intercepts__

    async def test_the_verdicts_are_written_before_the_result_outside_the_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        real_result = WALWriter.write_result
        real_verdict = verdict_store.write_verdict

        def result_spy(self: WALWriter, *args: Any) -> None:
            order.append("write_result")
            real_result(self, *args)

        def verdict_spy(run_dir: Path, verdict: Verdict) -> Path:
            order.append(f"write_verdict {verdict.verifier.verifier_id}")
            return real_verdict(run_dir, verdict)

        monkeypatch.setattr(WALWriter, "write_result", result_spy)
        monkeypatch.setattr(
            "spectragent.core.hooks.trace_writer.write_verdict", verdict_spy, raising=True
        )

        writer = await _closed_run(tmp_path, _verdicts())

        assert order == [
            "write_verdict androidworld",
            "write_verdict second_oracle",
            "write_result",
        ]
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert list(trace.verdicts().values()) == sorted(_verdicts(), key=lambda v: v.slug)
        assert "verdict" not in (writer.run_dir / "manifest.json").read_text()
        assert trace.manifest.status.value == "complete"

    async def test_no_verdict_writes_no_store(self, tmp_path: Path) -> None:
        writer = await _closed_run(tmp_path, [])
        assert writer.run_dir is not None
        assert not (writer.run_dir / "verdicts").exists()
        trace = RunTrace(writer.run_dir)
        assert (trace.verdicts(), trace.manifest.status.value) == ({}, "complete")

    async def test_the_recording_plan_does_not_apply(self, tmp_path: Path) -> None:
        nothing = RecordingPlan(metrics=False, errors=False, trajectory=False, observations=False)
        writer = await _closed_run(tmp_path, _verdicts(), recording=nothing)
        assert writer.run_dir is not None
        assert len(RunTrace(writer.run_dir).verdicts()) == 2

    async def test_a_write_failure_is_a_recording_problem_and_the_outcome_stands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk that is full must not lose a verdict quietly: the trace says it is incomplete."""

        def full_disk(run_dir: Path, verdict: Verdict) -> Path:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("spectragent.core.hooks.trace_writer.write_verdict", full_disk)

        writer = await _closed_run(tmp_path, _verdicts()[:1])

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status.value == "incomplete"
        assert trace.manifest.incomplete_reason == (
            f"verdicts: OSError: [Errno {errno.ENOSPC}] No space left on device"
        )
        assert trace.verdicts() == {}
        result = trace.result
        assert result is not None and result.agent_done == AgentDoneStatus.DONE
        health = result.metrics.recording_health
        assert health is not None
        assert [(p.channel, p.phase) for p in health.problems] == [("verdicts", "closeout")]

    async def test_one_failed_write_does_not_lose_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = verdict_store.write_verdict

        def first_fails(run_dir: Path, verdict: Verdict) -> Path:
            if verdict.verifier.verifier_id == "androidworld":
                raise OSError(errno.EIO, "I/O error")
            return real(run_dir, verdict)

        monkeypatch.setattr("spectragent.core.hooks.trace_writer.write_verdict", first_fails)

        writer = await _closed_run(tmp_path, _verdicts())

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert [v.verifier.verifier_id for v in trace.verdicts().values()] == ["second_oracle"]
        assert (trace.manifest.incomplete_reason or "").startswith("verdicts: OSError")

    async def test_what_is_not_a_list_of_verdicts_is_a_recording_problem(
        self, tmp_path: Path
    ) -> None:
        """Taking the verdicts never raises into the scoring point."""
        writer = await _closed_run(tmp_path, [{"not": "a verdict"}])
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.verdicts() == {}
        assert (trace.manifest.incomplete_reason or "").startswith("verdicts: ")
