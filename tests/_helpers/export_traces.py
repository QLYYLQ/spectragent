"""Recorded runs for the training export, written by the real ``WALWriter`` (M32).

The export reads only what a run committed (``RunTrace``), so every source
here goes through ``init_run`` / ``commit_step`` / ``finalize`` and the real
fragment encoder (``tests/_helpers/trace_builder.py``'s rule: nothing here
spells a path or a JSON shape). :func:`write_run` commits one run from a
list of :class:`ExportStep`; each step carries its record (origin, error
phase, executed actions and their results, the recorded reply), the
fragments it observed (real fragment instances, encoded as production
encodes them), the keys whose capture failed, and its archive files.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from spectragent.core.archive.privacy import RedactionEntry, RedactionLedger
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.trace.encoding import TraceFile, encode_fragment
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.eval_result import EvalResult
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import StepInfoObs, TaskObs
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import agent_result, write_verdict

TASK_TEXT = "Put the blue kettle in the cart"
START_URL = "https://shop.example.test/"


def png(width: int, height: int, color: tuple[int, int, int] = (230, 230, 230)) -> bytes:
    """Real PNG bytes of *width* x *height*."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def screenshot(
    *, viewport: tuple[int, int] = (1280, 720), dpr: float = 1, color: int = 230
) -> ScreenshotObs:
    """A browser screenshot fragment with real image bytes at device scale factor *dpr*."""
    image = (round(viewport[0] * dpr), round(viewport[1] * dpr))
    return ScreenshotObs(
        image_bytes=png(*image, (color, color, color)),
        viewport_width=viewport[0],
        viewport_height=viewport[1],
        image_width=image[0],
        image_height=image[1],
    )


@dataclass
class ExportStep:
    """One committed step of a recorded run.

    ``results`` defaults to one successful result per action (every action
    executed). ``fragments`` are fragment instances, encoded as production
    encodes them; a loop ``task`` / ``step_info`` pair is added unless
    ``loop_fragments`` is false. ``failed`` names fragments whose capture
    failed.
    """

    actions: list[Action] = field(default_factory=list)
    results: list[ActionResult] | None = None
    llm_output: str = ""
    origin: str = "policy"
    error_phase: str = ""
    fragments: list[Any] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    extra_files: list[TraceFile] = field(default_factory=list)
    loop_fragments: bool = True
    restoration: RestorationCheck | None = None


def setup_record(*, redacted: bool = False) -> TaskSetupRecord:
    entries = (
        [RedactionEntry(location="/start_url", field="token", rule="url_query")] if redacted else []
    )
    return TaskSetupRecord(
        context={"start_url": START_URL}, redactions=RedactionLedger(entries=entries)
    )


_DEFAULT: Any = object()


