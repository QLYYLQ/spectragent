"""Pure terminal presentation shared by live and recorded experiment views.

Everything here turns recorded facts into *parts*: short strings tagged with
a tone. A tone names what a fact means (``ok``, ``warn``, ``bad``, ``muted``
…), never a colour; ``spectragent.tui.style`` maps tones onto the active
theme. Every tone is paired with a word or a glyph, so a terminal without
colour (``NO_COLOR``, a monochrome theme) still reads the same.

The rules the views keep are the design's, not this module's: a tolerance
verdict and a measured difference are shown together, an unmeasured fact is
never drawn as zero, source material is never presented as this run's
decision, and the configured ``max_steps`` is a ceiling, not a fork's budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from spectragent.inspection.restoration import restoration_gap

Tone = Literal["plain", "strong", "muted", "ok", "warn", "bad", "info", "accent", "badge"]
Part = tuple[str, Tone]
UNKNOWN: list[Part] = [("unknown", "muted")]
NONE: list[Part] = [("—", "muted")]

# One glyph per meaning. All are single-width text symbols (none has an emoji
# presentation), and each always appears next to its word.
GLYPH = {
    "policy": "●",
    "restored": "↺",
    "terminal": "■",
    "checkpoint": "◆",
    "ok": "✓",
    "warn": "!",
    "bad": "✗",
    "unknown": "?",
    "differs": "≠",
    "queued": "○",
    "running": "●",
}


@dataclass(frozen=True)
class Fact:
    """One labelled line of a fact sheet; ``label=""`` continues the previous one."""

    label: str
    parts: tuple[Part, ...]


@dataclass(frozen=True)
class Heading:
    """A section title inside a fact sheet."""

    title: str


@dataclass(frozen=True)
class Banner:
    """A full-width line above the labelled facts; a ``badge`` part is drawn reversed."""

    parts: tuple[Part, ...]
    badge: Tone = "plain"


Sheet = list[Fact | Heading | Banner]


def fact(label: str, *parts: Part | str) -> Fact:
    return Fact(label, tuple((part, "plain") if isinstance(part, str) else part for part in parts))


def plain(parts: Iterable[Part]) -> str:
    return "".join(text for text, _ in parts)


def sheet_text(sheet: Sheet) -> str:
    """The sheet as plain text, for tests and for copying."""
    lines = []
    for item in sheet:
        if isinstance(item, Banner):
            lines.append(plain(item.parts))
        elif isinstance(item, Heading):
            lines.append(item.title)
        else:
            lines.append(f"{item.label:<12}{plain(item.parts)}".rstrip())
    return "\n".join(lines)


def terminal_value(value: Any) -> Any:
    """Keep image payloads out of terminal text while preserving all other details."""
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"[inline image, {len(value):,} encoded characters; view in dashboard]"
    if isinstance(value, dict):
        return {key: terminal_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [terminal_value(item) for item in value]
    return value


def step_presentation(record: dict[str, Any]) -> dict[str, Any]:
    """Label returned action pairs and all restoration attempts separately.

    Source replies and actions stay in ``source_history``. The step's top-level
    action/result pairs describe this run, including a returned failure.
    """
    if record.get("origin") != "restored":
        return record
    check = record.get("restoration") or {}
    return {
        "agent_history_vs_replay": restoration_gap(record),
        **{
            key: value
            for key, value in record.items()
            if key not in {"actions", "action_results", "restoration"}
        },
        "actual_actions": record.get("actions", []),
        "actual_action_results": record.get("action_results", []),
        "actual_restoration_actions": check.get("actions", []),
        "restoration_check": {key: value for key, value in check.items() if key != "actions"},
    }


# --- Step rows -------------------------------------------------------------


def _attempt_result(action: dict[str, Any]) -> Part:
    success = action.get("live_success")
    if action.get("live_error") or success is False:
        return ("failed", "bad")
    if success is True:
        return ("ok", "ok")
    return ("unknown result", "muted")


def skipped_actions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The step's last actions the Loop did not run because the one before failed.

    They stay in ``actions`` (``StepRecord.skipped_actions`` counts them) and
    have no result: they read as not executed, never as failed.
    """
    count = record.get("skipped_actions") or 0
    actions = record.get("actions") or []
    return actions[len(actions) - count :] if count else []


def skip_reason(record: dict[str, Any]) -> str:
    """Why the skipped actions did not run, numbered as the agents tell their models."""
    return f"action {len(record.get('action_results') or [])} failed"


def _skipped_parts(record: dict[str, Any], first: bool) -> list[Part]:
    parts: list[Part] = []
    for index, action in enumerate(skipped_actions(record)):
        if index or not first:
            parts.append((", ", "plain"))
        parts.extend([(f"{action.get('name', '?')} ", "plain"), ("(not executed)", "muted")])
    return parts


