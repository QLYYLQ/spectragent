"""The live monitor's status card, built from one progress snapshot.

A status word in reverse video, then labelled facts. Known token totals say
"known" and a session whose every call lacked usage says so instead of 0;
the configured ``max_steps`` is a ceiling next to the policy-step count,
never a fraction of it.
"""

from __future__ import annotations

from typing import Any

from spectragent.tui.presentation import (
    Banner,
    Part,
    Sheet,
    Tone,
    budget_parts,
    fact,
    format_duration,
)

_STATUS_TONE: dict[str, Tone] = {
    "preparing": "muted",
    "running": "info",
    "cancelling": "warn",
    "finished": "ok",
    "failed": "bad",
    "cancelled": "warn",
}


def _phase(snapshot: dict[str, Any]) -> list[Part]:
    active = snapshot["active"]
    if not active:
        return [(snapshot["phase"], "muted")]
    innermost = active[-1]
    detail = f" {innermost['detail']}" if innermost.get("detail") else ""
    parts: list[Part] = []
    if len(active) > 1:
        parts.append((" › ".join(item["phase"] for item in active[:-1]) + " › ", "muted"))
    parts.extend(
        [
            (f"{innermost['phase']}{detail}", "strong"),
            (f" {innermost['elapsed_s']:.1f}s", "plain"),
        ]
    )
    return parts


def _tokens(snapshot: dict[str, Any]) -> list[Part]:
    calls, missing = snapshot["calls"], snapshot["usage_missing"]
    parts: list[Part] = [("known session tokens ", "muted")]
    if calls and missing == calls:
        parts.append(("none reported", "warn"))
    else:
        parts.append((f"{snapshot['tokens']:,}", "strong"))
    if snapshot["token_limit"] is not None:
        limit = snapshot["token_limit"]
        parts.append((f" / {limit:,}" if isinstance(limit, int) else f" / {limit}", "plain"))
    parts.append(("  calls without usage ", "muted"))
    parts.append((str(missing), "warn" if missing else "plain"))
    return parts


def _bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return ""
    filled = round(width * done / total)
    return "▰" * filled + "▱" * (width - filled) + " "


def summary_sheet(snapshot: dict[str, Any], *, complete: bool) -> Sheet:
    identity = snapshot["identity"]
    status = str(snapshot["status"])
    head: list[Part] = [(f" {status.upper()} ", "badge"), ("  ", "plain"), *_phase(snapshot)]
    if snapshot["agent_done"]:
        head.extend([("  agent ended ", "muted"), (str(snapshot["agent_done"]), "strong")])
    if complete:
        head.extend([("  Press q to return", "muted")])
    sheet: Sheet = [Banner(tuple(head), badge=_STATUS_TONE.get(status, "warn"))]
    sheet.append(
        fact(
            "Task",
            (snapshot["task_id"] or "—", "strong"),
            ("  ", "plain"),
            (
                f"{identity.get('agent_id', '—')} / {identity.get('loop_id', '—')} / "
                f"{identity.get('llm_model', '—')}",
                "plain",
            ),
        )
    )
    total, completed = snapshot["tasks_total"], snapshot["tasks_completed"]
    sheet.append(
        fact(
            "Progress",
            ("tasks ", "muted"),
            (_bar(completed, total), "info"),
            (f"{completed}/{total}", "strong"),
            ("  elapsed ", "muted"),
            (format_duration(snapshot["elapsed_s"]), "plain"),
        )
    )
    sheet.append(
        fact(
            "Steps",
            ("policy steps ", "muted"),
            (str(snapshot["policy_steps"]), "strong"),
            ("  ", "plain"),
            *budget_parts(snapshot["max_steps"], snapshot["continue_steps"]),
        )
    )
    sheet.append(
        fact(
            "Usage",
            ("model calls ", "muted"),
            (str(snapshot["calls"]), "strong"),
            ("  ", "plain"),
            *_tokens(snapshot),
        )
    )
    branch = snapshot.get("branch")
    if branch:
        sheet.append(
            fact(
                "Branch",
                ("source ", "muted"),
                (f"{branch.get('source_run_id', '—')}", "strong"),
                ("  checkpoint ", "muted"),
                (f"◆ {branch.get('checkpoint_step', '—')}", "accent"),
                ("  branch ", "muted"),
                (str(branch.get("branch_index", "—")), "plain"),
                ("  Agent history: ", "muted"),
                (identity.get("history_source") or "unknown", "strong"),
                ("  verify: ", "muted"),
                (identity.get("verify") or "unknown", "strong"),
            )
        )
    trace = snapshot["trace_path"]
    sheet.append(fact("Trace", (trace or "not opened", "plain" if trace else "muted")))
    return sheet
