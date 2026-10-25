"""Recorded runs for the ``trace_branches`` loader, written by the real ``WALWriter``.

Every source a loader test reads is committed the way production commits it —
``init_run`` / ``commit_step`` / ``finalize`` — so a source is exactly as
readable as a recorded run is, and an uncommitted step is invisible for the
same reason it is in production (``tests/_helpers/trace_builder.py``'s rule:
nothing here spells a path or a JSON shape).

:func:`write_source` commits a run whose shape each loader rule reads: the
trace's ledger (the manifest status and how far it is complete), the agent's
(``result.json``), the configuration (task, identity, the setup context, the
negotiation with its archive binding, a branch provenance), the step records
(origin, error phase), a step's recorded ``browser.capture_state`` (how its
capture's settle wait ended), the environment fingerprint, and verdicts in
``evals/``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spectragent.core.archive.privacy import RedactionEntry, RedactionLedger
from spectragent.core.capabilities.browser.observations import BrowserCaptureStateObs
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.trace.encoding import EncodedFragment, encode_fragment
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
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import agent_result, archive_file, write_verdict

#: The setup context every source records unless told otherwise.
START_URL = "https://shop.test/"
TASK_TEXT = "put the blue kettle in the cart"


#: A capture taken before the page finished loading: the main document's ``load``
#: never came within the settle cap (§4.4 "初次加载是否完成").
NOT_LOADED = PageSettle(waited_ms=5002, outcome="capped", waiting_for=["load", "dom", "content"])
#: A page whose DOM never went quiet: capped, but its document had loaded.
NEVER_QUIET = PageSettle(waited_ms=5001, outcome="capped", waiting_for=["dom"])
#: A capture state whose capture failed (recorded as a ``FragmentFailure``).
CAPTURE_FAILED = "failed"


@dataclass(frozen=True)
class Step:
    """One committed step's record, as far as the loader reads it.

    ``capture``: the settle record of the step's ``browser.capture_state``
    (recorded only when some step of the run has one, which also puts the
    fragment in the run's negotiation); :data:`CAPTURE_FAILED` records its
    capture as failed; ``None`` records no capture state at this step.
    """

    origin: str = "policy"
    error_phase: str = ""
    capture: PageSettle | str | None = None


def _capture_fragment(step: int, capture: PageSettle | str) -> EncodedFragment:
    if capture == CAPTURE_FAILED:
        value: Any = FragmentFailure(
            fragment_id="browser.capture_state", capability="browser", error="TimeoutError"
        )
    else:
        value = BrowserCaptureStateObs(
            pointer=PointerState(status="unknown"),
            failed_visible_resources=FailedVisibleResources(status="measured", count=0),
        )
        value.settle = capture
    encoded = encode_fragment(step, value)
    assert encoded is not None
    return encoded


def setup_record(
    context: dict[str, Any] | None = None, *, redacted: bool = False
) -> TaskSetupRecord:
    """A recorded setup context; *redacted* adds one ledger entry (a value was replaced)."""
    entries = (
        [RedactionEntry(location="/token", field="token", rule="field_name")] if redacted else []
    )
    return TaskSetupRecord(
        context=dict(context if context is not None else {"start_url": START_URL}),
        redactions=RedactionLedger(entries=entries),
    )


_DEFAULT = object()
#: Agent outcomes a result carries no error for.
_CLEAN = frozenset({AgentDoneStatus.DONE, AgentDoneStatus.MAX_STEPS})


def write_source(
    root: Path,
    run_id: str,
    *,
    steps: Sequence[Step] | int = 3,
    status: TraceStatus = TraceStatus.COMPLETE,
    incomplete_reason: str = "recording failed at step 2: ArchiveCollectionError: boom",
    agent_done: AgentDoneStatus | None = AgentDoneStatus.DONE,
    task_setup: Any = _DEFAULT,
    archivers: Sequence[str] = ("browser.network",),
    fingerprint: bool = True,
    branch: BranchProvenance | None = None,
    benchmark: str = "online_mind2web",
    subset: str = "Hard",
    task_id: str = "task-001",
    env_type: str = "browser",
    extra: dict[str, Any] | None = None,
    verdicts: dict[str, EvalResult] | None = None,
    format_version: int | None = None,
    uncommitted: Sequence[int] = (),
) -> Path:
    """Commit one recorded run under ``root/<run_id>`` and return its directory.

    ``steps`` is a count of ordinary policy steps or one :class:`Step` per
    step; ``task_setup`` defaults to an unredacted ``{"start_url": ...}``
    (pass ``None`` for a run that recorded none); ``archivers`` are bound to
    the ``browser`` capability, whose environment fingerprint (a run record)
    is committed with step 0 unless ``fingerprint`` is false;
    ``format_version`` rewrites the manifest's version afterwards (``2``: a
    trace of the previous format); the steps in ``uncommitted`` are left out
    (a gap — the WAL commits a later step whatever came before it, and the
    trace is complete only through the step before the gap). ``status`` is
    the trace's (``writing``: never closed out; ``incomplete`` closes it with
    *incomplete_reason*); ``agent_done`` is the agent's, written as
    ``result.json`` (``None``: no result.json).
    """
    specs = [Step() for _ in range(steps)] if isinstance(steps, int) else list(steps)
    captured = any(spec.capture is not None for spec in specs)
    fragments = ["browser.capture_state", "browser.screenshot"] if captured else []
    wal = WALWriter(root, run_id)
    wal.init_run(
        TraceConfig(
            agent_id="ui_tars",
            loop_id="react",
            env_type=env_type,
            llm_model="gpt-4o",
            benchmark=benchmark,
            subset=subset,
            task=TASK_TEXT,
            task_id=task_id,
            extra=extra if extra is not None else {"difficulty": "hard", "tags": ["cart"]},
            negotiation=TraceNegotiation(
                active_fragments=fragments or ["browser.screenshot"],
                env_sourced_fragments=fragments,
                active_actions=["browser.click"],
                active_archivers=sorted(archivers),
                archive_bindings={key: key.partition(".")[0] for key in archivers},
            ),
            env_evidence_privacy=[] if archivers else None,
            task_setup=setup_record() if task_setup is _DEFAULT else task_setup,
            branch=branch,
        )
    )
    for index, spec in enumerate(specs):
        if index in uncommitted:
            continue
        extras = []
        if index == 0 and fingerprint and archivers:
            extras.append(
                archive_file("browser", "fingerprint.json", b"{}", scope="run", run_version=0)
            )
        wal.commit_step(
            index,
            StepRecord(
                step=index,
                observation_summary=f"page {index}",
                llm_output=f"click({index})",
                actions=[Action(name="browser.click", params={"index": index})],
                action_results=[ActionResult(success=True)],
                origin=spec.origin,  # type: ignore[arg-type]
                error_phase=spec.error_phase,
            ),
            fragments=(
                [_capture_fragment(index, spec.capture)] if spec.capture is not None else []
            ),
            extra_files=extras,
        )
    if agent_done is not None:
        wal.write_result(
            agent_result(
                agent_done=agent_done,
                agent_reported_success=True if agent_done is AgentDoneStatus.DONE else None,
                error=None if agent_done in _CLEAN else f"the run ended {agent_done.value}",
            )
        )
    if status is not TraceStatus.WRITING:
        wal.finalize(
            status,
            incomplete_reason=incomplete_reason if status is TraceStatus.INCOMPLETE else None,
        )
    for slug, verdict in (verdicts or {}).items():
        write_verdict(wal.run_dir, slug, verdict)
    if format_version is not None:
        manifest = wal.run_dir / "manifest.json"
        document = json.loads(manifest.read_text())
        document["format_version"] = format_version
        manifest.write_text(json.dumps(document))
    return wal.run_dir