def action_parts(record: dict[str, Any]) -> list[Part]:
    """Actual attempts, then source material, without inferring success.

    A restored step lists every restoration attempt with its own returned
    outcome (an attempt never executed says so), then names the source
    actions as source material. The attempts come first so that a narrow
    cell keeps what this run did. A policy step names its actions and marks
    only the exceptions: a failed result, and the actions the Loop skipped
    after it, which read as not executed. Its plain text is
    :func:`step_actions`.
    """
    if record.get("origin") != "restored":
        actions = record.get("actions") or []
        results = record.get("action_results") or []
        ran = len(actions) - len(skipped_actions(record))
        parts: list[Part] = []
        for index, action in enumerate(actions[:ran]):
            if index:
                parts.append((", ", "plain"))
            parts.append((action.get("name", "?"), "plain"))
            result = results[index] if index < len(results) else None
            if result is not None and result.get("success") is False:
                parts.extend([(" (", "plain"), ("failed", "bad"), (")", "plain")])
        return parts + _skipped_parts(record, first=not ran)
    parts = [("actual: ", "muted")]
    attempts = (record.get("restoration") or {}).get("actions", [])
    if not attempts:
        parts.append(("no execution recorded", "warn"))
    for index, action in enumerate(attempts):
        if index:
            parts.append((", ", "plain"))
        executed = action.get("executed")
        if executed is None:
            parts.append((f"not executed ({action.get('failure') or 'no action'})", "bad"))
            continue
        text, tone = _attempt_result(action)
        parts.extend([(f"{executed.get('name', '?')} (", "plain"), (text, tone), (")", "plain")])
    parts.extend(_skipped_parts(record, first=not attempts))
    source = record.get("source_history") or {}
    names = ", ".join(action.get("name", "?") for action in source.get("source_actions", []))
    parts.append((f"  source: {names or '—'}", "muted"))
    return parts


def step_actions(record: dict[str, Any]) -> str:
    """Summarize actual attempts and source material without inferring success."""
    return plain(action_parts(record))


def origin_parts(origin: str | None, *, checkpoint: bool = False) -> list[Part]:
    origin = origin or "policy"
    tone: Tone = {"restored": "info", "terminal": "muted"}.get(origin, "plain")  # type: ignore[assignment]
    parts: list[Part] = [(f"{GLYPH.get(origin, '?')} {origin}", tone)]
    if checkpoint:
        parts.append((f" {GLYPH['checkpoint']}", "accent"))
    return parts


def verdict_parts(gap: dict[str, Any] | None) -> list[Part]:
    """The restorer's tolerance verdict; a missing check is not a pass."""
    if gap is None:
        return [("—", "muted")]
    verdict = gap.get("verdict")
    if verdict == "ok":
        return [(f"{GLYPH['ok']} ok", "ok")]
    if verdict == "warn":
        return [(f"{GLYPH['warn']} warn", "warn")]
    if verdict == "diverged":
        return [(f"{GLYPH['bad']} diverged", "bad")]
    return [(f"{GLYPH['unknown']} no verdict", "muted")]


def uniform_unmeasured_parts(everywhere: dict[str, Any] | None) -> list[Part]:
    """The run-level statement of facts no checked step measured: unknown, never zero.

    ``everywhere`` is the read side's ``unmeasured_everywhere``.
    """
    facts = (everywhere or {}).get("facts") or []
    if not facts:
        return []
    checked = everywhere["checked_steps"] if everywhere else 0
    where = "the one checked step" if checked == 1 else f"all {checked} checked steps"
    return [(f"{GLYPH['unknown']} at {where}: ", "muted"), ("; ".join(facts), "plain")]


def measured_parts(gap: dict[str, Any] | None, uniform: Sequence[str] = ()) -> list[Part]:
    """Measured differences and unmeasured facts, counted separately.

    "no measured difference" never claims identity, and an unmeasured fact
    is a count of its own, never a zero difference. Facts in ``uniform``
    are unmeasured at every checked step of the run and are stated once
    for the run instead of on every row.
    """
    if gap is None:
        return [("—", "muted")]
    differences = len(gap["differences"])
    unmeasured = sum(line not in uniform for line in gap["unmeasured"])
    parts: list[Part] = []
    if differences:
        parts.append((f"{GLYPH['differs']} {differences} different", "warn"))
    if unmeasured:
        if parts:
            parts.append((" ", "plain"))
        parts.append((f"{GLYPH['unknown']} {unmeasured} unmeasured", "muted"))
    return parts or [("no measured difference", "muted")]


