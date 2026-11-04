"""The task graph: each run's committed steps, and how one task's runs descend from each other.

Two reads, both without images, model calls or archives. :func:`run_graph`
summarises one run's committed steps. :func:`task_graph` places one task's
runs: which commit a branch leaves, which runs are drawn without an edge and
why, and the fork points a crowded task folds. It reads the run list alone
(``RunRepository.list_runs`` rows), so a task of fifty branches is placed
without opening fifty traces; the page then reads the commits of the lines it
shows. Lineage comes only from recorded provenance (``TraceConfig.branch`` and
the source the list resolved for it), never from names or step numbers.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import task_identity
from spectragent.inspection.marks import action_text, addressed_elements
from spectragent.schemas.step_record import StepRecord

#: A task with more runs than this folds each fork point into one summary row.
EXPANDED_RUNS = 8

#: Why a branch is drawn without an edge to its source, by how the list resolved the source.
_SOURCE_ISSUES = {
    "ambiguous": "源运行 ID 对应多份记录，无法确定从哪条分出",
    "missing": "根目录内找不到源运行",
}


def run_graph(trace: RunTrace) -> dict[str, Any]:
    """Present real step indices and origins; lineage comes from the run's provenance.

    Reading a graph must not decode prompt images, load archives, or open another
    run. In particular, a restoration prefix is never represented as new decisions.
    An action naming an element is worded with that element as its step's
    observation recorded it (``marks.addressed_elements``). ``calls`` is the
    step's model calls, ``None`` when the run recorded no call metrics.
    """
    metrics = trace.config.recording.metrics
    calls = {entry.step: len(entry.call_metrics) for entry in trace.manifest.steps}
    steps = []
    for record in trace.step_records:
        actions = []
        elements = addressed_elements(trace, record)
        for index, action in enumerate(record.actions):
            result = record.action_results[index] if index < len(record.action_results) else None
            skipped = (
                record.skipped_actions > 0 and index >= len(record.actions) - record.skipped_actions
            )
            actions.append(
                {
                    "index": index,
                    "name": action.name,
                    "text": action_text(action, result, elements[index]),
                    "status": "skipped"
                    if skipped
                    else "succeeded"
                    if result is not None and result.success
                    else "failed"
                    if result is not None
                    else "no_result",
                }
            )
        check = record.restoration
        steps.append(
            {
                "step": record.step,
                "origin": record.origin,
                "actions": actions,
                "error": record.error,
                "error_phase": record.error_phase,
                "calls": calls.get(record.step) if metrics else None,
                "restoration_verdict": check.verdict if check else None,
                "restoration_reasons": list(check.reasons) if check else [],
            }
        )
    return {
        "steps": steps,
        "recording_trajectory": trace.config.recording.trajectory,
        "restored_steps": sum(step["origin"] == "restored" for step in steps),
    }


# ── one task ──────────────────────────────────────────────────────────


def task_key(config: TraceConfig) -> str | None:
    """The recorded task identity a run belongs to, as one key; ``None`` when incomplete.

    Benchmark, subset and task id (a branch's are its source's,
    ``core/trace/sources.py::task_identity``). Without a benchmark or a task
    id the run's task is not known from its own record.
    """
    identity = task_identity(config)
    if not identity.benchmark or not identity.task_id:
        return None
    return json.dumps([identity.benchmark, identity.subset, identity.task_id], ensure_ascii=False)


def task_keys(rows: Iterable[dict[str, Any]]) -> None:
    """Give every row its ``task_key``: its own, else its resolved source's, else its own run's.

    A run whose record lacks a benchmark or a task id joins its source's task
    when it is a branch whose source the list resolved; any other is a task of
    its own, ``run:<key>``. ``task_incomplete`` says the task is not known from
    a complete recorded identity. Rows are updated in place.
    """
    rows = list(rows)
    by_key = {row["key"]: row for row in rows}

    def resolve(row: dict[str, Any], seen: frozenset[str]) -> tuple[str, bool]:
        if row.get("task_key"):
            return row["task_key"], False
        source = by_key.get(row.get("source_key") or "")
        if (
            row.get("branch")
            and row.get("source_resolution") == "found"
            and source is not None
            and source["key"] not in seen
        ):
            return resolve(source, seen | {row["key"]})
        return f"run:{row['key']}", True

    resolved = [resolve(row, frozenset()) for row in rows]
    for row, (key, incomplete) in zip(rows, resolved, strict=True):
        row["task_key"], row["task_incomplete"] = key, incomplete


def restoration_tally(records: list[StepRecord], checkpoint: int) -> dict[str, Any]:
    """A branch's restoration at a glance: its restored prefix by verdict, and its checkpoint check.

    ``checkpoint`` is ``None`` when the branch committed no check at its
    checkpoint step (restoration stopped before it).
    """
    prefix = dict.fromkeys(("ok", "warn", "diverged"), 0)
    check = None
    for record in records:
        if record.origin == "restored" and record.restoration is not None:
            prefix[record.restoration.verdict] += 1
        elif record.step == checkpoint and record.restoration is not None:
            check = {
                "verdict": record.restoration.verdict,
                "reasons": list(record.restoration.reasons),
            }
    return {
        "restored": sum(record.origin == "restored" for record in records),
        "prefix": prefix,
        "checkpoint": check,
    }


def _checkpoint(row: Mapping[str, Any]) -> int:
    return int((row.get("branch") or {}).get("checkpoint_step", 0))


def _parent(
    row: Mapping[str, Any], runs: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, str | int] | None, str | None]:
    """The commit *row*'s branch leaves, ``{"key", "step"}``, or why it is drawn without one.

    A branch with checkpoint ``k`` leaves its recorded source's step ``k - 1``
    (``-1``: the source's start). The branch loader branches only a run that
    is no branch itself, at a step its record is complete through, so that
    commit exists whenever the source is one of the task's readable runs.
    """
    resolution = row.get("source_resolution") or "missing"
    source = runs.get(row.get("source_key") or "") if resolution == "found" else None
    if source is None:
        return None, _SOURCE_ISSUES.get(resolution, _SOURCE_ISSUES["missing"])
    return {"key": source["key"], "step": _checkpoint(row) - 1}, None


def _outcome(verdict: Mapping[str, Any]) -> str:
    """One verdict as a tally word: its label, else its score, else that it did not judge."""
    if verdict.get("status") != "judged":
        return "没判"
    if verdict.get("label") in (0, 1):
        return "成功" if verdict["label"] == 1 else "失败"
    score = verdict.get("score")
    return f"分数 {score:g}" if isinstance(score, int | float) else "判了但没有分数"


def verdict_tally(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each verifier's verdicts over *rows*, counted by outcome; no verdict is merged across slugs."""
    tally: dict[tuple[str, str], int] = {}
    for row in rows:
        for verdict in row.get("verdicts") or []:
            key = (verdict["slug"], _outcome(verdict))
            tally[key] = tally.get(key, 0) + 1
    return [
        {"slug": slug, "outcome": outcome, "count": count}
        for (slug, outcome), count in sorted(tally.items())
    ]


def _fork_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What a folded fork point says: its branches' restoration, end reasons and verdicts."""
    restoration: dict[str, int] = {}
    ended: dict[str, int] = {}
    for row in rows:
        tally = row.get("restoration") or {}
        verdicts = [v for v, n in (tally.get("prefix") or {}).items() if n]
        if tally.get("checkpoint"):
            verdicts.append(tally["checkpoint"]["verdict"])
        worst = next((v for v in ("diverged", "warn", "ok") if v in verdicts), "unchecked")
        restoration[worst] = restoration.get(worst, 0) + 1
        done = str(row.get("agent_done") or "no_result")
        ended[done] = ended.get(done, 0) + 1
    return {"restoration": restoration, "ended": ended, "verdicts": verdict_tally(rows)}


def _differences(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    """Which label fields differ among a task's own runs, and the ``#n`` of runs that differ in none."""
    fields = ("agent_id", "model", "config_hash")
    differs = [field for field in fields if len({row.get(field) for row in rows}) > 1]
    ordinals: dict[str, int] = {}
    same: dict[tuple[Any, ...], list[str]] = {}
    for row in rows:
        same.setdefault(tuple(row.get(field) for field in fields), []).append(row["key"])
    for keys in same.values():
        if len(keys) > 1:
            ordinals.update({key: index + 1 for index, key in enumerate(keys)})
    return differs, ordinals


def _started(row: Mapping[str, Any]) -> tuple[str, str]:
    return row.get("created_at") or "", row["key"]


def task_graph(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """One task's runs placed as a graph, from the run list alone.

    *rows* are run list rows with ``task_key``: every run under the root, plus
    the sources of branches the list's filters left out, marked ``context``.
    Returns the task, its runs, its lines and its fork points. The lines are
    every run of the task in the order a top-down graph draws them: top-level
    runs by start time, each followed by its branches by the step they leave;
    a line drawn without an edge (``detached`` says why) comes last. A line's
    ``parent`` is the commit it leaves, ``{"key", "step"}``, step ``-1`` being
    the parent's start. ``collapse`` says the task has more than
    :data:`EXPANDED_RUNS` runs, so each fork point is folded.

    Raises:
        LookupError: No readable run belongs to the task *key*.
    """
    members = sorted(
        (row for row in rows if row.get("task_key") == key and not row.get("read_error")),
        key=_started,
    )
    if not members:
        raise LookupError(f"No readable run of the task {key} in this directory")
    runs = {row["key"]: row for row in members}
    placed: dict[str, dict[str, Any]] = {}
    for row in members:
        parent, detached = _parent(row, runs) if row.get("branch") else (None, None)
        placed[row["key"]] = {
            "key": row["key"],
            "parent": parent,
            "detached": detached,
            "context": bool(row.get("context")),
        }
    children: dict[tuple[str, int], list[str]] = {}
    for line in placed.values():
        if line["parent"] is not None:
            at = (line["parent"]["key"], line["parent"]["step"])
            children.setdefault(at, []).append(line["key"])
    differs, ordinals = _differences([row for row in members if not row.get("branch")])
    ordered: list[dict[str, Any]] = []

    def visit(line_key: str, depth: int) -> None:
        ordered.append({**placed[line_key], "depth": depth, "ordinal": ordinals.get(line_key)})
        for (parent, _), keys in sorted(children.items(), key=lambda item: item[0][1]):
            if parent == line_key:
                for child in keys:
                    visit(child, depth + 1)

    for line in sorted(placed.values(), key=lambda line: line["detached"] is not None):
        if line["parent"] is None:
            visit(line["key"], 0)
    forks = [
        {
            "parent": parent,
            "step": step,
            "branches": keys,
            "summary": _fork_summary([runs[k] for k in keys]),
        }
        for (parent, step), keys in sorted(children.items())
    ]
    first = members[0]
    branch = first.get("branch") or {}
    variant = branch.get("task_variant") or {}
    return {
        "task": {
            "key": key,
            "benchmark": branch.get("source_benchmark") if branch else first.get("benchmark"),
            "subset": branch.get("source_subset") if branch else first.get("subset"),
            "task_id": branch.get("source_task_id") if branch else first.get("task_id"),
            "title": variant.get("source_task_text") or first.get("task") or "",
            "incomplete": bool(first.get("task_incomplete")),
        },
        "runs": runs,
        "lines": ordered,
        "forks": forks,
        "differs": differs,
        "collapse": len(members) > EXPANDED_RUNS,
    }
