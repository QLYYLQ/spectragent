"""EvaluationRunner + summary aggregation — scoring a directory, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.evaluator import (
    EvaluationRunner,
    ExactMatchEvaluator,
    discover_traces,
    summarize,
)
from spectragent.evaluator.summary import format_summary
from spectragent.schemas.eval_reference import EvalReference
from tests.test_evaluator.conftest import write_trace


class _Loader:
    """A minimal eval loader — the runner duck-types this surface."""

    benchmark_name = "offline_bench"
    subset = "All"

    def __init__(self, answers: dict[str, str]):
        self._refs = {
            task_id: EvalReference(
                task_id=task_id, benchmark="offline_bench", expected_answer=answer
            )
            for task_id, answer in answers.items()
        }

    def task_ids(self) -> list[str]:
        return list(self._refs)

    def get(self, task_id: str) -> EvalReference | None:
        return self._refs.get(task_id)

    def __getitem__(self, task_id: str) -> EvalReference:
        return self._refs[task_id]


@pytest.fixture
def three_traces(traces_dir: Path) -> list[Path]:
    return [
        write_trace(traces_dir, run_id="r1", task_id="t-1", answer="42"),
        write_trace(traces_dir, run_id="r2", task_id="t-2", answer="wrong"),
        write_trace(traces_dir, run_id="r3", task_id="t-3", answer="paris"),
    ]


@pytest.fixture
def loader() -> _Loader:
    return _Loader({"t-1": "42", "t-2": "43", "t-3": "Paris"})


# ── Discovery ───────────────────────────────────────────────────────


def test_discover_traces_finds_committed_runs(traces_dir, three_traces):
    assert discover_traces(traces_dir) == sorted(three_traces)


def test_discover_traces_skips_half_written_directories(traces_dir, three_traces):
    (traces_dir / "not-a-trace").mkdir()
    assert (traces_dir / "not-a-trace") not in discover_traces(traces_dir)


def test_discover_traces_on_a_missing_directory_is_empty(tmp_path):
    assert discover_traces(tmp_path / "nope") == []


# ── Scoring a sweep ─────────────────────────────────────────────────


async def test_every_trace_gets_a_verdict(traces_dir, three_traces, loader):
    runner = EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader)

    report = await runner.run(three_traces, benchmark="offline_bench", subset="All")

    assert len(report.results) == 3
    labels = {r.task_id: r.predicted_label for r in report.results}
    assert labels == {"t-1": 1, "t-2": 0, "t-3": 1}


async def test_verdicts_land_next_to_the_evidence(traces_dir, three_traces, loader):
    """The WAL has always made ``evals/`` and never used it."""
    evaluator = ExactMatchEvaluator()
    runner = EvaluationRunner(evaluator=evaluator, eval_loader=loader)

    await runner.run(three_traces)

    written = three_traces[0] / "evals" / f"{evaluator.verdict_slug}.json"
    assert written.is_file()
    payload = json.loads(written.read_text("utf-8"))
    assert payload["task_id"] == "t-1"
    assert payload["predicted_label"] == 1
    assert payload["evaluator_id"] == "exact_match"


def test_the_verdict_filename_carries_the_configuration(three_traces):
    """``evaluator_id`` alone is not a filename — it collides."""
    strict = ExactMatchEvaluator(case_sensitive=True)
    lenient = ExactMatchEvaluator(allow_substring=True)

    assert strict.verdict_slug.startswith("exact_match_")
    assert strict.verdict_slug != lenient.verdict_slug
    # Stable across instances, or re-scoring would never overwrite even
    # its own previous verdict.
    assert strict.verdict_slug == ExactMatchEvaluator(case_sensitive=True).verdict_slug


async def test_flat_evals_dir_collects_results_and_summary(tmp_path, three_traces, loader):
    evals_dir = tmp_path / "evals"
    evaluator = ExactMatchEvaluator()
    slug = evaluator.verdict_slug
    runner = EvaluationRunner(evaluator=evaluator, eval_loader=loader, evals_dir=evals_dir)

    await runner.run(three_traces, benchmark="offline_bench")

    names = sorted(p.name for p in evals_dir.iterdir())
    assert names == [
        f"{slug}_summary.json",
        f"r1__{slug}.json",
        f"r2__{slug}.json",
        f"r3__{slug}.json",
    ]
    summary = json.loads((evals_dir / f"{slug}_summary.json").read_text("utf-8"))
    assert summary["overall_statistics"]["successful_tasks"] == 2


async def test_read_only_mode_writes_nothing(three_traces, loader):
    runner = EvaluationRunner(
        evaluator=ExactMatchEvaluator(), eval_loader=loader, write_into_trace=False
    )

    await runner.run(three_traces)

    # The WAL creates ``evals/`` itself, so "wrote nothing" means the
    # directory is empty — not that it is absent.
    assert list((three_traces[0] / "evals").glob("*.json")) == []


async def test_one_bad_trace_does_not_destroy_the_sweep(traces_dir, three_traces, loader):
    """A sweep that dies on trace 2 of 300 has thrown away 298 verdicts."""
    orphan = write_trace(traces_dir, run_id="r4", task_id="not-in-the-split")

    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        [*three_traces, orphan]
    )

    assert len(report.graded) == 3
    assert len(report.errors) == 1
    failure = report.errors[0].error
    assert failure is not None
    assert "not-in-the-split" in failure
    # The unscorable trace gets a record too, so it cannot sit in the
    # denominator as a task with no row at all.
    assert len(report.results) == 4
    assert report.summary().overall_statistics.total_tasks == 4


# ── Coverage policy ─────────────────────────────────────────────────


class _FlakyEvaluator:
    """A grader whose judge is unreachable for some tasks.

    Duck-typed on the Protocol, like a real third-party evaluator, and it
    lets the transport error escape — which is what the ported graders
    deliberately stopped swallowing.
    """

    evaluator_id = "flaky"

    def __init__(self, broken: set[str]):
        self._broken = broken
        self._inner = ExactMatchEvaluator()

    async def evaluate(self, trace, reference):
        if reference.task_id in self._broken:
            raise ConnectionError("429 Too Many Requests")
        return await self._inner.evaluate(trace, reference)


@pytest.fixture
def all_correct(traces_dir: Path) -> list[Path]:
    """Three traces whose answers are all right."""
    return [
        write_trace(traces_dir, run_id=f"r{i}", task_id=f"t-{i}", answer="42") for i in (1, 2, 3)
    ]


async def test_a_judge_outage_is_not_scored_as_a_wrong_answer(all_correct):
    """An API outage must not become a benchmark score.

    Every answer here is correct and the judge is unreachable for exactly
    one task. Before this was fixed the sweep reported a 100%-correct
    agent at 66.67% with ``evaluated=3``, because the rate-limited task
    was manufactured into an ordinary ``predicted_label=0`` verdict —
    reintroducing, one layer up, the ``return "correct: no"`` on retry
    exhaustion that the ported BrowseComp grader exists to avoid.
    """
    loader = _Loader({"t-1": "42", "t-2": "42", "t-3": "42"})

    report = await EvaluationRunner(
        evaluator=_FlakyEvaluator(broken={"t-2"}), eval_loader=loader
    ).run(
        all_correct,
        expected_task_ids=["t-1", "t-2", "t-3"],
        benchmark="offline_bench",
    )

    verdicts = {r.task_id: r for r in report.results}
    assert verdicts["t-1"].verdict_source == "graded"
    assert verdicts["t-3"].verdict_source == "graded"

    outage = verdicts["t-2"]
    assert outage.verdict_source == "evaluation_error"
    assert outage.failure_category == "evaluation_error"
    assert "429" in outage.evaluation_details.benchmark_details["evaluation_error"]

    overall = report.summary().overall_statistics
    # The coverage policy still holds: an unscored task keeps its place in
    # the denominator rather than inflating the score by vanishing.
    assert overall.total_tasks == 3
    # But it was never judged, and the summary no longer claims it was.
    assert overall.evaluated_tasks == 2
    assert len(report.graded) == 2


async def test_a_wrong_answer_and_an_unreachable_judge_are_different_records(
    traces_dir,
):
    """Same label, different meaning — and the record must say which.

    Both land on ``predicted_label=0``, so the label alone cannot carry
    the distinction. ``verdict_source`` is what separates "the grader read
    this and it was wrong" from "no grader ever saw it".
    """
    traces = [
        write_trace(traces_dir, run_id="r1", task_id="t-1", answer="nonsense"),
        write_trace(traces_dir, run_id="r2", task_id="t-2", answer="42"),
    ]
    loader = _Loader({"t-1": "42", "t-2": "42"})

    report = await EvaluationRunner(
        evaluator=_FlakyEvaluator(broken={"t-2"}), eval_loader=loader
    ).run(traces, expected_task_ids=["t-1", "t-2"])

    verdicts = {r.task_id: r for r in report.results}
    wrong, outage = verdicts["t-1"], verdicts["t-2"]

    assert wrong.predicted_label == outage.predicted_label == 0
    assert (wrong.verdict_source, wrong.failure_category) == (
        "graded",
        "wrong_answer",
    )
    assert (outage.verdict_source, outage.failure_category) == (
        "evaluation_error",
        "evaluation_error",
    )


async def test_the_summary_headline_carries_its_own_caveat(all_correct):
    """``success_rate`` must not read as a measurement when it is not."""
    loader = _Loader({"t-1": "42", "t-2": "42", "t-3": "42"})

    report = await EvaluationRunner(
        evaluator=_FlakyEvaluator(broken={"t-2"}), eval_loader=loader
    ).run(all_correct, expected_task_ids=["t-1", "t-2", "t-3"])

    printed = format_summary(report.summary())

    assert "tasks=3 evaluated=2" in printed
    assert "WARNING" in printed
    assert "1 task(s) carry no verdict" in printed
    assert "1 because scoring itself failed" in printed
    assert "lower bound" in printed


async def test_a_clean_sweep_prints_no_warning(three_traces, loader):
    """The caveat must not cry wolf when every task really was judged."""
    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        three_traces, expected_task_ids=["t-1", "t-2", "t-3"]
    )

    printed = format_summary(report.summary())

    assert "tasks=3 evaluated=3" in printed
    assert "WARNING" not in printed


async def test_a_task_that_produced_no_trace_counts_as_failed(three_traces, loader):
    """Dropping crashed tasks from the denominator inflates the score."""
    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        three_traces[:1],
        expected_task_ids=["t-1", "t-2", "t-3"],
        benchmark="offline_bench",
    )

    assert len(report.results) == 3
    synthetic = [r for r in report.results if r.failure_category == "not_evaluated"]
    assert {r.task_id for r in synthetic} == {"t-2", "t-3"}
    assert all(r.predicted_label == 0 for r in synthetic)
    # No trace arrived — that is the agent's failure, not the judge's, so
    # it stays "not_evaluated" and never becomes "evaluation_error".
    assert all(r.verdict_source == "not_evaluated" for r in synthetic)
    assert all(
        r.evaluation_details.benchmark_details["is_synthetic_failure"] is True for r in synthetic
    )
    assert report.summary().overall_statistics.total_tasks == 3
    assert report.summary().overall_statistics.success_rate == pytest.approx(100 / 3)


# ── Summary aggregation ─────────────────────────────────────────────


async def test_summary_partitions_metrics_by_outcome(traces_dir, loader):
    """'Successes take 8 steps, failures burn 31' is the useful statement."""
    traces = [
        write_trace(
            traces_dir, run_id="p", task_id="t-1", answer="42", steps=2, end_to_end_ms=1000
        ),
        write_trace(
            traces_dir, run_id="f", task_id="t-2", answer="nope", steps=6, end_to_end_ms=9000
        ),
    ]

    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(traces)
    summary = report.summary()

    assert summary.successful_tasks_metrics["steps"].mean == 2
    assert summary.failed_tasks_metrics["steps"].mean == 6
    assert summary.metrics_statistics["steps"].mean == 4
    assert summary.metrics_statistics["end_to_end_ms"].max == 9000


async def test_summary_counts_failure_categories(traces_dir, loader):
    traces = [
        write_trace(traces_dir, run_id="a", task_id="t-1", answer="42"),
        write_trace(traces_dir, run_id="b", task_id="t-2", answer="nope"),
    ]

    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(traces)
    stats = report.summary().failure_category_statistics

    assert stats.total_failed_tasks == 1
    assert stats.by_category["wrong_answer"]["count"] == 1
    assert stats.by_category["wrong_answer"]["percentage"] == 100.0


def test_summary_of_nothing_does_not_divide_by_zero():
    summary = summarize([])
    assert summary.overall_statistics.total_tasks == 0
    assert summary.overall_statistics.success_rate == 0.0
    assert summary.evaluation_cost is None
    assert "tasks=0" in format_summary(summary)


async def test_task_list_names_who_passed_and_who_failed(traces_dir, three_traces, loader):
    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        three_traces
    )

    task_list = report.summary().task_list
    assert task_list["successful"] == ["t-1", "t-3"]
    assert task_list["failed"] == ["t-2"]


async def test_summary_records_which_evaluator_produced_it(three_traces, loader):
    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        three_traces, benchmark="offline_bench", subset="All"
    )

    config = report.summary().evaluation_config
    assert config["evaluator_id"] == "exact_match"
    assert config["benchmark"] == "offline_bench"
    assert config["subset"] == "All"


async def test_judge_costs_aggregate_when_present(traces_dir, loader):
    """exact_match spends nothing, so there is nothing to report."""
    trace = write_trace(traces_dir, run_id="c", task_id="t-1", answer="42")

    report = await EvaluationRunner(evaluator=ExactMatchEvaluator(), eval_loader=loader).run(
        [trace]
    )

    assert report.summary().evaluation_cost is None


async def test_the_same_traces_can_be_scored_twice_by_different_evaluators(
    traces_dir, three_traces, loader
):
    """Record once, evaluate many — the whole point of the WAL."""
    strict = ExactMatchEvaluator(case_sensitive=True)
    lenient = ExactMatchEvaluator(allow_substring=True)

    strict_report = await EvaluationRunner(evaluator=strict, eval_loader=loader).run(three_traces)
    lenient_report = await EvaluationRunner(evaluator=lenient, eval_loader=loader).run(three_traces)

    # "paris" vs "Paris" passes leniently and fails under case sensitivity.
    strict_labels = {r.task_id: r.predicted_label for r in strict_report.results}
    lenient_labels = {r.task_id: r.predicted_label for r in lenient_report.results}
    assert strict_labels["t-3"] == 0
    assert lenient_labels["t-3"] == 1

    # Both verdicts persisted side by side under the same trace. Asserting
    # only that *a* file exists — which this test used to do — passes just
    # as happily when the second run silently overwrites the first, which
    # is exactly the bug it claimed to disprove. So: two files, and the
    # opposing labels still readable in them.
    written = sorted((three_traces[2] / "evals").glob("*.json"))
    assert len(written) == 2
    labels = {json.loads(p.read_text("utf-8"))["predicted_label"] for p in written}
    assert labels == {0, 1}