def gap_text(gap: dict[str, Any] | None) -> str:
    """The verdict and measurements as one plain line."""
    if gap is None:
        return "—"
    return f"{plain(verdict_parts(gap))} · {plain(measured_parts(gap))}"


def duration_parts(milliseconds: float | None) -> list[Part]:
    if milliseconds is None:
        return [(f"{GLYPH['unknown']}", "muted")]
    return [(format_duration(milliseconds / 1000), "plain")]


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def step_row(
    record: dict[str, Any], *, checkpoint: int | None = None, uniform: Sequence[str] = ()
) -> list[list[Part]]:
    """Cells of one step row: step, origin, check, measured, actions, time, error."""
    gap = restoration_gap(record)
    step = record.get("step")
    error = record.get("error")
    return [
        [(str(step), "strong")],
        origin_parts(
            record.get("origin"), checkpoint=step == checkpoint and checkpoint is not None
        ),
        verdict_parts(gap),
        measured_parts(gap, uniform),
        action_parts(record),
        duration_parts(record.get("duration_ms")),
        [(str(error), "bad")] if error else [],
    ]


STEP_COLUMNS = ("Step", "Origin", "Check", "Measured", "Actions", "Time", "Error")


# --- Run rows --------------------------------------------------------------

# Normal endings stay quiet; colour is kept for the endings that need a look.
_ENDING_TONE: dict[str, Tone] = {
    "done": "plain",
    "max_steps": "plain",
    "timeout": "warn",
    "error": "bad",
    "vetoed": "bad",
}
_TRACE_TONE: dict[str, Tone] = {
    "complete": "muted",
    "writing": "info",
    "incomplete": "warn",
    "unreadable": "bad",
}
_KIND_LABEL = {
    "live": "live",
    "replay": "replay",
    "branch": "branch",
    "branch_replay": "branch replay",
    "unknown": "unknown",
}


def ending_parts(value: str | None) -> list[Part]:
    """How the agent ended; ``done`` is an ending, not a task verdict."""
    if not value:
        return [("—", "muted")]
    return [(value, _ENDING_TONE.get(value, "plain"))]


def trace_parts(value: str | None) -> list[Part]:
    value = value or "unreadable"
    return [(value, _TRACE_TONE.get(value, "plain"))]


def tokens_parts(tokens: int | None, *, partial: bool = False) -> list[Part]:
    """Known tokens; ``≥`` when some calls reported no usage, ``unknown`` when none did."""
    if tokens is None:
        return [("unknown", "muted")]
    return [(f"≥{tokens:,}" if partial else f"{tokens:,}", "plain")]


def config_label(source: dict[str, Any] | None, root: Path | None) -> str:
    """A recorded config source, relative to the browsed root when inside it."""
    path = (source or {}).get("path")
    if not path:
        return "unknown"
    if root is not None:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return path


def run_row(row: dict[str, Any], root: Path | None = None) -> list[list[Part]]:
    """Cells of one recorded run: task, kind, history, ending, trace, steps, tokens, time, model, config."""
    if row.get("read_error"):
        # The reason sits in the one column that never drops.
        return [
            [
                (str(row.get("run_id") or row["key"]), "plain"),
                (f"  {GLYPH['bad']} {row['read_error']}", "bad"),
            ],
            *([("—", "muted")] for _ in range(3)),
            trace_parts("unreadable"),
            *([("—", "muted")] for _ in range(4)),
            [(str(row["key"]), "muted")],
        ]
    strategy = row.get("strategy") or {}
    history: list[Part] = (
        [(str(strategy.get("history_source") or "unknown"), "plain")]
        if row.get("branch")
        else [("—", "muted")]
    )
    kind = str(row.get("execution_kind") or "unknown")
    policy, total = row.get("policy_steps"), row.get("total_steps")
    steps: list[Part] = [(str(policy if policy is not None else "—"), "strong")]
    if total is not None and total != policy:
        steps.append((f" of {total}", "muted"))
    milliseconds = row.get("execution_ms")
    return [
        [(str(row.get("task_id") or row.get("run_id") or row["key"]), "plain")],
        [(_KIND_LABEL.get(kind, kind), "plain")],
        history,
        ending_parts(row.get("agent_done")),
        trace_parts(row.get("trace_status")),
        steps,
        tokens_parts(row.get("tokens"), partial=bool(row.get("usage_missing"))),
        duration_parts(milliseconds) if milliseconds is not None else [("unknown", "muted")],
        [(f"{row.get('agent_id') or '—'} · {row.get('model') or '—'}", "plain")],
        [(config_label(row.get("config_source"), root), "muted")],
    ]


