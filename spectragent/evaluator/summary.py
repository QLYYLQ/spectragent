"""Aggregate many ``EvalResult`` into one ``EvalSummary``.

``EvalSummary`` and its five sub-models have lived in ``schemas/`` with
zero producers in-package: the only code that ever built them was a
gitignored ``generate_evaluation_summary`` in a module that does not
exist. This is that function, typed, and reading ``EvalResult`` objects
instead of loosely-parsed JSON lines.

Metrics are sliced three ways — all, successful, failed — because "our
agent takes 12 steps on average" is uninformative next to "successful
runs take 8 and failures burn 31 before giving up".
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence

from spectragent.schemas.eval_result import EvalResult
from spectragent.schemas.eval_summary import (
    EvalSummary,
    FailureCategoryStats,
    JudgeTokenUsage,
    MetricStats,
    OverallStatistics,
)

#: Numeric agent metrics worth summarising. Each maps a result to a value
#: or ``None`` when that run did not record it — a missing metric must
#: shrink the sample, never contribute a zero.
_METRIC_KEYS = ("steps", "end_to_end_ms", "ttft_ms", "total_tokens")


def _metric_value(result: EvalResult, key: str) -> float | None:
    metrics = result.evaluation_details.agent_metrics
    if metrics is None:
        return None
    if key == "total_tokens":
        usage = metrics.usage
        return float(usage.total_tokens) if usage is not None else None
    value = getattr(metrics, key, None)
    return float(value) if value is not None else None


def _stats(values: Sequence[float]) -> MetricStats:
    if not values:
        return MetricStats()
    return MetricStats(
        count=len(values),
        mean=statistics.fmean(values),
        min=min(values),
        max=max(values),
        median=statistics.median(values),
    )


def _metric_block(results: Sequence[EvalResult]) -> dict[str, MetricStats]:
    block: dict[str, MetricStats] = {}
    for key in _METRIC_KEYS:
        values = [v for v in (_metric_value(r, key) for r in results) if v is not None]
        if values:
            block[key] = _stats(values)
    return block


def aggregate_judge_tokens(results: Iterable[EvalResult]) -> JudgeTokenUsage | None:
    """Sum the judge's own token spend across every verdict.

    ``None`` when no verdict carried usage at all — a zero-filled summary
    would claim the judge ran for free rather than admit it was not measured.
    """
    summary = JudgeTokenUsage()
    seen = False
    for result in results:
        usage = result.evaluation_details.eval_usage
        if usage is None:
            continue
        seen = True
        summary.total_prompt_tokens += usage.prompt_tokens
        summary.total_completion_tokens += usage.completion_tokens
        summary.total_cached_tokens += usage.cached_tokens
        summary.total_non_cached_prompt += usage.non_cached_prompt
    return summary if seen else None


def summarize(
    results: Sequence[EvalResult],
    *,
    total_tasks: int | None = None,
    evaluation_config: dict[str, object] | None = None,
) -> EvalSummary:
    """Roll a batch of verdicts into the reportable summary.

    *total_tasks* is the size of the benchmark slice that was *supposed*
    to run. It defaults to the number of results, but pass the real split
    size when you have it: an evaluator that scored 40 of 300 tasks at
    100% is not a 100% agent, and a denominator that quietly shrinks to
    match the numerator is how that gets reported.
    """
    total = total_tasks if total_tasks is not None else len(results)
    successful = [r for r in results if r.predicted_label == 1]
    failed = [r for r in results if r.predicted_label != 1]
    # ``evaluated_tasks`` means "a grader read this trace and judged it".
    # Counting the coverage-policy placeholders here would report three
    # tasks evaluated when the judge was only reached twice — and the
    # difference between the two numbers is the reader's only clue that
    # ``success_rate`` is a lower bound rather than a measurement.
    graded = [r for r in results if r.verdict_source == "graded"]

    overall = OverallStatistics(
        total_tasks=total,
        evaluated_tasks=len(graded),
        successful_tasks=len(successful),
        failed_tasks=len(failed),
        success_rate=(len(successful) / total * 100) if total else 0.0,
        failure_rate=(len(failed) / total * 100) if total else 0.0,
    )

    by_category: dict[str, dict[str, float]] = {}
    for result in failed:
        category = result.failure_category or "uncategorized"
        entry = by_category.setdefault(category, {"count": 0.0, "percentage": 0.0})
        entry["count"] += 1
    for entry in by_category.values():
        entry["percentage"] = (entry["count"] / len(failed) * 100) if failed else 0.0

    return EvalSummary(
        overall_statistics=overall,
        metrics_statistics=_metric_block(results),
        successful_tasks_metrics=_metric_block(successful),
        failed_tasks_metrics=_metric_block(failed),
        failure_category_statistics=FailureCategoryStats(
            total_failed_tasks=len(failed),
            by_category=by_category,
        ),
        task_list={
            "successful": [r.task_id for r in successful],
            "failed": [r.task_id for r in failed],
        },
        evaluation_config=dict(evaluation_config or {}),
        judge_token_usage=aggregate_judge_tokens(results),
    )


def format_summary(summary: EvalSummary) -> str:
    """One-screen rendering for the CLI."""
    overall = summary.overall_statistics
    lines = [
        f"tasks={overall.total_tasks} evaluated={overall.evaluated_tasks} "
        f"passed={overall.successful_tasks} failed={overall.failed_tasks}",
        f"success_rate={overall.success_rate:.2f}%",
    ]
    # The headline is the one thing a reader takes away, so the caveat
    # goes next to it rather than three screens down in the category
    # breakdown. A judge that timed out is not an agent that was wrong.
    unscored = max(0, overall.total_tasks - overall.evaluated_tasks)
    if unscored:
        broken = summary.failure_category_statistics.by_category.get("evaluation_error")
        infra = int(broken["count"]) if broken else 0
        lines.append(
            f"  WARNING: {unscored} task(s) carry no verdict "
            f"({infra} because scoring itself failed) — success_rate is a "
            f"lower bound, not a measurement. Re-run the scoring pass."
        )
    for name, stats in sorted(summary.metrics_statistics.items()):
        lines.append(
            f"  {name}: mean={stats.mean:.2f} median={stats.median:.2f} "
            f"min={stats.min:.2f} max={stats.max:.2f} (n={stats.count})"
        )
    categories = summary.failure_category_statistics.by_category
    if categories:
        lines.append("  failures by category:")
        for name, entry in sorted(categories.items()):
            lines.append(f"    {name}: {int(entry['count'])} ({entry['percentage']:.1f}%)")
    judge = summary.judge_token_usage
    if judge is not None:
        lines.append(
            f"  judge tokens: prompt={judge.total_prompt_tokens} "
            f"completion={judge.total_completion_tokens} "
            f"cached={judge.total_cached_tokens}"
        )
    return "\n".join(lines)
