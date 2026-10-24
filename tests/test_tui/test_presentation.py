"""Terminal wording keeps the design's reading rules, independent of layout."""

from pathlib import Path

from spectragent.tui.presentation import (
    budget_parts,
    config_label,
    measured_parts,
    plain,
    run_facts,
    run_row,
    sheet_text,
    step_actions,
    step_facts,
    step_row,
    tokens_parts,
    verdict_parts,
)
from spectragent.tui.summary import summary_sheet


def _gap(differences=(), unmeasured=(), verdict="ok"):
    return {
        "status": "changed"
        if differences
        else "unmeasured"
        if unmeasured
        else "no_measured_difference",
        "differences": list(differences),
        "unmeasured": list(unmeasured),
        "verdict": verdict,
    }


def test_a_pass_within_tolerance_still_shows_measured_differences_and_unmeasured_facts():
    gap = _gap(["browser: screenshot dHash distance 3"], ["browser: DOM signature not measured"])
    assert plain(verdict_parts(gap)) == "✓ ok"
    measured = plain(measured_parts(gap))
    assert "≠ 1 different" in measured
    assert "? 1 unmeasured" in measured
    # Nothing measured is not drawn as "identical", and no check is not a pass.
    assert plain(measured_parts(_gap())) == "no measured difference"
    assert plain(verdict_parts(None)) == "—"
    assert plain(verdict_parts(_gap(verdict=None))) == "? no verdict"


def _restored(**attempt):
    return {
        "step": 0,
        "origin": "restored",
        "actions": [],
        "action_results": [],
        "source_history": {
            "source_run_id": "source",
            "source_step": 0,
            "source_reply": "Thought: open the page",
            "source_actions": [{"name": "browser.click", "params": {"index": 1}}],
        },
        "restoration": {
            "step": 0,
            "verdict": "warn",
            "reason": "action_failed",
            "reasons": ["action_failed"],
            "actions": [
                {
                    "recorded": {"name": "browser.click", "params": {"index": 1}},
                    "executed": {"name": "browser.click", "params": {"index": 9}},
                    "method": "geometry_signature",
                    "recorded_success": True,
                    **attempt,
                }
            ],
        },
    }


def test_restored_step_rows_put_this_runs_attempts_before_source_material():
    text = step_actions(_restored(live_success=False, live_error="element disappeared"))
    assert text.startswith("actual: browser.click (failed)")
    assert text.index("actual:") < text.index("source: browser.click")
    assert "actual: browser.click (unknown result)" in step_actions(_restored(live_success=None))
    never = _restored()
    never["restoration"]["actions"][0].update(executed=None, failure="no target")
    assert "not executed (no target)" in step_actions(never)


def test_restored_step_facts_label_the_old_reply_as_source_material():
    sheet = sheet_text(step_facts(_restored(live_success=True)))
    actual = sheet.index("Actual restoration in this run")
    source = sheet.index("Source material · recorded, not this run's thinking")
    assert actual < source
    assert "Thought: open the page" in sheet[source:]
    assert "retargeted" in sheet[actual:source]


def test_checkpoint_and_step_time_appear_on_the_step_row():
    row = step_row({"step": 3, "origin": "policy", "duration_ms": None}, checkpoint=3)
    assert plain(row[1]) == "● policy ◆"
    # An unmeasured duration is a question mark, never 0.
    assert plain(row[5]) == "?"
    assert plain(step_row({"step": 1, "duration_ms": 1500})[5]) == "1.5s"


def test_unknown_usage_is_never_drawn_as_zero():
    assert plain(tokens_parts(None)) == "unknown"
    assert plain(tokens_parts(1234, partial=True)) == "≥1,234"
    snapshot = {
        "identity": {},
        "status": "running",
        "active": [],
        "phase": "between operations",
        "agent_done": None,
        "task_id": "t",
        "tasks_total": 1,
        "tasks_completed": 0,
        "elapsed_s": 1.0,
        "policy_steps": 0,
        "max_steps": None,
        "continue_steps": None,
        "calls": 2,
        "usage_missing": 2,
        "tokens": 0,
        "token_limit": None,
        "branch": None,
        "trace_path": None,
    }
    usage = next(
        line
        for line in sheet_text(summary_sheet(snapshot, complete=False)).splitlines()
        if line.startswith("Usage")
    )
    assert "known session tokens none reported" in usage
    assert "tokens 0" not in usage
    no_calls = sheet_text(
        summary_sheet({**snapshot, "calls": 0, "usage_missing": 0}, complete=False)
    )
    assert "known session tokens 0" in no_calls


def test_branch_budget_is_a_configured_ceiling_never_a_fraction():
    text = plain(budget_parts(30, 0))
    assert text == "configured ceiling 30  continue_steps 0"
    assert "/" not in text


def test_replay_log_states_read_differently():
    base = {"summary": {}, "execution": {"network_source": "archive", "relationship": "replay"}}

    def network(log):
        lines = sheet_text(run_facts({**base, "replay_log": log})).splitlines()
        return next(line for line in lines if line.startswith("Network"))

    assert "not recorded (no replay log bound)" in network(
        {"recorded": False, "counts": None, "error": None}
    )
    assert "recorded, no serving decisions" in network(
        {"recorded": True, "counts": dict.fromkeys(("hit", "miss"), 0), "error": None}
    )
    assert "unreadable: broken batch" in network(
        {"recorded": True, "counts": None, "error": "broken batch"}
    )
    counted = network(
        {"recorded": True, "counts": {"hit": 447, "miss": 120, "exhausted": 0}, "error": None}
    )
    assert "447 hit" in counted and "120 miss" in counted and "0 exhausted" in counted


def test_unreadable_runs_show_their_reason_in_the_row():
    row = run_row({"key": "broken", "run_id": "broken", "read_error": "manifest does not parse"})
    cells = [plain(cell) for cell in row]
    assert "unreadable" in cells
    assert any("manifest does not parse" in cell for cell in cells)


def test_config_source_is_shown_relative_to_the_browsed_root():
    root = Path("/experiments")
    assert config_label({"path": "/experiments/configs/a.yaml"}, root) == "configs/a.yaml"
    assert config_label({"path": "/elsewhere/a.yaml"}, root) == "/elsewhere/a.yaml"
    assert config_label(None, root) == "unknown"


def test_fork_overview_shows_strategy_checkpoint_and_checks():
    detail = {
        "summary": {
            "key": "fork",
            "branch": {"branch_index": 0},
            "policy_steps": 1,
            "total_steps": 7,
        },
        "execution": {
            "relationship": "branch",
            "source_key": "record/source",
            "source_resolution": "found",
            "checkpoint": 5,
            "history_source": "restored",
            "verify": "warn",
            "restorer": "prefix_replay",
            "continue_steps": 0,
            "max_steps": 32,
            "network_source": "archive",
        },
        "restoration_overview": {
            "expected": 6,
            "checked": 6,
            "accepted": 5,
            "warned": 1,
            "diverged": 0,
            "different_steps": [1, 2],
        },
        "replay_log": None,
    }
    text = sheet_text(run_facts(detail))
    assert "record/source  s opens it" in text
    assert "step 5 ◆" in text
    assert "Agent history: restored  verify: warn" in text
    assert "configured ceiling 32  continue_steps 0" in text
    assert "6 of 6 checked" in text and "! 1 warn" in text
    assert "measured differences at steps 1, 2" in text