@dataclass(frozen=True)
class RunNode:
    """One visible row of the run list, placed by recorded branch provenance."""

    key: str
    depth: int
    descendants: int
    expanded: bool
    last: bool
    note: tuple[Part, ...] = ()


def _source_note(row: dict[str, Any], keys: set[str]) -> tuple[Part, ...]:
    """Why a branch run is not under its source: only from the recorded resolution."""
    resolution = row.get("source_resolution")
    source_id = (row.get("branch") or {}).get("source_run_id", "?")
    if resolution == "found":
        if row.get("source_key") in keys:
            return ()
        # The source key is in the run's overview; the row only says it is elsewhere.
        return (("  ↳ source not listed", "muted"),)
    if resolution == "missing":
        return ((f"  {GLYPH['warn']} source run {source_id} missing under this root", "warn"),)
    if resolution == "ambiguous":
        return ((f"  {GLYPH['warn']} several runs here share source id {source_id}", "warn"),)
    return ()


def run_tree(rows: Sequence[dict[str, Any]], expanded: Iterable[str]) -> list[RunNode]:
    """Branch runs nested under the run their provenance names, as far as it resolves.

    A branch goes under its source only when the repository resolved the
    recorded ``source_run_id`` to exactly one run under the root and that run
    is in ``rows``; otherwise it stays at the top with the reason. Directory
    names play no part. Top-level order is the list's own; branches of one
    source are ordered by checkpoint, then task and run key.
    """
    keys = {row["key"] for row in rows}
    open_keys = set(expanded)
    children: dict[str, list[dict[str, Any]]] = {}
    top: list[dict[str, Any]] = []
    for row in rows:
        parent = row.get("source_key") if row.get("source_resolution") == "found" else None
        if (
            row.get("branch")
            and isinstance(parent, str)
            and parent in keys
            and parent != row["key"]
        ):
            children.setdefault(parent, []).append(row)
        else:
            top.append(row)
    # A provenance cycle would leave runs reachable from no top row: lift
    # the first of them to the top, which makes the rest of the cycle reachable.
    reachable: set[str] = set()

    def reach(key: str) -> None:
        stack = [key]
        while stack:
            current = stack.pop()
            if current not in reachable:
                reachable.add(current)
                stack.extend(child["key"] for child in children.get(current, []))

    for row in top:
        reach(row["key"])
    for row in rows:
        if row["key"] not in reachable:
            for siblings in children.values():
                if row in siblings:
                    siblings.remove(row)
            top.append(row)
            reach(row["key"])
    for siblings in children.values():
        siblings.sort(
            key=lambda row: (
                (row.get("branch") or {}).get("checkpoint_step", 1 << 30),
                str(row.get("task_id") or ""),
                row["key"],
            )
        )

    def count(key: str, seen: frozenset[str]) -> int:
        return sum(
            1 + count(child["key"], seen | {key})
            for child in children.get(key, [])
            if child["key"] not in seen
        )

    nodes: list[RunNode] = []

    def visit(row: dict[str, Any], depth: int, last: bool, seen: frozenset[str]) -> None:
        key = row["key"]
        below = count(key, seen)
        is_open = key in open_keys
        note = _source_note(row, keys) if depth == 0 and row.get("branch") else ()
        nodes.append(RunNode(key, depth, below, is_open, last, note))
        if is_open:
            kids = [child for child in children.get(key, []) if child["key"] not in seen]
            for index, child in enumerate(kids):
                visit(child, depth + 1, index == len(kids) - 1, seen | {key})

    for row in top:
        visit(row, 0, False, frozenset())
    return nodes


def tree_task_parts(node: RunNode, row: dict[str, Any]) -> list[Part]:
    """The task cell with its place in the lineage: fold mark, branch line, count."""
    # Top-level rows keep a fold column so their task ids align; branch rows
    # without branches of their own need none.
    fold = ("▾ " if node.expanded else "▸ ") if node.descendants else "  " if not node.depth else ""
    branch = "" if node.depth == 0 else "  " * node.depth + ("└ " if node.last else "├ ")
    parts: list[Part] = [(branch, "muted"), (fold, "accent")]
    parts.append((str(row.get("task_id") or row.get("run_id") or row["key"]), "plain"))
    if node.descendants:
        noun = "branch" if node.descendants == 1 else "branches"
        parts.append((f"  {node.descendants} {noun}", "muted"))
    parts.extend(node.note)
    return parts


RUN_COLUMNS = (
    "Task",
    "Kind",
    "History",
    "Ending",
    "Trace",
    "Policy",
    "Tokens",
    "Time",
    "Agent · model",
    "Config source",
)