def write_run(
    root: Path,
    run_id: str,
    steps: Sequence[ExportStep],
    *,
    agent_id: str = "browser_use",
    llm_model: str = "gpt-4o",
    benchmark: str = "online_mind2web",
    subset: str = "test",
    task_id: str = "task-001",
    task: str = TASK_TEXT,
    task_setup: Any = _DEFAULT,
    negotiation: TraceNegotiation | None = _DEFAULT,
    branch: BranchProvenance | None = None,
    status: TraceStatus = TraceStatus.COMPLETE,
    incomplete_reason: str = "recording failed at step 1: ArchiveCollectionError: boom",
    agent_done: AgentDoneStatus | None = AgentDoneStatus.DONE,
    verdicts: dict[str, EvalResult] | None = None,
    secrets_recorded: bool = False,
) -> Path:
    """Commit one recorded run under ``root/<run_id>`` and return its directory.

    ``status`` is the trace's ledger (``writing``: never closed out;
    ``incomplete`` closes it with *incomplete_reason*); ``agent_done`` the
    agent's, written as ``result.json`` (``None``: none).
    """
    wal = WALWriter(root, run_id)
    wal.init_run(
        TraceConfig(
            agent_id=agent_id,
            loop_id="branch" if branch is not None else "react",
            env_type="browser",
            llm_model=llm_model,
            benchmark=benchmark,
            subset=subset,
            task=task,
            task_id=task_id,
            negotiation=(
                TraceNegotiation(
                    active_fragments=["browser.screenshot", "loop.step_info", "loop.task"],
                    env_sourced_fragments=["browser.screenshot"],
                    loop_sourced_fragments=["loop.step_info", "loop.task"],
                )
                if negotiation is _DEFAULT
                else negotiation
            ),
            task_setup=setup_record() if task_setup is _DEFAULT else task_setup,
            branch=branch,
            secrets_recorded=secrets_recorded,
        )
    )
    for index, spec in enumerate(steps):
        results = (
            spec.results
            if spec.results is not None
            else [ActionResult(success=True) for _ in spec.actions]
        )
        values: list[Any] = list(spec.fragments)
        if spec.loop_fragments:
            values += [
                TaskObs(user_request=task),
                StepInfoObs(step=index + 1, max_steps=30, today="2026-10-11"),
            ]
        values += [
            FragmentFailure(fragment_id=key, capability="browser", error="TimeoutError")
            for key in spec.failed
        ]
        encoded = [encode_fragment(index, value) for value in values]
        wal.commit_step(
            index,
            StepRecord(
                step=index,
                observation_summary=f"page {index}",
                llm_output=spec.llm_output,
                actions=list(spec.actions),
                action_results=results,
                origin=spec.origin,  # type: ignore[arg-type]
                error_phase=spec.error_phase,
                restoration=spec.restoration,
            ),
            fragments=[item for item in encoded if item is not None],
            extra_files=list(spec.extra_files),
        )
    if agent_done is not None:
        clean = agent_done in (AgentDoneStatus.DONE, AgentDoneStatus.MAX_STEPS)
        wal.write_result(
            agent_result(
                task_id=task_id,
                agent_done=agent_done,
                agent_reported_success=True if agent_done is AgentDoneStatus.DONE else None,
                error=None if clean else f"the run ended {agent_done.value}",
            )
        )
    if status is not TraceStatus.WRITING:
        wal.finalize(
            status,
            incomplete_reason=incomplete_reason if status is TraceStatus.INCOMPLETE else None,
        )
    for slug, verdict in (verdicts or {}).items():
        write_verdict(wal.run_dir, slug, verdict)
    return wal.run_dir


# ── a UI-TARS teacher, the v1 student's own action space (M32 export tests) ──

#: What a UI-TARS run over the browser negotiates (``write_run(negotiation=...)``).
UI_TARS_NEGOTIATION = TraceNegotiation(
    active_fragments=["browser.screenshot", "loop.step_info", "loop.task"],
    required_fragments=["browser.screenshot", "loop.task"],
    env_sourced_fragments=["browser.screenshot"],
    loop_sourced_fragments=["loop.step_info", "loop.task"],
    active_actions=[
        "browser.click_coordinate",
        "browser.done",
        "browser.drag",
        "browser.scroll",
        "browser.send_keys",
        "browser.type_coordinate",
        "browser.wait",
    ],
)
#: What a DOM teacher (``browser_use`` in dom_vision mode) negotiates: the DOM,
#: the screenshot, the page meta, and index actions besides the coordinate ones.
BROWSER_USE_NEGOTIATION = TraceNegotiation(
    active_fragments=[
        "browser.browser_meta",
        "browser.dom",
        "browser.screenshot",
        "loop.step_info",
        "loop.task",
    ],
    required_fragments=["browser.dom", "loop.task"],
    env_sourced_fragments=["browser.browser_meta", "browser.dom", "browser.screenshot"],
    loop_sourced_fragments=["loop.step_info", "loop.task"],
    active_actions=[
        "browser.click",
        "browser.done",
        "browser.navigate",
        "browser.scroll",
        "browser.type",
    ],
)


def click_at(x: int, y: int) -> Action:
    """A recorded ``browser.click_coordinate`` at viewport point (*x*, *y*)."""
    return Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y})


def finished(text: str = "done") -> Action:
    """A recorded ``browser.done`` (UI-TARS ``finished``)."""
    return Action(name="browser.done", params={"extracted_content": text, "success": True})


def ui_tars_step(
    action: Action, *, thought: str = "look", color: int = 230, **kwargs: Any
) -> ExportStep:
    """One UI-TARS step: its screenshot, the reply that named *action*, the action itself."""
    return ExportStep(
        actions=[action],
        llm_output=f"Thought: {thought}\nAction: (recorded reply)",
        fragments=[screenshot(color=color)],
        **kwargs,
    )


def write_ui_tars_run(root: Path, run_id: str, steps: Sequence[ExportStep], **kwargs: Any) -> Path:
    """:func:`write_run` for a UI-TARS teacher on a benchmark with no eval loader."""
    kwargs.setdefault("agent_id", "ui_tars")
    kwargs.setdefault("negotiation", UI_TARS_NEGOTIATION)
    kwargs.setdefault("benchmark", "export_test")
    return write_run(root, run_id, steps, **kwargs)
