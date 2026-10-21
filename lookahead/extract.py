"""``extract``: one row per step that has an observation, as JSON lines.

Policy and restored steps get a row; a terminal step appears only as the next
observation of the step before it. Every fact comes from the trace's public
read boundary (``RunTrace``) and the browser package's readers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from lookahead import contracts
from lookahead.rows import (
    ActionRow,
    BranchInfo,
    BranchRestoration,
    NextObservation,
    ObservationRef,
    RawDom,
    RestorationSummary,
    Settle,
    StepRow,
)
from lookahead.runs import Run, RunSet
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    Location,
    NavigationEffect,
    PointerEffect,
    ScrollEffect,
    ScrollOffset,
    SelectionEffect,
    TabFacts,
    TabSwitchEffect,
    TextInputEffect,
    ViewportPoint,
)
from spectragent.schemas.action import Action, ActionEffect, ActionResult
from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.step_record import StepRecord

#: Step origins that get a row of their own.
ROW_ORIGINS = frozenset({"policy", "restored"})


def element(fact: Any) -> dict[str, str] | str | None:
    """An effect's element fact as JSON: ``{"role", "name"}``, ``"nothing"`` or a gap word."""
    if isinstance(fact, ElementFacts):
        return {"role": fact.role, "name": fact.name}
    return fact if isinstance(fact, str) else None


def _point(fact: Any) -> list[int] | str | None:
    if isinstance(fact, ViewportPoint):
        return [fact.x, fact.y]
    return fact if isinstance(fact, str) else None


def _offset(fact: Any) -> list[int] | str | None:
    if isinstance(fact, ScrollOffset):
        return [fact.x, fact.y]
    return fact if isinstance(fact, str) else None


def _url(fact: Any) -> str | None:
    if isinstance(fact, Location):
        return fact.url
    if isinstance(fact, TabFacts) and isinstance(fact.location, Location):
        return fact.location.url
    return None


def effect_facts(effect: ActionEffect | None, params: dict[str, Any]) -> dict[str, Any]:
    """The effect's most used facts, flattened; the typed text from the parameters."""
    facts: dict[str, Any] = {}
    if isinstance(effect, PointerEffect):
        facts["point"] = _point(effect.point)
        if effect.release_point is not None:
            facts["release_point"] = _point(effect.release_point)
        facts["hit"] = element(effect.hit)
        facts["on_target"] = effect.on_target
        facts["focus_after"] = element(effect.focus_after)
    elif isinstance(effect, TextInputEffect):
        facts["target"] = element(effect.target)
        facts["value_changed"] = effect.value_changed
        facts["keys"] = list(effect.keys)
        facts["focus_after"] = element(effect.focus_after)
    elif isinstance(effect, ScrollEffect):
        facts["direction"] = effect.direction
        facts["container"] = effect.container
        facts["scroll_before"] = _offset(effect.before)
        facts["scroll_after"] = _offset(effect.after)
        facts["at_boundary"] = effect.at_boundary
    elif isinstance(effect, NavigationEffect | TabSwitchEffect):
        facts["url_before"] = _url(effect.before)
        facts["url_after"] = _url(effect.after)
        facts["document_loaded"] = effect.document_loaded
    elif isinstance(effect, SelectionEffect):
        facts["selected"] = contracts.plain(effect.selected)
        facts["value_changed"] = effect.value_changed
    text = params.get("text")
    if isinstance(text, str):
        facts["text"] = text
    return facts


def action_rows(actions: list[Action], results: list[ActionResult]) -> list[ActionRow]:
    rows: list[ActionRow] = []
    for index, action in enumerate(actions):
        result = results[index] if index < len(results) else None
        effect = result.effect if result is not None else None
        rows.append(
            ActionRow(
                index=index,
                name=action.name,
                params=contracts.plain(action.params),
                success=result.success if result is not None else None,
                error=result.error if result is not None else None,
                effect=effect.model_dump(mode="json") if effect is not None else None,
                effect_text=effect.describe() if effect is not None else None,
                facts=effect_facts(effect, action.params),
            )
        )
    return rows


def restoration_summary(check: RestorationCheck | None) -> RestorationSummary | None:
    if check is None:
        return None
    evidence = check.evidence[0] if check.evidence else None
    return RestorationSummary(
        verdict=check.verdict,
        reason=check.reason,
        reasons=list(check.reasons),
        screenshot_distance=evidence.screenshot_distance if evidence is not None else None,
        url_match=evidence.url_match if evidence is not None else None,
        document_misses=evidence.document_misses if evidence is not None else None,
        actions=len(check.actions),
        failed_actions=sum(
            1
            for action in check.actions
            if action.failure is not None or action.live_success is False or action.live_error
        ),
    )