# --- Fact sheets -----------------------------------------------------------


def _params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    return " " + ", ".join(f"{key}={value}" for key, value in params.items())


def _first_lines(text: str, limit: int = 6) -> list[str]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[:limit] + (["…"] if len(lines) > limit else [])


def _gap_facts(gap: dict[str, Any], uniform: Sequence[str] = ()) -> Sheet:
    """The step's own verdict and every recorded measurement, uniform ones marked."""
    sheet: Sheet = [
        fact("Check", *verdict_parts(gap), ("  ", "plain"), *measured_parts(gap, uniform))
    ]
    for line in gap["differences"]:
        sheet.append(fact("", (f"{GLYPH['differs']} {line}", "warn")))
    for line in gap["unmeasured"]:
        note = " (at every checked step of this run)" if line in uniform else ""
        sheet.append(fact("", (f"{GLYPH['unknown']} {line}{note}", "muted")))
    return sheet


def _result_parts(result: dict[str, Any] | None) -> list[Part]:
    if result is None:
        return [("no result returned", "warn")]
    if result.get("error"):
        return [(f"{GLYPH['bad']} {result['error']}", "bad")]
    if result.get("success") is False:
        return [(f"{GLYPH['bad']} failed", "bad")]
    if result.get("success") is True:
        return [(f"{GLYPH['ok']} ok", "ok")]
    return [("unknown result", "muted")]


def _attempt_facts(record: dict[str, Any], comparison: dict[str, Any] | None) -> Sheet:
    """Every restoration attempt with its own returned outcome."""
    sheet: Sheet = [Heading("Actual restoration in this run")]
    attempts = (record.get("restoration") or {}).get("actions", [])
    if not attempts:
        sheet.append(fact("Attempts", ("no execution recorded", "warn")))
    for index, attempt in enumerate(attempts, 1):
        executed = attempt.get("executed")
        label = "Attempts" if index == 1 else ""
        if executed is None:
            sheet.append(
                fact(
                    label,
                    (f"{index}. not executed", "bad"),
                    (f" ({attempt.get('failure') or 'no action'})", "muted"),
                )
            )
            continue
        text, tone = _attempt_result(attempt)
        glyph = {"ok": GLYPH["ok"], "bad": GLYPH["bad"]}.get(tone, GLYPH["unknown"])
        sheet.append(
            fact(
                label,
                (f"{index}. {executed.get('name', '?')}{_params(executed.get('params'))}", "plain"),
                ("  ", "plain"),
                (f"{glyph} {text}", tone),
                (f"  {attempt.get('method') or ''}", "muted"),
                (" · retargeted" if executed != attempt.get("recorded") else "", "warn"),
            )
        )
        if attempt.get("live_error"):
            sheet.append(fact("", (f"   {attempt['live_error']}", "bad")))
    for index, action in enumerate(skipped_actions(record), 1):
        sheet.append(
            fact(
                "Not run" if index == 1 else "",
                (f"{action.get('name', '?')}{_params(action.get('params'))}", "plain"),
                (f"  not executed: {skip_reason(record)}", "muted"),
            )
        )
    for index, item in enumerate((comparison or {}).get("results") or [], 1):
        status = item.get("status")
        tone = "warn" if status == "changed" else "muted"
        sheet.append(fact("Results" if index == 1 else "", (f"{index}. {status} vs source", tone)))
    return sheet


def _source_facts(source: dict[str, Any]) -> Sheet:
    """The recorded step a restored step re-executed, labelled as source material."""
    if not source:
        return []
    sheet: Sheet = [Heading("Source material · recorded, not this run's thinking")]
    sheet.append(
        fact(
            "From",
            (f"step {source.get('source_step')} of run {source.get('source_run_id')}", "muted"),
        )
    )
    for index, line in enumerate(_first_lines(source.get("source_reply") or "")):
        sheet.append(fact("Reply" if index == 0 else "", (line, "muted")))
    for index, action in enumerate(source.get("source_actions", [])):
        sheet.append(
            fact(
                "Actions" if index == 0 else "",
                (f"{action.get('name', '?')}{_params(action.get('params'))}", "muted"),
            )
        )
    return sheet


