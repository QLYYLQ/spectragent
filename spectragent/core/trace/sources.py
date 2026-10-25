"""Which recorded runs a sources root and its globs name, and which task each was (arch §4.10, §4.15, §4.16).

The rules the offline consumers share: the ``trace_branches`` task loader
(branch tasks from recorded runs), the training export (samples from recorded
runs) and the flywheel metrics (a report over a chain's runs) all take a root
and globs relative to it. A match is a run when it is a directory holding a
``manifest.json``; the matches come back sorted by their POSIX path relative
to the root, so the order never depends on the filesystem's.

The export and the metrics also share what "the task" and "the site" of a run
are (:func:`task_identity`: a branch run's are its source task's) and how a
run that does not read back is named (:func:`unreadable_reason`), so the
dataset's ``split.by: site`` / ``exclude_sites`` and the report's per-site
statistics use one key.

The branch loader and the export also share how they select sources by the
two ledgers (arch §4.10, §4.15): a hard condition on the trace's own ledger —
closed, and complete through the step used (:func:`not_closed_reason`,
:func:`past_complete_reason`) — and a filter on the agent's, how the run
ended (:data:`AgentOutcomeFilter`, :func:`agent_outcome_filtered_out`).

And which recorded steps they leave out because the step's observation was
captured before the captured surface finished its initial load
(:func:`unloaded_capture_reason`, arch §4.4 "初次加载是否完成"): the branch
loader's ``exclude_unloaded_checkpoints`` and the export's
``filters.exclude_unloaded_steps`` read the same fact the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, get_args
from urllib.parse import urlsplit

from spectragent.core.trace.encoding import MANIFEST_FILE
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig, TraceManifest, TraceStatus
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.capture_state import CaptureStateFragment
from spectragent.schemas.observation.failure import FragmentFailure

if TYPE_CHECKING:
    from spectragent.core.trace.reader import RunTrace

logger = logging.getLogger(__name__)

#: The agent-outcome filter of a source selection (``trace_branches``'
#: ``agent_outcome``, the export's ``filters.agent_outcome``): ``any`` keeps
#: every source; any other value keeps the runs whose ``result.json`` says the
#: agent ended that way (``AgentDoneStatus``).
AgentOutcomeFilter = Literal["any", "done", "max_steps", "error", "timeout", "vetoed"]
#: :data:`AgentOutcomeFilter`'s values, for a check outside pydantic.
AGENT_OUTCOMES: tuple[str, ...] = get_args(AgentOutcomeFilter)
#: The local name every capability's capture state carries
#: (``browser.capture_state``, ``desktop.capture_state``); negotiation lets at
#: most one fragment take a local name per run.
_CAPTURE_STATE = "capture_state"


def source_pattern_problem(pattern: str) -> str | None:
    """Why *pattern* cannot name runs under a sources root, or ``None`` when it can.

    A pattern is relative to the root and stays inside it: not empty, not
    absolute, no ``~``, no ``..``.
    """
    if not pattern:
        return "is empty"
    path = PurePosixPath(pattern)
    if path.is_absolute() or pattern.startswith("~"):
        return "must be relative to the sources root"
    if ".." in path.parts:
        return "must stay inside the sources root (no '..')"
    return None


def match_runs(root: Path, patterns: Sequence[str]) -> list[tuple[str, Path]]:
    """``(relative POSIX path, directory)`` of every run directory a glob matches, by path.

    A match without a ``manifest.json`` is not a run and is ignored (logged
    at DEBUG); a run two globs both match is listed once.
    """
    found: dict[str, Path] = {}
    for pattern in patterns:
        for match in root.glob(pattern):
            if match.is_dir() and (match / MANIFEST_FILE).is_file():
                found.setdefault(match.relative_to(root).as_posix(), match)
            else:
                logger.debug("%s is not a recorded run; ignored", match)
    return sorted(found.items())


def unreadable_reason(directory: Path, exc: TraceSchemaError) -> str:
    """Why the run in *directory* is left out, worded without the reader's message.

    ``TraceSchemaError`` names files by absolute path (and may quote the bytes
    that did not parse); a dataset manifest or a metrics report holds no
    absolute path and is the same whichever root the runs sit under (arch
    §4.15 "数据集 manifest", §4.16 "报告"). So the reason names the file
    relative to the run directory and the error class — callers log the
    message itself.
    """
    where = ""
    path = exc.path
    if path is not None and path.is_relative_to(directory):
        file = path.relative_to(directory).as_posix()
        if file != ".":
            where = f": {file} does not read back"
    return f"not a readable trace{where} ({type(exc).__name__})"


@dataclass(frozen=True)
class TaskIdentity:
    """The task a run was recorded on — a branch run's is its source task's.

    ``task`` is the instruction the run itself was given. A branch run under a
    task variant was given another instruction than its source task's
    (``BranchProvenance.task_variant``); ``source_task`` keeps the source's,
    which the benchmark, subset and task id above belong to.
    """

    benchmark: str
    subset: str
    task_id: str
    task: str
    start_url: str | None
    source_task: str | None = None

    @property
    def instructions(self) -> tuple[str, ...]:
        """Every instruction the run stands for: its own, and its source's under a task variant.

        Decontamination by instruction checks each one: a variant's text is
        new, but the screens it was cut from were recorded on the source task.
        """
        if self.source_task is None or self.source_task == self.task:
            return (self.task,)
        return (self.task, self.source_task)

    @property
    def key(self) -> str:
        """``<benchmark>/<subset>/<task_id>`` — the ``split.by: task`` key."""
        return f"{self.benchmark}/{self.subset}/{self.task_id}"

    @property
    def site(self) -> str | None:
        """The start URL's host — the ``split.by: site`` key — or ``None`` without one."""
        if not self.start_url:
            return None
        host = urlsplit(self.start_url).hostname
        return host.lower() if host else None