def branch_restoration(run: Run) -> BranchRestoration:
    branch = run.config.branch
    assert branch is not None
    verdicts: dict[str, int] = {}
    restored = 0
    first_problem: int | None = None
    checkpoint: RestorationSummary | None = None
    for step in run.steps():
        record = run.records[step]
        check = record.restoration
        if check is None:
            continue
        if record.origin == "restored":
            restored += 1
        if step == branch.checkpoint_step:
            checkpoint = restoration_summary(check)
        verdicts[check.verdict] = verdicts.get(check.verdict, 0) + 1
        if check.verdict != "ok" and first_problem is None:
            first_problem = step
    return BranchRestoration(
        checkpoint=checkpoint,
        restored_steps=restored,
        verdicts=dict(sorted(verdicts.items())),
        first_problem_step=first_problem,
    )


def branch_info(run: Run, runs: RunSet) -> BranchInfo | None:
    branch = run.config.branch
    if branch is None:
        return None
    source = runs.source_of(run)
    return BranchInfo(
        source_run_id=branch.source_run_id,
        source_trace=branch.source_trace,
        source_run=source.key if source is not None else None,
        source_task=source.config.task if source is not None else None,
        checkpoint_step=branch.checkpoint_step,
        branch_index=branch.branch_index,
        branch_group_id=branch.branch_group_id,
        restoration=branch_restoration(run),
    )


def observation_ref(run: Run, step: int, record: StepRecord) -> ObservationRef:
    shot = run.screenshot(step)
    if shot is None:
        return ObservationRef(step=step, origin=record.origin, screenshot=False)
    return ObservationRef(
        step=step,
        origin=record.origin,
        screenshot=run.has_screenshot(step),
        url=shot.source_url,
        captured_at=shot.captured_at,
        image_width=shot.image_width or None,
        image_height=shot.image_height or None,
        viewport_width=shot.viewport_width or None,
        viewport_height=shot.viewport_height or None,
    )


def next_observation(run: Run, step: int) -> NextObservation:
    later = step + 1
    record = run.records.get(later)
    if record is None or not run.has_observation(later):
        return NextObservation(step=None, exists=False)
    ref = observation_ref(run, later, record)
    return NextObservation(
        step=later, exists=True, origin=record.origin, screenshot=ref.screenshot, url=ref.url
    )


def _settle(run: Run, step: int) -> Settle | None:
    shot = run.screenshot(step)
    settle = shot.settle if shot is not None else None
    if settle is None:
        state = run.capture_state(step)
        settle = state.settle if state is not None else None
    if settle is None:
        return None
    return Settle(
        outcome=settle.outcome,
        waited_ms=settle.waited_ms,
        waiting_for=[str(item) for item in settle.waiting_for],
        detail=settle.detail,
    )


def _capture_state(run: Run, step: int) -> dict[str, Any] | None:
    state = run.capture_state(step)
    if state is None:
        return None
    foreground = state.foreground_change
    return {
        "pointer": state.pointer.model_dump(mode="json"),
        "failed_visible_resources": state.failed_visible_resources.model_dump(mode="json"),
        "foreground_change": foreground.model_dump(mode="json") if foreground is not None else None,
    }


def step_rows(run: Run, runs: RunSet) -> Iterator[StepRow]:
    """Every row of one run, in step order."""
    branch = branch_info(run, runs)
    variant = contracts.task_variant(run.config)
    for step in run.steps():
        record = run.records[step]
        if record.origin not in ROW_ORIGINS or not run.has_observation(step):
            continue
        following = next_observation(run, step)
        yield StepRow(
            run=run.key,
            run_id=run.run_id,
            kind=run.kind,
            site=run.site,
            task=run.config.task,
            task_variant=variant,
            step=step,
            origin=record.origin,
            timestamp=record.timestamp or None,
            error_phase=record.error_phase,
            error=record.error,
            llm_output=record.llm_output,
            observation=observation_ref(run, step, record),
            observation_cycle=run.cycles.get(step),
            settle=_settle(run, step),
            capture_state=_capture_state(run, step),
            actions=action_rows(record.actions, record.action_results),
            action_spans=contracts.action_spans(record),
            frames=run.frames_after(step),
            next=following,
            restoration=restoration_summary(record.restoration),
            branch=branch,
            raw_dom=RawDom(
                current=run.raw_dom(step),
                next=following.step is not None and run.raw_dom(following.step),
            ),
        )


def all_rows(runs: RunSet) -> Iterator[StepRow]:
    for run in runs.runs:
        yield from step_rows(run, runs)


def write_rows(runs: RunSet, out: Path) -> int:
    """Write every row as one JSON object a line; the number written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for row in all_rows(runs):
            handle.write(row.model_dump_json() + "\n")
            count += 1
    return count