def step_facts(
    record: dict[str, Any],
    *,
    checkpoint: int | None = None,
    comparison: dict[str, Any] | None = None,
    uniform: Sequence[str] = (),
) -> Sheet:
    """What one step did, in reading order; the JSON below it stays complete."""
    origin = record.get("origin") or "policy"
    step = record.get("step")
    heading = f"Step {step} · {origin}"
    if checkpoint is not None and step == checkpoint:
        heading += f" · checkpoint {GLYPH['checkpoint']}"
    sheet: Sheet = [Heading(heading)]
    gap = restoration_gap(record)
    if gap is not None:
        sheet += _gap_facts(gap, uniform)
    if origin == "terminal":
        sheet.append(
            fact("Observed", ("final observation after the run ended; no decision", "muted"))
        )
    if origin == "restored":
        sheet += _attempt_facts(record, comparison)
        sheet += _source_facts(record.get("source_history") or {})
    else:
        reply = record.get("llm_output") or ""
        if origin == "policy":
            lines = _first_lines(reply)
            if not lines:
                sheet.append(fact("Reply", ("none recorded", "muted")))
            for index, line in enumerate(lines):
                sheet.append(fact("Reply" if index == 0 else "", (line, "plain")))
        results = record.get("action_results", [])
        actions = record.get("actions", [])
        ran = len(actions) - len(skipped_actions(record))
        for index, action in enumerate(actions, 1):
            result = results[index - 1] if index - 1 < len(results) else None
            outcome: list[Part] = (
                _result_parts(result)
                if index <= ran
                else [(f"not executed: {skip_reason(record)}", "muted")]
            )
            sheet.append(
                fact(
                    "Actions" if index == 1 else "",
                    (f"{index}. {action.get('name', '?')}{_params(action.get('params'))}", "plain"),
                    ("  ", "plain"),
                    *outcome,
                )
            )
        if record.get("truncated_actions"):
            sheet.append(
                fact(
                    "",
                    (f"{record['truncated_actions']} more actions discarded by the loop", "warn"),
                )
            )
    if record.get("observation_summary"):
        sheet.append(fact("Observation", (record["observation_summary"], "muted")))
    timing: list[Part] = [*duration_parts(record.get("duration_ms"))]
    if record.get("llm_latency_ms") is not None:
        timing.extend([("  of which model ", "muted"), *duration_parts(record["llm_latency_ms"])])
    sheet.append(fact("Time", *timing))
    if record.get("error"):
        phase = f" ({record['error_phase']})" if record.get("error_phase") else ""
        sheet.append(fact("Error", (f"{record['error']}{phase}", "bad")))
    return sheet


def _replay_log_parts(log: dict[str, Any] | None) -> list[Part]:
    """Unbound, empty and unreadable replay logs read differently; unknown is not zero."""
    if not log:
        return [("not recorded", "muted")]
    if log.get("error"):
        return [(f"{GLYPH['bad']} unreadable: {log['error']}", "bad")]
    if not log.get("recorded"):
        return [("not recorded (no replay log bound)", "muted")]
    counts = log.get("counts") or {}
    if not any(counts.values()):
        return [("recorded, no serving decisions", "muted")]
    parts: list[Part] = []
    for name in ("hit", "miss", "exhausted", "substituted", "synthetic"):
        if parts:
            parts.append(("  ", "plain"))
        value = counts.get(name, 0)
        tone: Tone = "warn" if value and name != "hit" else "plain" if value else "muted"
        parts.append((f"{value:,} {name}", tone))
    return parts