def task_identity(config: TraceConfig) -> TaskIdentity:
    """The task *config*'s run was recorded on; for a branch run, its source task.

    The start URL is the recorded task setup's (M28: value-level redaction
    leaves the host alone); a branch run's own setup context is its source
    task's, re-resolved. A task-variant branch's ``task`` is the variant it
    ran and ``source_task`` the source run's instruction.
    """
    setup = config.task_setup
    start_url = setup.context.get("start_url") if setup is not None else None
    branch = config.branch
    if branch is not None:
        variant = branch.task_variant
        return TaskIdentity(
            benchmark=branch.source_benchmark,
            subset=branch.source_subset,
            task_id=branch.source_task_id,
            task=config.task,
            start_url=start_url if isinstance(start_url, str) else None,
            source_task=variant.source_task_text if variant is not None else None,
        )
    return TaskIdentity(
        benchmark=config.benchmark,
        subset=config.subset,
        task_id=config.task_id,
        task=config.task,
        start_url=start_url if isinstance(start_url, str) else None,
    )


def not_closed_reason(manifest: TraceManifest) -> str | None:
    """Why no step of the run may be used, when its trace was never closed; else ``None``.

    A hard condition of every source selection, not a filter: a trace still
    ``writing`` is being recorded right now, or its process died before
    closing it out, and nothing about it is final.
    """
    if manifest.status is not TraceStatus.WRITING:
        return None
    return (
        "its trace was never closed (status writing): the run is still being recorded, or its "
        "process died before closing the trace out"
    )


def past_complete_reason(manifest: TraceManifest, step: int) -> str | None:
    """Why *step* may not be used because the trace is not complete through it; else ``None``.

    A hard condition: past ``complete_through`` the trace lost a step's
    record or evidence, so a branch from there would restore a world nobody
    recorded, and an exported step's context would have a hole.
    """
    if step <= manifest.complete_through:
        return None
    why = f": {manifest.incomplete_reason}" if manifest.incomplete_reason else ""
    return (
        f"step {step} is past the step its trace is complete through "
        f"({manifest.complete_through}){why}"
    )


def agent_outcome_filtered_out(trace: RunTrace, wanted: AgentOutcomeFilter) -> str | None:
    """Why the agent-outcome filter *wanted* leaves *trace* out, or ``None`` when it is kept.

    Reads ``result.json`` only when the filter is set (``any`` reads nothing).

    Raises:
        TraceSchemaError: ``result.json`` does not read back.
    """
    if wanted == "any":
        return None
    result = trace.result
    if result is None:
        return f"it has no result.json, so how its agent ended is unknown (agent_outcome: {wanted})"
    if result.agent_done != wanted:
        return f"its agent ended {result.agent_done.value} (agent_outcome: {wanted})"
    return None


def unloaded_capture_reason(trace: RunTrace, step: int) -> str | None:
    """Why *step*'s observation was captured before its initial load finished; else ``None``.

    Reads the step's recorded capture state through the platform-neutral
    :class:`CaptureStateFragment`, never a capability's own labels: the
    run's negotiated env-source fragment whose local name is
    ``capture_state``. A reason only when its ``settle_state.loaded`` is
    ``False`` — the initial load was still in progress when the step was
    captured (arch §4.4 "初次加载是否完成"). ``None`` is no evidence: the run
    negotiated no capture state, the step recorded none, its capture failed,
    there was no wait, or the load was not known to be incomplete.

    Raises:
        RecordingScopeError: The run recorded no observations.
        TraceSchemaError: The recorded fragment does not read back, or is not
            a capture state.
    """
    negotiation = trace.config.negotiation
    keys = (
        [k for k in negotiation.env_sourced_fragments if fragment_field_name(k) == _CAPTURE_STATE]
        if negotiation is not None
        else []
    )
    for key in keys:
        fragment = trace.observation_fragment(step, key)
        if fragment is None or isinstance(fragment, FragmentFailure):
            continue
        if not isinstance(fragment, CaptureStateFragment):
            raise TraceSchemaError(
                f"step {step} records {key!r} as a {type(fragment).__name__}, not a capture state"
            )
        settle = fragment.settle_state
        if settle is not None and settle.loaded is False:
            return (
                f"step {step} was captured before its initial load finished ({key}: the wait "
                f"ended {settle.outcome} after {settle.waited_ms} ms)"
            )
    return None


__all__ = [
    "AGENT_OUTCOMES",
    "AgentOutcomeFilter",
    "TaskIdentity",
    "agent_outcome_filtered_out",
    "match_runs",
    "not_closed_reason",
    "past_complete_reason",
    "source_pattern_problem",
    "task_identity",
    "unloaded_capture_reason",
    "unreadable_reason",
]