def _plural(count: Any, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def run_facts(detail: dict[str, Any], root: Path | None = None) -> Sheet:
    """A run's overview: what it was and how it ended, its lineage, then its record."""
    summary = detail.get("summary") or {}
    execution = detail.get("execution") or {}
    sheet: Sheet = [Heading(f"Run {summary.get('key') or summary.get('run_id') or ''}")]
    if summary.get("task"):
        sheet.append(fact("Task", (str(summary["task"]), "strong")))
    sheet.append(
        fact(
            "Agent",
            f"{summary.get('agent_id') or '—'} / {summary.get('loop_id') or '—'} / "
            f"{summary.get('model') or '—'}",
        )
    )
    outcome: list[Part] = [("agent ended ", "muted"), *ending_parts(summary.get("agent_done"))]
    if summary.get("env_status"):
        outcome.extend([("  environment ", "muted"), (str(summary["env_status"]), "plain")])
    sheet.append(fact("Outcome", *outcome))
    if summary.get("answer"):
        sheet.append(fact("Answer", str(summary["answer"])))
    if summary.get("error"):
        sheet.append(fact("Error", (str(summary["error"]), "bad")))
    verdicts = summary.get("verdicts") or []
    if summary.get("evaluation_error"):
        sheet.append(fact("Evaluations", (f"unreadable: {summary['evaluation_error']}", "bad")))
    elif not verdicts:
        sheet.append(fact("Evaluations", ("none recorded", "muted")))
    for index, item in enumerate(verdicts):
        sheet.append(
            fact(
                "Evaluations" if index == 0 else "",
                (f"{item.get('evaluator')}: ", "muted"),
                (str(item.get("predicted_label")), "strong"),
                (f"  ({item.get('verdict_source')})", "muted"),
            )
        )
    relationship = execution.get("relationship")
    if relationship:
        sheet += lineage_facts(execution, summary.get("branch"), detail.get("restoration_overview"))
    if parts := uniform_unmeasured_parts(detail.get("unmeasured_everywhere")):
        sheet.append(fact("Unmeasured", *parts))
    if execution.get("network_source") or relationship:
        sheet.append(
            fact(
                "Network",
                (str(execution.get("network_source") or "unknown"), "plain"),
                ("  replay log ", "muted"),
                *_replay_log_parts(detail.get("replay_log")),
            )
        )
    sheet.append(Heading("Record"))
    recording: list[Part] = [*trace_parts(summary.get("trace_status"))]
    if summary.get("complete_through") is not None:
        recording.append((f", complete through step {summary['complete_through']}", "muted"))
    if summary.get("missing_steps"):
        recording.append((f", missing steps {summary['missing_steps']}", "warn"))
    channels = [name for name, on in (summary.get("recording") or {}).items() if on]
    recording.append((f"  channels: {', '.join(channels) or 'none'}", "muted"))
    sheet.append(fact("Trace", *recording))
    for problem in summary.get("recording_problems") or []:
        sheet.append(fact("", (str(problem), "warn")))
    steps: list[Part] = [(str(summary.get("policy_steps", "—")), "strong"), (" policy", "plain")]
    if summary.get("total_steps") is not None:
        steps.append((f" of {_plural(summary['total_steps'], 'recorded step')}", "muted"))
    sheet.append(fact("Steps", *steps))
    calls = summary.get("llm_calls")
    usage: list[Part] = [
        (_plural(calls, "model call") if calls is not None else "unknown model calls", "plain"),
        ("  ", "plain"),
        *tokens_parts(summary.get("tokens"), partial=bool(summary.get("usage_missing"))),
        (" known tokens", "muted"),
    ]
    if summary.get("usage_missing"):
        usage.append((f"  {_plural(summary['usage_missing'], 'call')} without usage", "warn"))
    sheet.append(fact("Usage", *usage))
    milliseconds = summary.get("execution_ms")
    sheet.append(
        fact(
            "Time",
            *(duration_parts(milliseconds) if milliseconds is not None else UNKNOWN),
        )
    )
    sheet.append(
        fact(
            "Config",
            (config_label(summary.get("config_source"), root), "plain"),
            (f"  hash {summary.get('config_hash') or 'unknown'}", "muted"),
        )
    )
    return sheet


_RESOLUTION = {
    "found": None,
    "none": "no recorded source relationship",
    "missing": "source run unavailable under this root",
    "ambiguous": "several runs under this root share the source id",
    "outside_root": "source is outside this root",
    "inconsistent_reference": "step and run name different source runs",
}


def lineage_facts(
    execution: dict[str, Any], branch: dict[str, Any] | None, overview: dict[str, Any] | None
) -> Sheet:
    """Fork or replay provenance exactly as recorded."""
    relationship = execution.get("relationship")
    sheet: Sheet = [Heading("Branch" if relationship == "branch" else "Replay")]
    source: list[Part] = [
        (str(execution.get("source_key") or execution.get("source_run_id") or "unknown"), "strong")
    ]
    problem = _RESOLUTION.get(execution.get("source_resolution") or "none")
    if problem and execution.get("source_resolution") != "none":
        source.append((f"  {GLYPH['warn']} {problem}", "warn"))
    elif execution.get("source_key"):
        source.append(("  s opens it", "muted"))
    sheet.append(fact("Source", *source))
    if relationship != "branch":
        return sheet
    checkpoint = execution.get("checkpoint")
    sheet.append(
        fact(
            "Checkpoint",
            (f"step {checkpoint} {GLYPH['checkpoint']}", "accent"),
            (f"  branch {(branch or {}).get('branch_index', '—')}", "muted"),
        )
    )
    sheet.append(
        fact(
            "Strategy",
            ("Agent history: ", "muted"),
            (str(execution.get("history_source") or "unknown"), "strong"),
            ("  verify: ", "muted"),
            (str(execution.get("verify") or "unknown"), "strong"),
            (f"  restorer {execution.get('restorer') or 'unknown'}", "muted"),
        )
    )
    sheet.append(
        fact("Budget", *budget_parts(execution.get("max_steps"), execution.get("continue_steps")))
    )
    if overview:
        parts: list[Part] = [
            (f"{overview['checked']} of {overview['expected']} checked  ", "plain"),
            (
                f"{GLYPH['ok']} {overview['accepted']} ok  ",
                "ok" if overview["accepted"] else "muted",
            ),
            (
                f"{GLYPH['warn']} {overview['warned']} warn  ",
                "warn" if overview["warned"] else "muted",
            ),
            (
                f"{GLYPH['bad']} {overview['diverged']} diverged",
                "bad" if overview["diverged"] else "muted",
            ),
        ]
        sheet.append(fact("Checks", *parts))
        different = overview.get("different_steps") or []
        sheet.append(
            fact(
                "",
                (
                    f"{GLYPH['differs']} measured differences at steps {', '.join(map(str, different))}",
                    "warn",
                )
                if different
                else ("no measured difference at any checked step", "muted"),
            )
        )
    return sheet


def budget_parts(max_steps: int | None, continue_steps: int | None) -> list[Part]:
    """The configured ceiling and the branch continuation, never a fork's budget as a fraction."""
    parts: list[Part] = [
        ("configured ceiling ", "muted"),
        (str(max_steps) if max_steps is not None else "—", "plain"),
    ]
    if continue_steps is not None:
        parts.extend([("  continue_steps ", "muted"), (str(continue_steps), "strong")])
    return parts


def task_facts(row: dict[str, Any]) -> Sheet:
    """A scheduled task as the CLI reported it."""
    sheet: Sheet = [Heading(f"Task {row.get('task_id')}")]
    sheet.append(fact("State", *task_state_parts(row.get("status"))))
    if row.get("stop_reason"):
        sheet.append(fact("Stopped", (str(row["stop_reason"]), "warn")))
    sheet.append(fact("Ending", *ending_parts(row.get("agent_done"))))
    if row.get("env_status"):
        sheet.append(fact("Environment", str(row["env_status"])))
    sheet.append(
        fact(
            "Trace",
            *(trace_parts(row.get("trace_status")) if row.get("trace_status") else NONE),
        )
    )
    if "tokens" in row:
        sheet.append(fact("Tokens", *tokens_parts(row.get("tokens"))))
    if row.get("elapsed_ms") is not None:
        sheet.append(fact("Time", *duration_parts(row["elapsed_ms"])))
    if row.get("trace_dir"):
        sheet.append(fact("Trace dir", (str(row["trace_dir"]), "muted")))
    for flag, text in (
        ("infrastructure_failure", "infrastructure failure"),
        ("vetoed", "vetoed by a guard"),
    ):
        if row.get(flag):
            sheet.append(fact("", (f"{GLYPH['bad']} {text}", "bad")))
    return sheet


_TASK_STATE: dict[str, Part] = {
    "queued": (f"{GLYPH['queued']} queued", "muted"),
    "running": (f"{GLYPH['running']} running", "info"),
    "finished": (f"{GLYPH['ok']} finished", "plain"),
    "interrupted": (f"{GLYPH['warn']} interrupted", "warn"),
}


def task_state_parts(status: str | None) -> list[Part]:
    return [_TASK_STATE.get(status or "", (status or "—", "plain"))]


def task_note_parts(row: dict[str, Any]) -> list[Part]:
    """What else the CLI reported about a task: only the exceptions."""
    notes: list[Part] = []
    if row.get("stop_reason"):
        notes.append((f"stopped: {row['stop_reason']}", "warn"))
    if row.get("vetoed"):
        notes.append((f"{GLYPH['bad']} vetoed by a guard", "bad"))
    if row.get("infrastructure_failure"):
        notes.append((f"{GLYPH['bad']} infrastructure failure", "bad"))
    if row.get("env_status") and row["env_status"] != "success":
        notes.append((f"environment {row['env_status']}", "warn"))
    joined: list[Part] = []
    for note in notes:
        if joined:
            joined.append(("  ", "plain"))
        joined.append(note)
    return joined


def task_row(row: dict[str, Any]) -> list[list[Part]]:
    milliseconds = row.get("elapsed_ms")
    return [
        [(str(row["task_id"]), "plain")],
        task_state_parts(row.get("status")),
        ending_parts(row.get("agent_done")),
        trace_parts(row.get("trace_status")) if row.get("trace_status") else NONE,
        tokens_parts(row.get("tokens")) if "tokens" in row else NONE,
        duration_parts(milliseconds) if milliseconds is not None else NONE,
        task_note_parts(row),
    ]


TASK_COLUMNS = ("Task", "State", "Ending", "Trace", "Tokens", "Time", "Note")


def message_sheet(title: str, lines: Sequence[str], tone: Tone = "plain") -> Sheet:
    sheet: Sheet = [Heading(title)]
    sheet.extend([fact("", (line, tone)) for line in lines])
    return sheet
