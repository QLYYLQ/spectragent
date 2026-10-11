"""The flywheel's metrics: PRD §9 and the §10 / §11 sections, from a chain's committed data (arch §4.16).

**What is read, and how.** A chain root has a fixed layout
(:class:`ChainLayout`): runs under ``traces/<role>/`` for the roles
:data:`ROLES`, the two exports under ``datasets/{first,second}/``. A role's
runs are opened with :func:`open_role` — matched by ``core/trace/sources.py``
like every other offline consumer, read only through ``RunTrace``'s accessors
(only what was committed exists); a run that does not read back is skipped
with a reason worded as the export words it (``unreadable_reason``), never
fatal. The browser's evidence is read by the browser package's own functions
(``iter_index_lines``, ``iter_decisions``, ``archive_stats``); this module
only organises the calls. Datasets are read as the files an export published.
Two measures walk directories on purpose: privacy (a leak in a file nothing
indexes is still a leak — every file under ``traces/`` and ``datasets/``) and
the dataset digests (an export's output is its files).

**Every number has its denominator.** A ratio over nothing is ``null`` with
the reason, never ``0`` — "not measured" must not read as "measured zero"
(the ``evaluator/summary.py`` rule). A percentile of no values raises.

**Lifted, not copied** (§4.16 "共用而不是复制"): :class:`RestorationRate` /
:func:`restoration_rate` and :func:`components` were M31's live-test
helpers; :class:`DedupBytes` and :func:`percentile` were M29's benchmark's
(its revisit half is the browser's ``archive_stats.revisit_saving``).

.. note::
   No ``from __future__ import annotations``: pydantic models throughout.
"""

import gzip
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, computed_field

from spectragent.core.capabilities.browser.archive_stats import (
    BodyCompleteness,
    body_completeness,
    challenge_documents,
    revisit_saving,
    site_rule_candidates,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    IndexEntry,
    ReplayArchiveError,
    iter_index_lines,
)
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLogEntry,
    iter_decisions,
)
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import CommittedFile, RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.secret_patterns import find_secrets, iter_readable
from spectragent.core.trace.sources import match_runs, task_identity, unreadable_reason
from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: The run roles of a chain, in stage order (arch §4.16 "布局").
ROLES: tuple[str, ...] = ("record", "control", "same_path", "branch")
#: The two exports of a chain; export determinism compares them.
DATASETS: tuple[str, ...] = ("first", "second")
#: The trace format with the per-step index every measure reads.
_INDEXED_FORMAT = 2
#: The site key of a run whose task recorded no start URL.
NO_SITE = "(no start_url)"
#: The action key of a miss the run's close-out collection took.
TRAILING = "trailing"
#: The action key of a miss a terminal observation's collection took (no action ran there).
TERMINAL = "terminal"
#: The action key of a step that ran no action.
NO_ACTION = "(no action)"
#: The step error phases (``StepRecord.error_phase``) in which the environment failed — the
#: ones the loop ends a run on with ``env_status: failed``. A failed model call, an
#: unparseable reply or a prompt that could not be built is not the site's failure.
ENVIRONMENT_PHASES: frozenset[str] = frozenset({"observe", "act"})
#: The step error phase of a failed model call.
MODEL_CALL_PHASE = "llm"
#: What reading a run's evidence can raise when the evidence is damaged.
EVIDENCE_ERRORS: tuple[type[Exception], ...] = (
    TraceSchemaError,
    ReplayArchiveError,
    ValueError,
    KeyError,
)
#: Leading bytes of files that are already compressed (gzip, PNG, JPEG, GIF, WebP's RIFF).
_COMPRESSED_MAGIC: tuple[bytes, ...] = (
    b"\x1f\x8b",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF8",
    b"RIFF",
)
#: The gzip level the compression estimate measures with.
GZIP_LEVEL = 6


# ── the chain root ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainLayout:
    """Where a chain keeps everything, under one root (arch §4.16 "布局").

    ``traces/{record,control,same_path,branch}/``, ``datasets/{first,second}/``,
    ``report/{report.json,report.md}``, ``configs/<stage>.yaml`` (the
    configuration each stage actually ran), ``evals/<stage>/`` (verdicts of
    a stage that evaluates) and ``chain.json`` (provenance; nothing in the
    report reads it).
    """

    root: Path

    @property
    def traces_dir(self) -> Path:
        return self.root / "traces"

    def traces(self, role: str) -> Path:
        return self.traces_dir / role

    @property
    def datasets_dir(self) -> Path:
        return self.root / "datasets"

    def dataset(self, name: str) -> Path:
        return self.datasets_dir / name

    @property
    def report_dir(self) -> Path:
        return self.root / "report"

    @property
    def configs_dir(self) -> Path:
        return self.root / "configs"

    def evals(self, stage: str) -> Path:
        return self.root / "evals" / stage

    @property
    def chain_record(self) -> Path:
        return self.root / "chain.json"


class Run:
    """One committed run of a role, read through its manifest.

    ``rel`` is its directory relative to the role's directory (POSIX) —
    what a branch run's ``source_trace`` names for a ``record`` run.
    The replay decisions and the network index lines are read once, on
    first use; damaged evidence raises one of :data:`EVIDENCE_ERRORS` there.
    """

    def __init__(
        self, role: str, rel: str, trace: RunTrace, config: TraceConfig, steps: Sequence[StepRecord]
    ) -> None:
        self.role = role
        self.rel = rel
        self.trace = trace
        self.config = config
        self.steps: tuple[StepRecord, ...] = tuple(steps)

    @property
    def pointer(self) -> str:
        """``traces/<role>/<rel>`` — how the report names the run (relative to the chain root)."""
        return f"traces/{self.role}/{self.rel}"

    @cached_property
    def site(self) -> str:
        """The run's site key — its (source) task's start-URL host (``task_identity``)."""
        return task_identity(self.config).site or NO_SITE

    @cached_property
    def task_key(self) -> str:
        return task_identity(self.config).key

    @cached_property
    def decisions(self) -> list[tuple[int | None, ReplayLogEntry, int]]:
        """Every replay-log decision, folding undone (``iter_decisions``); ``[]`` for a live run."""
        return list(iter_decisions(self.trace))

    @cached_property
    def index_lines(self) -> list[tuple[int | None, IndexEntry]]:
        """Every ``browser.network`` index line (``iter_index_lines``); ``[]`` without one."""
        return list(iter_index_lines(self.trace))

    def step(self, number: int) -> StepRecord | None:
        return next((record for record in self.steps if record.step == number), None)

    @property
    def checkpoint(self) -> int | None:
        """A branch run's checkpoint step, ``None`` for a run that is not one."""
        branch = self.config.branch
        return branch.checkpoint_step if branch is not None else None

    def actions_at(self, step: int | None) -> str:
        """The name of the action(s) *step* ran — what a miss in its collection is put under.

        A close-out collection is :data:`TRAILING`, a terminal observation
        :data:`TERMINAL` (a page a step's action opened may finish loading only
        then: the collection that takes a request names the step).
        """
        if step is None:
            return TRAILING
        record = self.step(step)
        if record is not None and record.origin == "terminal":
            return TERMINAL
        names = [action.name for action in record.actions] if record is not None else []
        return "+".join(names) if names else NO_ACTION


@dataclass(frozen=True)
class RoleRuns:
    """The runs of one role: readable ones by path, and why each other one was skipped.

    ``present`` is whether the role's directory exists at all — a stage that
    never ran, as opposed to one that ran and produced nothing readable.
    """

    role: str
    present: bool
    runs: tuple[Run, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)

    def by_rel(self) -> dict[str, Run]:
        return {run.rel: run for run in self.runs}


def open_role(directory: Path, role: str) -> RoleRuns:
    """Every run under *directory*, read through its manifest, or why it cannot be.

    A run directory is a direct child holding a ``manifest.json``
    (``match_runs``). Skipped, with the reason: a trace whose manifest,
    configuration or step records do not read back (``unreadable_reason``:
    the file, relative to the run, and the error class), and a format-1
    trace. Never raises for a damaged run.
    """
    if not directory.is_dir():
        return RoleRuns(role=role, present=False)
    runs: list[Run] = []
    skipped: dict[str, str] = {}
    for rel, run_dir in match_runs(directory, ["*"]):
        try:
            trace = RunTrace(run_dir)
            version = trace.manifest.format_version
            if version < _INDEXED_FORMAT:
                skipped[rel] = (
                    f"format {version} trace: the metrics read the format-{_INDEXED_FORMAT} "
                    f"per-step index — re-record the run"
                )
                continue
            config = trace.config
            steps = trace.step_records
        except TraceSchemaError as exc:
            logger.warning("metrics: skipping %s/%s, it does not read back: %s", role, rel, exc)
            skipped[rel] = unreadable_reason(run_dir, exc)
            continue
        runs.append(Run(role, rel, trace, config, steps))
    return RoleRuns(
        role=role, present=True, runs=tuple(runs), skipped=dict(sorted(skipped.items()))
    )


@dataclass(frozen=True)
class ChainRuns:
    """Every role of one chain root, opened."""

    layout: ChainLayout
    roles: Mapping[str, RoleRuns]

    def role(self, name: str) -> RoleRuns:
        return self.roles[name]

    def source_of(self, run: Run) -> Run | None:
        """A branch run's source run: its ``source_trace`` under ``traces/record``."""
        branch = run.config.branch
        if branch is None:
            return None
        return self.roles["record"].by_rel().get(branch.source_trace)


def open_chain(layout: ChainLayout) -> ChainRuns:
    """Open every role of *layout*'s root (:func:`open_role`)."""
    return ChainRuns(
        layout=layout, roles={role: open_role(layout.traces(role), role) for role in ROLES}
    )


# ── shared shapes ─────────────────────────────────────────────────────


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Ratio(_Model):
    """``numerator / denominator`` — ``value`` is ``null`` (with ``reason``) when nothing was measured."""

    numerator: int
    denominator: int
    value: float | None
    reason: str | None = None


def ratio(numerator: int, denominator: int, *, empty: str) -> Ratio:
    """``numerator / denominator``, or ``null`` and *empty* (the reason) when the denominator is 0."""
    if denominator <= 0:
        return Ratio(numerator=numerator, denominator=0, value=None, reason=empty)
    return Ratio(numerator=numerator, denominator=denominator, value=numerator / denominator)


def percentile(values: Sequence[float], q: float) -> float:
    """The *q*-th percentile (0–100), nearest-rank on a sorted copy (M29's definition).

    Raises:
        ValueError: *values* is empty — a percentile of nothing is not 0.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("a percentile of no values")
    rank = max(0, min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1))))
    return float(ordered[rank])


def canonical_json(value: Any) -> str:
    """*value* as JSON with sorted keys and fixed separators — byte-stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _counts(counter: Mapping[str, int]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter) if counter[key]}


def _url_path(url: str) -> str:
    """``scheme://host/path`` — a URL without its query and fragment (queries may carry tokens)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# ── lifted: M31's restoration rate and components ─────────────────────


class RestorationRate(_Model):
    """PRD §9 恢复校验率 over a set of branch runs.

    ``ok`` of ``total`` restoration checks passed (one per restored step,
    plus the checkpoint step's). Failed checks by reason — a check with
    several reasons counts for each — and failed retargets by failure and
    method (PRD §10 "元素重定位失败").
    """

    ok: int = 0
    total: int = 0
    by_reason: dict[str, int] = {}
    retarget_failures: dict[str, dict[str, int]] = {}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rate(self) -> float | None:
        """``ok / total``; ``None`` when no check was recorded."""
        return self.ok / self.total if self.total else None

    def summary(self) -> str:
        reasons = ", ".join(f"{reason}={n}" for reason, n in self.by_reason.items())
        shown = f"{self.rate:.0%}" if self.rate is not None else "n/a"
        return f"{self.ok}/{self.total} checks passed ({shown}){'; ' + reasons if reasons else ''}"


def _checks(steps: Iterable[StepRecord]) -> Iterable[RestorationCheck]:
    return (record.restoration for record in steps if record.restoration is not None)


def restoration_rate(traces: Iterable[RunTrace]) -> RestorationRate:
    """Every restoration check the branch runs *traces* recorded, and how many passed."""
    ok = total = 0
    reasons: Counter[str] = Counter()
    retargets: dict[str, Counter[str]] = {}
    for trace in traces:
        for check in _checks(trace.step_records):
            total += 1
            if check.verdict == "ok":
                ok += 1
            reasons.update(check.reasons)
            for action in check.actions:
                if action.failure is not None:
                    retargets.setdefault(action.failure, Counter())[action.method] += 1
    return RestorationRate(
        ok=ok,
        total=total,
        by_reason=_counts(reasons),
        retarget_failures={name: _counts(retargets[name]) for name in sorted(retargets)},
    )


def components(trace: RunTrace) -> dict[str, Any]:
    """The components the trace says ran: agent, loop, restorer, cleaner, serializer, backend, network source, model route.

    Read from ``TraceConfig.resolved`` (the composed configuration, redacted:
    a free-form value nobody marked trace-safe reads ``<omitted>``) — PRD §12
    item 8, "name what ran".
    """
    resolved = trace.config.resolved
    agent = (resolved.get("agent") or [{}])[0]
    capability = ((resolved.get("environment") or [{}])[0].get("capabilities") or [{}])[0]
    llm = agent.get("llm") or {}
    return {
        "agent": trace.config.agent_id,
        "loop": trace.config.loop_id,
        "restorer": (agent.get("agent_loop") or {}).get("restorer", {}).get("restorer_id"),
        "cleaner": (capability.get("dom_cleaner") or {}).get("cleaner_type"),
        "serializer": (capability.get("dom_serializer") or {}).get("serializer_type"),
        "backend": (capability.get("backend") or {}).get("provider_id"),
        "network_source": (capability.get("network_source") or {}).get("source_id"),
        "route": f"{llm.get('model_type')}/{(llm.get('provider') or {}).get('name')} "
        f"model={trace.config.llm_model} base_url={llm.get('base_url')}",
    }


def run_tier(config: TraceConfig) -> str | None:
    """The run's replayability tier: ``<capability_id>:<replayability_id>`` per capability that has one."""
    tiers: list[str] = []
    for environment in config.resolved.get("environment") or []:
        for capability in (environment or {}).get("capabilities") or []:
            knob = (capability or {}).get("replayability")
            if isinstance(knob, dict) and knob.get("replayability_id"):
                tiers.append(f"{capability.get('capability_id')}:{knob['replayability_id']}")
    return ",".join(sorted(tiers)) or None


# ── lifted: M29's dedup accounting ────────────────────────────────────


class DedupBytes(_Model):
    """Archive bytes before and after the run's two deduplications (M29's accounting).

    - **Revisit records** (``browser.network``): a payload already written in
      the run is a WARC ``revisit`` record instead of a full response record;
      what it replaced is counted as the stored length of the record it
      points at (``archive_stats.revisit_saving`` — can be negative).
    - **Content addressing** (the WAL, §5.2): a content-addressed file the run
      already stored is written once; every further reference is saved.

    ``before == after + saved_by_revisit + saved_by_content_addressing``.
    """

    before: int
    after: int
    saved_by_revisit: int
    saved_by_content_addressing: int

    @classmethod
    def account(cls, *, listed: int, distinct: int, saved_by_revisit: int) -> "DedupBytes":
        """From the bytes every reference lists, the bytes stored once, and the revisit saving."""
        return cls(
            before=listed + saved_by_revisit,
            after=distinct,
            saved_by_revisit=saved_by_revisit,
            saved_by_content_addressing=listed - distinct,
        )

    def __add__(self, other: "DedupBytes") -> "DedupBytes":
        return DedupBytes(
            before=self.before + other.before,
            after=self.after + other.after,
            saved_by_revisit=self.saved_by_revisit + other.saved_by_revisit,
            saved_by_content_addressing=(
                self.saved_by_content_addressing + other.saved_by_content_addressing
            ),
        )


_NO_BYTES = DedupBytes(before=0, after=0, saved_by_revisit=0, saved_by_content_addressing=0)


def archive_bytes(run: Run) -> DedupBytes:
    """*run*'s ``env/`` bytes before and after deduplication (:class:`DedupBytes`).

    Over the archive files ``RunTrace.committed_files`` lists: after = each
    distinct path once; before = every listing (a content-addressed file once
    per commit that references it) plus what the revisit records saved.

    Raises:
        One of :data:`EVIDENCE_ERRORS`: the evidence does not read back, or a
        revisit points at a response the archive does not hold.
    """
    listed = [file.archive for file in run.trace.committed_files if file.archive is not None]
    sizes = {record.path: record.size for record in listed}
    return DedupBytes.account(
        listed=sum(record.size for record in listed),
        distinct=sum(sizes.values()),
        saved_by_revisit=revisit_saving(line for _step, line in run.index_lines),
    )


# ── §9: archive overhead ──────────────────────────────────────────────


class LatencyGroup(_Model):
    """Per-step environment-side time of one group of runs (policy steps from step 1)."""

    role: str
    runs: int
    steps: int
    tier: str | None
    p50_ms: float | None
    p95_ms: float | None


class ArchiveBytes(_Model):
    """``env/`` bytes of the archived runs, in total and per committed step."""

    runs: int
    steps: int
    total: DedupBytes
    per_step_before: float | None
    per_step_after: float | None
    skipped: dict[str, str] = {}


class ArchiveOverhead(_Model):
    """PRD §9 归档开销: archived (``record``) against control, and the bytes."""

    archived: LatencyGroup
    control: LatencyGroup | None
    p50_overhead_ms: float | None
    p95_overhead_ms: float | None
    reason: str | None = None
    bytes: ArchiveBytes


def env_times(run: Run) -> list[int]:
    """Environment-side milliseconds of each policy step from step 1: ``duration_ms − llm_latency_ms``.

    Step 0 is left out (its duration includes the environment's setup), and
    so is a step whose duration was not measured.
    """
    return [
        record.duration_ms - (record.llm_latency_ms or 0)
        for record in run.steps
        if record.origin == "policy" and record.step >= 1 and record.duration_ms is not None
    ]


def _latency(role: RoleRuns) -> tuple[LatencyGroup, str | None]:
    values = [value for run in role.runs for value in env_times(run)]
    tiers = {run_tier(run.config) for run in role.runs}
    problem: str | None = None
    tier = next(iter(tiers)) if len(tiers) == 1 else None
    if len(tiers) > 1:
        problem = (
            f"the {role.role} runs do not share one replayability tier ({sorted(map(str, tiers))})"
        )
    group = LatencyGroup(
        role=role.role,
        runs=len(role.runs),
        steps=len(values),
        tier=tier,
        p50_ms=percentile(values, 50) if values else None,
        p95_ms=percentile(values, 95) if values else None,
    )
    return group, problem


def archive_overhead(record: RoleRuns, control: RoleRuns) -> ArchiveOverhead:
    """PRD §9 归档开销 (arch §4.16): latency against the control group, and ``env/`` bytes.

    Latency: p50 / p95 of :func:`env_times` in each group, overhead = archived
    minus control; ``null`` with the reason when the control group was not
    run or is empty, a group has no measured step, or the two groups (or the
    runs within one) are not at one tier.
    """
    archived, archived_problem = _latency(record)
    reference: LatencyGroup | None = None
    reason: str | None = archived_problem
    if not control.present or not control.runs:
        reason = reason or "no control group (a recording at the same tier with archive: [])"
    else:
        reference, control_problem = _latency(control)
        reason = reason or control_problem
        if reason is None and reference.tier != archived.tier:
            reason = (
                f"the groups are at different tiers (record {archived.tier}, "
                f"control {reference.tier})"
            )
    if reason is None and (
        archived.p50_ms is None or reference is None or reference.p50_ms is None
    ):
        reason = "a group has no measured policy step after step 0"
    p50 = p95 = None
    if reason is None and reference is not None:
        assert archived.p50_ms is not None and archived.p95_ms is not None
        assert reference.p50_ms is not None and reference.p95_ms is not None
        p50 = archived.p50_ms - reference.p50_ms
        p95 = archived.p95_ms - reference.p95_ms

    total = _NO_BYTES
    steps = 0
    counted = 0
    skipped: dict[str, str] = {}
    for run in record.runs:
        try:
            run_bytes = archive_bytes(run)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its env/ evidence does not account ({type(exc).__name__})"
            continue
        total = total + run_bytes
        steps += len(run.steps)
        counted += 1
    return ArchiveOverhead(
        archived=archived,
        control=reference,
        p50_overhead_ms=p50,
        p95_overhead_ms=p95,
        reason=reason,
        bytes=ArchiveBytes(
            runs=counted,
            steps=steps,
            total=total,
            per_step_before=total.before / steps if steps else None,
            per_step_after=total.after / steps if steps else None,
            skipped=skipped,
        ),
    )


# ── §9: body completeness ─────────────────────────────────────────────


class BodyCompletenessMetric(_Model):
    """PRD §9 body 完整率 over the recordings' ``browser.network`` records."""

    runs: int
    records: int
    by_state: dict[str, int]
    #: Records with a body or an explicit state, over all records.
    complete: Ratio
    #: Records with a body, over those that need one (``redirect`` / ``websocket`` left out).
    with_body: Ratio
    skipped: dict[str, str] = {}


def body_completeness_metric(record: RoleRuns) -> BodyCompletenessMetric:
    """PRD §9 body 完整率 (arch §4.16): every record's final state, by ``archive_stats``."""
    total = BodyCompleteness()
    runs = 0
    skipped: dict[str, str] = {}
    for run in record.runs:
        try:
            total = total + body_completeness(line for _step, line in run.index_lines)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its network index does not read back ({type(exc).__name__})"
            continue
        runs += 1
    explicit = total.with_body + total.no_body_needed + total.explicit_no_body
    return BodyCompletenessMetric(
        runs=runs,
        records=total.records,
        by_state=dict(total.by_state),
        complete=ratio(explicit, total.records, empty="no network record was archived"),
        with_body=ratio(
            total.with_body,
            total.records - total.no_body_needed,
            empty="no archived record needs a body",
        ),
        skipped=skipped,
    )


# ── §9: same-path replay fidelity ─────────────────────────────────────


class FirstFailure(_Model):
    """The first verified step of a run that did not pass, and why."""

    run: str
    step: int | None
    reasons: list[str] = []
    note: str | None = None


class Fidelity(_Model):
    """Same-path replay fidelity of a set of runs."""

    runs: int
    errored_runs: int
    verified_steps: int
    #: The restorer's verdict: checks that were ``ok``.
    restorer: Ratio
    #: URL not mismatched and the screenshot's sha256 equal to the source run's same step.
    strict: Ratio
    #: Runs whose every verified step passed.
    runs_passing: Ratio
    first_failures: list[FirstFailure] = []
    #: Runs left out of the strict measure, and why: a screenshot it compares does not read back.
    skipped: dict[str, str] = {}


class SamePathFidelity(_Model):
    """PRD §9 原路径回放保真, overall and by site."""

    overall: Fidelity
    by_site: dict[str, Fidelity]
    #: Same-path runs whose source run is not under ``traces/record``.
    unmatched: list[str] = []


def _sha256(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


class _UnreadableScreenshot(Exception):
    """A committed screenshot a measure compares does not read back; the message is the skip reason."""


def _screenshot_sha256(run: Run, step: int | None, *, whose: str = "its ") -> str | None:
    """sha256 of *run*'s screenshot at *step*; ``None`` without a step, a commit or a screenshot.

    Raises:
        _UnreadableScreenshot: The committed screenshot does not read back
            (missing, empty, outside the run): the reason names the step and
            the error class, never a path.
    """
    if step is None:
        return None
    try:
        return _sha256(run.trace.screenshot(step))
    except EVIDENCE_ERRORS as exc:
        raise _UnreadableScreenshot(
            f"{whose}step {step} screenshot does not read back ({type(exc).__name__})"
        ) from exc


@dataclass
class _FidelityTally:
    runs: int = 0
    errored: int = 0
    verified: int = 0
    ok: int = 0
    strict_ok: int = 0
    strict_compared: int = 0
    passing: int = 0
    failures: list[FirstFailure] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def result(self) -> Fidelity:
        return Fidelity(
            runs=self.runs,
            errored_runs=self.errored,
            verified_steps=self.verified,
            restorer=ratio(self.ok, self.verified, empty="no verified step"),
            strict=ratio(
                self.strict_ok, self.strict_compared, empty="no step with both screenshots"
            ),
            runs_passing=ratio(self.passing, self.runs, empty="no same-path run"),
            first_failures=sorted(self.failures, key=lambda item: item.run),
            skipped=dict(sorted(self.skipped.items())),
        )


def _strict(run: Run, source: Run, checked: Sequence[StepRecord]) -> tuple[int, int]:
    """The strict measure over *run*'s verified steps: ``(passed, compared)``.

    A step is compared when it and its source run's same step both have a
    screenshot; it passes when no evidence says the URL differs and the two
    sha256 are equal.

    Raises:
        _UnreadableScreenshot: A screenshot it compares does not read back.
    """
    passed = compared = 0
    for record in checked:
        ours = _screenshot_sha256(run, record.step)
        theirs = _screenshot_sha256(source, record.step, whose=f"its source {source.pointer}: ")
        if ours is None or theirs is None:
            continue
        compared += 1
        check = record.restoration
        url_differs = check is not None and any(e.url_match is False for e in check.evidence)
        if not url_differs and ours == theirs:
            passed += 1
    return passed, compared


def same_path_fidelity(chain: ChainRuns) -> SamePathFidelity:
    """PRD §9 原路径回放保真 (arch §4.16), from the ``same_path`` role.

    Every verified step (each restored step and the checkpoint step, each
    with a ``restoration`` check): the restorer's verdict, and the strict
    measure — no evidence says the URL differs, and the screenshot's sha256
    equals the source run's at the same step (the source resolved by
    ``source_trace`` under ``traces/record``). Every run counts, a failed or
    errored one included; a run with no verified step does not pass. A run
    one of whose compared screenshots (or its source's) does not read back is
    left out of the strict measure only, with the reason in ``skipped``: the
    restorer's verdicts are in its step records.
    """
    overall = _FidelityTally()
    by_site: dict[str, _FidelityTally] = {}
    unmatched: list[str] = []
    for run in chain.role("same_path").runs:
        source = chain.source_of(run)
        if source is None:
            unmatched.append(run.pointer)
        tallies = (overall, by_site.setdefault(run.site, _FidelityTally()))
        errored = run.trace.manifest.status is not RunStatus.COMPLETED
        checked = [record for record in run.steps if record.restoration is not None]
        first: FirstFailure | None = None
        verified = ok = strict_ok = strict_compared = 0
        for record in checked:
            check = record.restoration
            assert check is not None
            verified += 1
            if check.verdict == "ok":
                ok += 1
            elif first is None:
                first = FirstFailure(run=run.pointer, step=record.step, reasons=list(check.reasons))
        if not checked:
            first = FirstFailure(
                run=run.pointer, step=None, note="no verified step (the run ended before it)"
            )
        unreadable: str | None = None
        if source is not None:
            try:
                strict_ok, strict_compared = _strict(run, source, checked)
            except _UnreadableScreenshot as exc:
                unreadable = f"{exc}: left out of the strict measure"
        for tally in tallies:
            tally.runs += 1
            tally.errored += int(errored)
            tally.verified += verified
            tally.ok += ok
            tally.strict_ok += strict_ok
            tally.strict_compared += strict_compared
            if unreadable is not None:
                tally.skipped[run.rel] = unreadable
            if first is None:
                tally.passing += 1
            else:
                tally.failures.append(first)
    return SamePathFidelity(
        overall=overall.result(),
        by_site={site: by_site[site].result() for site in sorted(by_site)},
        unmatched=sorted(unmatched),
    )


# ── §9: restoration verification rate ─────────────────────────────────


def restoration(branch: RoleRuns) -> RestorationRate:
    """PRD §9 恢复校验率 (arch §4.16), over the ``branch`` role."""
    return restoration_rate(run.trace for run in branch.runs)


# ── §9: branch outcome coverage ───────────────────────────────────────


class MissDistribution(_Model):
    """Replay misses, counted (folded ones by how many they fold) and broken down."""

    total: int = 0
    by_action: dict[str, int] = {}
    by_resource_type: dict[str, int] = {}
    by_host: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_class: dict[str, int] = {}


@dataclass
class _MissTally:
    total: int = 0
    action: Counter[str] = field(default_factory=Counter)
    resource_type: Counter[str] = field(default_factory=Counter)
    host: Counter[str] = field(default_factory=Counter)
    reason: Counter[str] = field(default_factory=Counter)
    request_class: Counter[str] = field(default_factory=Counter)

    def add(self, run: Run, step: int | None, entry: ReplayLogEntry, weight: int) -> None:
        self.total += weight
        self.action[run.actions_at(step)] += weight
        self.resource_type[entry.resource_type or "(unknown)"] += weight
        self.host[urlsplit(entry.url).hostname or "(none)"] += weight
        self.reason[entry.reason or "(none)"] += weight
        self.request_class[entry.request_class or "(none)"] += weight

    def result(self) -> MissDistribution:
        return MissDistribution(
            total=self.total,
            by_action=_counts(self.action),
            by_resource_type=_counts(self.resource_type),
            by_host=_counts(self.host),
            by_reason=_counts(self.reason),
            by_class=_counts(self.request_class),
        )


def after_checkpoint(run: Run) -> list[tuple[int | None, ReplayLogEntry, int]]:
    """*run*'s replay decisions from its checkpoint step on, the close-out included."""
    checkpoint = run.checkpoint or 0
    return [
        (step, entry, weight)
        for step, entry, weight in run.decisions
        if step is None or step >= checkpoint
    ]


class BranchCoverage(_Model):
    """PRD §9 分叉结果覆盖: from each branch's checkpoint on."""

    branches: int
    #: Branches with no main-frame document miss at or after the checkpoint.
    zero_document_miss: Ratio
    document_misses: int
    #: Every other miss in the same range (sub-frame documents, beacons, subresources).
    other_misses: MissDistribution
    skipped: dict[str, str] = {}


def branch_coverage(branch: RoleRuns) -> BranchCoverage:
    """PRD §9 分叉结果覆盖 (arch §4.16), over the ``branch`` role's replay logs.

    A main-frame document miss is a ``diverged`` decision (the replay's own
    definition). Misses before the checkpoint belong to restoration (its
    ``document_miss`` reason), not here.
    """
    clean = branches = documents = 0
    others = _MissTally()
    skipped: dict[str, str] = {}
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its replay log does not read back ({type(exc).__name__})"
            continue
        branches += 1
        diverged = sum(weight for _step, entry, weight in decisions if entry.diverged)
        documents += diverged
        clean += int(diverged == 0)
        for step, entry, weight in decisions:
            if entry.kind == "miss" and not entry.diverged:
                others.add(run, step, entry, weight)
    return BranchCoverage(
        branches=branches,
        zero_document_miss=ratio(clean, branches, empty="no branch run"),
        document_misses=documents,
        other_misses=others.result(),
        skipped=skipped,
    )


# ── §9: replay determinism ────────────────────────────────────────────


class Determinism(_Model):
    """Replay determinism of one role's runs."""

    #: Runs this measure compared (the ones in ``skipped`` are not).
    runs: int
    #: Groups of the same input (source run, checkpoint, policy actions) with two runs or more.
    groups: int
    #: Runs alone in their input group: nothing to compare them with.
    unpaired: int
    #: Runs without a terminal screenshot (not compared).
    without_terminal: int
    #: Pairs within a group whose terminal screenshots' sha256 are equal, of the comparable pairs.
    identical: Ratio
    #: Checkpoint groups (same source run and checkpoint) whose checkpoint-step screenshots are all equal.
    checkpoint_identical: Ratio
    #: Runs left out, and why: a screenshot this measure compares does not read back.
    skipped: dict[str, str] = {}


class ReplayDeterminism(_Model):
    """PRD §9 回放确定性, by role (``same_path`` and ``branch``)."""

    by_role: dict[str, Determinism]


def _policy_actions(run: Run) -> str:
    return canonical_json(
        [
            [action.name, action.params]
            for record in run.steps
            if record.origin == "policy"
            for action in record.actions
        ]
    )


def _determinism(role: RoleRuns) -> Determinism:
    # Each run's terminal and checkpoint-step screenshot digests, by input and by checkpoint.
    terminals: dict[tuple[str, int, str], list[str | None]] = {}
    checkpoints: dict[tuple[str, int], list[str | None]] = {}
    skipped: dict[str, str] = {}
    for run in role.runs:
        branch = run.config.branch
        if branch is None:
            continue
        terminal = next((r.step for r in run.steps if r.origin == "terminal"), None)
        try:
            final = _screenshot_sha256(run, terminal)
            restored = _screenshot_sha256(run, branch.checkpoint_step)
        except _UnreadableScreenshot as exc:
            skipped[run.rel] = str(exc)
            continue
        terminals.setdefault(
            (branch.source_trace, branch.checkpoint_step, _policy_actions(run)), []
        ).append(final)
        checkpoints.setdefault((branch.source_trace, branch.checkpoint_step), []).append(restored)
    pairs = identical = without = unpaired = groups = 0
    for digests in terminals.values():
        if len(digests) < 2:
            unpaired += len(digests)
            continue
        groups += 1
        known = [digest for digest in digests if digest is not None]
        without += len(digests) - len(known)
        for i, first in enumerate(known):
            for second in known[i + 1 :]:
                pairs += 1
                identical += int(first == second)
    same_checkpoint = compared = 0
    for digests in checkpoints.values():
        known = [digest for digest in digests if digest is not None]
        if len(known) < 2:
            continue
        compared += 1
        same_checkpoint += int(len(set(known)) == 1)
    return Determinism(
        runs=len(role.runs) - len(skipped),
        groups=groups,
        unpaired=unpaired,
        without_terminal=without,
        identical=ratio(
            identical, pairs, empty="no two runs of one input with terminal screenshots"
        ),
        checkpoint_identical=ratio(
            same_checkpoint, compared, empty="no checkpoint with two branches' screenshots"
        ),
        skipped=dict(sorted(skipped.items())),
    )


def replay_determinism(chain: ChainRuns) -> ReplayDeterminism:
    """PRD §9 回放确定性 (arch §4.16): terminal and checkpoint screenshots within one input.

    A run one of whose compared screenshots does not read back is left out of
    its role's comparisons, with the reason in ``skipped`` (arch §4.16:
    damaged evidence is skipped only in the measures that read it).
    """
    return ReplayDeterminism(
        by_role={role: _determinism(chain.role(role)) for role in ("same_path", "branch")}
    )


# ── §9: export determinism ────────────────────────────────────────────


class TreeDigest(_Model):
    """A directory tree's digest: every file's relative path and sha256, by path."""

    digest: str
    files: int


def tree_digest(root: Path) -> TreeDigest:
    """sha256 over ``<relative path>\\0<file sha256>\\n`` of every file under *root*, sorted by path."""
    digest = hashlib.sha256()
    files = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
        files += 1
    return TreeDigest(digest=digest.hexdigest(), files=files)


class ExportDeterminism(_Model):
    """PRD §9 导出确定性: the two exports' tree digests."""

    first: TreeDigest | None
    second: TreeDigest | None
    identical: bool | None
    reason: str | None = None


def export_determinism(layout: ChainLayout) -> ExportDeterminism:
    """PRD §9 导出确定性 (arch §4.16): ``datasets/first`` and ``datasets/second`` digest alike.

    ``null`` with the reason when an export is missing (``repeat: 1`` writes
    only the first).
    """
    first_dir, second_dir = layout.dataset("first"), layout.dataset("second")
    first = tree_digest(first_dir) if first_dir.is_dir() else None
    second = tree_digest(second_dir) if second_dir.is_dir() else None
    if first is None:
        return ExportDeterminism(
            first=None, second=second, identical=None, reason="no first export"
        )
    if second is None:
        return ExportDeterminism(
            first=first,
            second=None,
            identical=None,
            reason="no second export (repeat: 1 does not measure it)",
        )
    return ExportDeterminism(first=first, second=second, identical=first == second)


# ── §9: privacy ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Planted:
    """A credential planted in a chain's inputs, by label.

    ``value`` is ``None`` when it was to come from an environment variable
    that is not set (``source`` names the variable, never the value).
    """

    label: str
    value: str | None
    source: str


class PlantedCount(_Model):
    """How often one planted credential appears; ``null`` when its value was not available."""

    label: str
    source: str
    occurrences: int | None
    files: int | None
    reason: str | None = None


class Privacy(_Model):
    """PRD §9 隐私: planted credentials and credential shapes under ``traces/`` and ``datasets/``."""

    files_scanned: int
    planted: list[PlantedCount]
    shapes: dict[str, int]
    shape_files: dict[str, int]


def privacy(layout: ChainLayout, planted: Sequence[Planted]) -> Privacy:
    """PRD §9 隐私 (arch §4.16): every file under ``traces/`` and ``datasets/``, gzip peeled.

    Walks the trees, not the manifests (``iter_readable``): a leak in a file
    nothing indexes is still a leak. Counts each planted value's occurrences
    and the files holding it, and each ``SECRET_PATTERNS`` shape's hits —
    labels and counts only, never a value.
    """
    occurrences: Counter[str] = Counter()
    holding: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    shape_files: Counter[str] = Counter()
    scanned = 0
    for tree in (layout.traces_dir, layout.datasets_dir):
        for _path, text in iter_readable(tree):
            scanned += 1
            for item in planted:
                if item.value:
                    found = text.count(item.value)
                    occurrences[item.label] += found
                    holding[item.label] += int(found > 0)
            labels = Counter(label for label, _match in find_secrets(text))
            shapes.update(labels)
            shape_files.update(set(labels))
    counts = [
        PlantedCount(
            label=item.label,
            source=item.source,
            occurrences=occurrences[item.label] if item.value else None,
            files=holding[item.label] if item.value else None,
            reason=None if item.value else f"{item.source} is not set: nothing to search for",
        )
        for item in planted
    ]
    return Privacy(
        files_scanned=scanned,
        planted=counts,
        shapes=_counts(shapes),
        shape_files=_counts(shape_files),
    )


# ── §10 / §11 sections ────────────────────────────────────────────────


class ChallengeHit(_Model):
    """One recorded document an anti-bot challenge answered."""

    site: str
    run: str
    step: int | None
    status: int
    url: str
    signals: list[str]


class AntiBot(_Model):
    """PRD §10 anti-bot / Cloudflare: sites, runs and steps whose documents carried challenge marks."""

    runs_checked: int
    hits: list[ChallengeHit]
    blocked_sites: list[str]
    skipped: dict[str, str] = {}


def anti_bot(record: RoleRuns) -> AntiBot:
    """PRD §10 anti-bot (arch §4.16): ``archive_stats.challenge_documents`` over the recordings."""
    hits: list[ChallengeHit] = []
    checked = 0
    skipped: dict[str, str] = {}
    for run in record.runs:
        try:
            found = challenge_documents(run.trace)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its network archive does not load ({type(exc).__name__})"
            continue
        checked += 1
        hits.extend(
            ChallengeHit(
                site=run.site,
                run=run.pointer,
                step=document.step,
                status=document.status,
                url=_url_path(document.url),
                signals=list(document.signals),
            )
            for document in found
        )
    return AntiBot(
        runs_checked=checked,
        hits=hits,
        blocked_sites=sorted({hit.site for hit in hits}),
        skipped=skipped,
    )


class SiteStats(_Model):
    """One site's numbers across the chain (§10 "按站点统计")."""

    tasks: int
    record_runs: int
    record_status: dict[str, int]
    record_outcome: dict[str, int]
    #: Recordings whose environment failed at step 0 (or that committed no step).
    record_failed_at_step0: int
    #: Recordings that ended on a failed model call — the route's failure, never an exclusion reason.
    record_model_failures: int
    blocked: bool
    same_path: Ratio
    restoration: Ratio
    branch_zero_document_miss: Ratio
    document_misses: int
    other_misses: int


class SiteExclusion(_Model):
    site: str
    reasons: list[str]


class Sites(_Model):
    """PRD §10 per-site statistics and the exclusion list, in ``ExportFilters.exclude_sites`` shape."""

    by_site: dict[str, SiteStats]
    exclusions: list[SiteExclusion]
    exclude_sites: list[str]
    #: The exclusion list as an export ``filters`` fragment, ready to paste.
    yaml: str


def failed_on_model_call(run: Run) -> bool:
    """Whether a step of *run* failed on its model call (``error_phase`` :data:`MODEL_CALL_PHASE`).

    The model route's failure — a 429, a transport timeout, a reply the
    provider refused — not the environment's:
    the loop ends such a run with ``env_status: success``. Never evidence
    about the site.
    """
    return any(record.error_phase == MODEL_CALL_PHASE for record in run.steps)


def _failed_at_step0(run: Run) -> bool:
    """Whether *run*'s environment failed at step 0: no step committed, or an environment phase failed."""
    first = run.step(0)
    return first is None or first.error_phase in ENVIRONMENT_PHASES


def sites(
    chain: ChainRuns, *, fidelity: SamePathFidelity, blocked: Sequence[str], exclude_below: float
) -> Sites:
    """PRD §10 by site (arch §4.16): counts, outcomes, fidelity, restoration, coverage, misses.

    Excluded: a site whose recordings met a challenge (*blocked*), whose
    same-path fidelity (the restorer's) is below *exclude_below*, or one of
    whose recordings' environment failed at step 0 (an observation or an
    action failed, or no step committed). A recording that failed on its
    model call is counted apart (``record_model_failures``) and excludes
    nothing.
    """
    record = chain.role("record")
    names = sorted(
        {run.site for run in record.runs}
        | {run.site for role in ("same_path", "branch") for run in chain.role(role).runs}
    )
    by_site: dict[str, SiteStats] = {}
    exclusions: list[SiteExclusion] = []
    for site in names:
        recorded = [run for run in record.runs if run.site == site]
        statuses = Counter(run.trace.manifest.status.value for run in recorded)
        outcomes: Counter[str] = Counter()
        for run in recorded:
            try:
                result = run.trace.result
            except TraceSchemaError:
                result = None
            outcomes[str(result.agent_done) if result is not None else "(no result)"] += 1
        failed0 = sum(1 for run in recorded if _failed_at_step0(run))
        branches = [run for run in chain.role("branch").runs if run.site == site]
        rate = restoration_rate(run.trace for run in branches)
        clean = counted = documents = others = 0
        for role in ("same_path", "branch"):
            for run in chain.role(role).runs:
                if run.site != site:
                    continue
                try:
                    decisions = run.decisions
                except EVIDENCE_ERRORS:
                    continue
                documents += sum(w for _s, e, w in decisions if e.diverged)
                others += sum(w for _s, e, w in decisions if e.kind == "miss" and not e.diverged)
                if role == "branch":
                    counted += 1
                    clean += int(not any(e.diverged for _s, e, _w in after_checkpoint(run)))
        site_fidelity = fidelity.by_site.get(site)
        same_path = (
            site_fidelity.restorer
            if site_fidelity is not None
            else ratio(0, 0, empty="no same-path run of this site")
        )
        stats = SiteStats(
            tasks=len({run.task_key for run in recorded}),
            record_runs=len(recorded),
            record_status=_counts(statuses),
            record_outcome=_counts(outcomes),
            record_failed_at_step0=failed0,
            record_model_failures=sum(1 for run in recorded if failed_on_model_call(run)),
            blocked=site in blocked,
            same_path=same_path,
            restoration=ratio(rate.ok, rate.total, empty="no restoration check on this site"),
            branch_zero_document_miss=ratio(clean, counted, empty="no branch run of this site"),
            document_misses=documents,
            other_misses=others,
        )
        by_site[site] = stats
        reasons: list[str] = []
        if stats.blocked:
            reasons.append("an anti-bot challenge answered its recording")
        if same_path.value is not None and same_path.value < exclude_below:
            reasons.append(f"same-path fidelity {same_path.value:.2f} < {exclude_below}")
        if failed0:
            reasons.append(f"{failed0} recording(s) failed at step 0")
        if reasons and site != NO_SITE:
            exclusions.append(SiteExclusion(site=site, reasons=reasons))
    excluded = [item.site for item in exclusions]
    snippet = "filters:\n  exclude_sites: [" + ", ".join(excluded) + "]\n"
    return Sites(by_site=by_site, exclusions=exclusions, exclude_sites=excluded, yaml=snippet)


class DuplicateBranches(_Model):
    """PRD §10 重复分叉率: branches whose checkpoint-step actions repeat an earlier branch of the group."""

    branches: int
    groups: int
    duplicates: int
    rate: Ratio
    #: The first export's ``duplicates`` (its samples flagged ``duplicate_of``), when it exists.
    dataset_duplicates: int | None


def duplicate_branches(
    branch: RoleRuns, dataset_manifest: Mapping[str, Any] | None
) -> DuplicateBranches:
    """PRD §10 重复分叉率 (arch §4.16): the export's ``duplicate_of`` rule, on executed actions.

    Within one ``branch_group_id``, in ``branch_index`` order, a branch whose
    checkpoint step ran the same actions (canonical JSON of names and
    params) as an earlier one is a duplicate.
    """
    groups: dict[str, list[Run]] = {}
    for run in branch.runs:
        provenance = run.config.branch
        if provenance is not None:
            groups.setdefault(provenance.branch_group_id, []).append(run)
    duplicates = branches = 0
    for members in groups.values():
        seen: set[str] = set()
        for run in sorted(
            members, key=lambda r: r.config.branch.branch_index if r.config.branch else 0
        ):
            branches += 1
            record = run.step(run.checkpoint or 0)
            target = canonical_json(
                [[a.name, a.params] for a in record.actions] if record is not None else []
            )
            if target in seen:
                duplicates += 1
            seen.add(target)
    flagged = dataset_manifest.get("duplicates") if dataset_manifest is not None else None
    return DuplicateBranches(
        branches=branches,
        groups=len(groups),
        duplicates=duplicates,
        rate=ratio(duplicates, branches, empty="no branch run"),
        dataset_duplicates=flagged if isinstance(flagged, int) else None,
    )


class PostBranchMisses(_Model):
    """PRD §10 POST 分叉的 miss: POST misses from each branch's checkpoint on."""

    branches: int
    document_misses: int
    other_misses: int
    #: Branches with at least one POST miss.
    branches_with_post_miss: Ratio


def post_branch_misses(branch: RoleRuns) -> PostBranchMisses:
    """PRD §10 POST 分叉的 miss (arch §4.16): documents and the rest apart."""
    documents = others = with_miss = branches = 0
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
        except EVIDENCE_ERRORS:
            continue
        branches += 1
        posts = [
            (e, w) for _s, e, w in decisions if e.kind == "miss" and e.method.upper() == "POST"
        ]
        documents += sum(w for e, w in posts if e.resource_type == "Document")
        others += sum(w for e, w in posts if e.resource_type != "Document")
        with_miss += int(any(w > 0 for _e, w in posts))
    return PostBranchMisses(
        branches=branches,
        document_misses=documents,
        other_misses=others,
        branches_with_post_miss=ratio(with_miss, branches, empty="no branch run"),
    )


class StorageCategory(_Model):
    """One kind of file: its bytes, and a gzip estimate for the part not already compressed."""

    files: int
    bytes: int
    compressible_bytes: int
    gzip_bytes: int
    #: ``gzip_bytes / compressible_bytes`` (gzip level 6); ``null`` when nothing is compressible.
    ratio: float | None


class RoleStorage(_Model):
    runs: int
    steps: int
    bytes: int
    per_run: float | None
    per_step: float | None
    categories: dict[str, StorageCategory]
    skipped: dict[str, str] = {}


class Storage(_Model):
    """PRD §10 存储增长 and the compression evaluation's input, by role, plus the datasets."""

    by_role: dict[str, RoleStorage]
    datasets: dict[str, int]


def _already_compressed(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in _COMPRESSED_MAGIC)


#: The storage category of each kind of committed file that has one fixed category.
_CATEGORY_OF_KIND: dict[str, str] = {
    "manifest": "run",
    "config": "run",
    "result": "run",
    "step": "steps",
    "extra_index": "steps",
    "llm_call": "messages",
    "output_schema": "messages",
}


def storage_category(file: CommittedFile) -> str:
    """The storage section's category of one committed file (arch §4.16 "存储").

    ``observations:<fragment>`` / ``artifacts:<fragment>``, ``env:run`` (a
    capability's run record), ``env:blobs`` (content-addressed evidence),
    ``env:<archiver>``, ``extra`` (an extra file that is no evidence), and
    ``run`` / ``steps`` / ``messages`` by kind.
    """
    if file.kind == "fragment":
        return f"observations:{file.fragment}"
    if file.kind == "artifact":
        return f"artifacts:{file.fragment}"
    if file.kind == "extra":
        archive = file.archive
        if archive is None:
            return "extra"
        if archive.scope == "run":
            return "env:run"
        return "env:blobs" if archive.content_addressed else f"env:{archive.archiver_id}"
    return _CATEGORY_OF_KIND[file.kind]


def _run_storage(trace: RunTrace) -> dict[str, list[int]]:
    """Per category: files, bytes, compressible bytes, their gzip size — each committed path once.

    Raises:
        One of :data:`EVIDENCE_ERRORS` or ``OSError``: a committed file does
        not read back.
    """
    totals: dict[str, list[int]] = {}
    seen: set[str] = set()
    for file in trace.committed_files:
        if file.path in seen:
            continue
        seen.add(file.path)
        data = trace.read_committed(file.path)
        slot = totals.setdefault(storage_category(file), [0, 0, 0, 0])
        slot[0] += 1
        slot[1] += len(data)
        if not _already_compressed(data):
            slot[2] += len(data)
            slot[3] += len(gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0))
    return totals


def _role_storage(role: RoleRuns) -> RoleStorage:
    totals: dict[str, list[int]] = {}
    steps = runs = 0
    skipped: dict[str, str] = {}
    for run in role.runs:
        try:
            found = _run_storage(run.trace)
        except (*EVIDENCE_ERRORS, OSError) as exc:
            skipped[run.rel] = f"a listed file does not read back ({type(exc).__name__})"
            continue
        runs += 1
        steps += len(run.steps)
        for category, counts in found.items():
            slot = totals.setdefault(category, [0, 0, 0, 0])
            for index, value in enumerate(counts):
                slot[index] += value
    total = sum(slot[1] for slot in totals.values())
    return RoleStorage(
        runs=runs,
        steps=steps,
        bytes=total,
        per_run=total / runs if runs else None,
        per_step=total / steps if steps else None,
        categories={
            name: StorageCategory(
                files=slot[0],
                bytes=slot[1],
                compressible_bytes=slot[2],
                gzip_bytes=slot[3],
                ratio=slot[3] / slot[2] if slot[2] else None,
            )
            for name, slot in sorted(totals.items())
        },
        skipped=skipped,
    )


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def storage(chain: ChainRuns) -> Storage:
    """PRD §10 存储 (arch §4.16): bytes by category per role, per run and per step; gzip ratios.

    A run's files are the ones ``RunTrace.committed_files`` lists (never a
    walk of the directory), each path once, read through ``read_committed``
    (:func:`storage_category` files them); bytes already compressed —
    screenshots, the WARC, gzip captures, by their leading bytes — are
    reported as size only.
    """
    return Storage(
        by_role={
            role: _role_storage(chain.role(role)) for role in ROLES if chain.role(role).present
        },
        datasets={
            name: _tree_bytes(chain.layout.dataset(name))
            for name in DATASETS
            if chain.layout.dataset(name).is_dir()
        },
    )


class Projection(_Model):
    tasks: int
    bytes: float


class Retention(_Model):
    """PRD §11 Q7: bytes per run of each role and per dataset sample, and a projection."""

    per_run: dict[str, float | None]
    per_sample: float | None
    #: Of each role, runs per recorded task (the chain's own ratio).
    runs_per_task: dict[str, float | None]
    per_task: float | None
    projections: list[Projection]


def retention(
    chain: ChainRuns, stored: Storage, samples: int | None, tasks: Sequence[int]
) -> Retention:
    """PRD §11 Q7 (arch §4.16): per-run and per-sample bytes, projected to *tasks* tasks.

    Bytes per task = a recording plus, for every other role, its runs per
    recording times its bytes per run; the first dataset's bytes per sample
    times its samples per recording are added.
    """
    record_runs = len(chain.role("record").runs)
    per_run = {role: item.per_run for role, item in stored.by_role.items()}
    first = stored.datasets.get("first")
    per_sample = first / samples if first is not None and samples else None
    runs_per_task = {
        role: (item.runs / record_runs if record_runs else None)
        for role, item in stored.by_role.items()
    }
    per_task: float | None = None
    if record_runs and per_run.get("record") is not None:
        per_task = 0.0
        for role, share in runs_per_task.items():
            value = per_run.get(role)
            if share is not None and value is not None:
                per_task += share * value
        if per_sample is not None and samples:
            per_task += per_sample * samples / record_runs
    return Retention(
        per_run=per_run,
        per_sample=per_sample,
        runs_per_task=runs_per_task,
        per_task=per_task,
        projections=[Projection(tasks=n, bytes=n * per_task) for n in tasks]
        if per_task is not None
        else [],
    )


class ExportYield(_Model):
    """PRD §10 导出产出 from the first dataset: samples, drops, skips, ``action_failed`` share."""

    present: bool
    samples: dict[str, int] = {}
    untranslatable: dict[str, int] = {}
    steps_skipped: dict[str, int] = {}
    sources_exported: int = 0
    sources_skipped: dict[str, str] = {}
    sources_filtered: dict[str, str] = {}
    duplicates: int | None = None
    action_failed: Ratio


def read_dataset_manifest(layout: ChainLayout, name: str = "first") -> dict[str, Any] | None:
    """An export's ``manifest.json`` as JSON (the first by default), or ``None`` when it has none."""
    path = layout.dataset(name) / "manifest.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def export_yield(layout: ChainLayout, manifest: Mapping[str, Any] | None) -> ExportYield:
    """PRD §10 导出产出 (arch §4.16), read from the published files of ``datasets/first``.

    ``action_failed``: samples whose ``labels.step_status`` is
    ``action_failed`` over all samples (``<split>.samples.jsonl``) — the
    keep-or-drop question M32 handed to M33.
    """
    if manifest is None:
        return ExportYield(present=False, action_failed=ratio(0, 0, empty="no first export"))
    root = layout.dataset("first")
    failed = total = 0
    for path in sorted(root.glob("*.samples.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            sample = json.loads(line).get("sample") or {}
            total += 1
            failed += int((sample.get("labels") or {}).get("step_status") == "action_failed")
    split = manifest.get("split") or {}
    steps = manifest.get("steps") or {}
    sources = manifest.get("sources") or {}
    duplicates = manifest.get("duplicates")
    return ExportYield(
        present=True,
        samples={str(k): int(v) for k, v in sorted((split.get("samples") or {}).items())},
        untranslatable={
            str(k): int(v) for k, v in sorted((steps.get("untranslatable") or {}).items())
        },
        steps_skipped={str(k): int(v) for k, v in sorted((steps.get("skipped") or {}).items())},
        sources_exported=len(sources.get("exported") or []),
        sources_skipped={str(k): str(v) for k, v in sorted((sources.get("skipped") or {}).items())},
        sources_filtered={
            str(k): str(v) for k, v in sorted((sources.get("filtered") or {}).items())
        },
        duplicates=duplicates if isinstance(duplicates, int) else None,
        action_failed=ratio(failed, total, empty="the export holds no sample"),
    )


class SiteRuleCandidates(_Model):
    """Replay misses a per-site query rule would have matched (``archive_stats``), by host."""

    runs: int
    by_host: dict[str, int]
    params: dict[str, dict[str, int]]


def site_rules(chain: ChainRuns) -> SiteRuleCandidates:
    """Near misses in every replayed run (``same_path`` and ``branch``), by host and parameter."""
    misses: Counter[str] = Counter()
    params: dict[str, Counter[str]] = {}
    runs = 0
    for role in ("same_path", "branch"):
        for run in chain.role(role).runs:
            try:
                found = site_rule_candidates(run.decisions)
            except EVIDENCE_ERRORS:
                continue
            runs += 1
            for host, candidate in found.items():
                misses[host] += candidate.misses
                params.setdefault(host, Counter()).update(dict(candidate.params))
    return SiteRuleCandidates(
        runs=runs,
        by_host=_counts(misses),
        params={host: _counts(params[host]) for host in sorted(params)},
    )


class BranchDocumentMisses(_Model):
    """Q4's input: the branches' main-frame document misses, and how many were GETs of unrecorded URLs."""

    document_misses: int
    get_no_match: Ratio


def branch_document_misses(branch: RoleRuns) -> BranchDocumentMisses:
    """Of the branches' document misses (from the checkpoint on), the GETs nothing recorded matched."""
    total = unrecorded = 0
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
        except EVIDENCE_ERRORS:
            continue
        for _step, entry, weight in decisions:
            if not entry.diverged:
                continue
            total += weight
            if entry.method.upper() == "GET" and entry.reason == "no_match":
                unrecorded += weight
    return BranchDocumentMisses(
        document_misses=total,
        get_no_match=ratio(unrecorded, total, empty="no document miss in any branch"),
    )


__all__ = [
    "DATASETS",
    "ENVIRONMENT_PHASES",
    "EVIDENCE_ERRORS",
    "MODEL_CALL_PHASE",
    "NO_ACTION",
    "NO_SITE",
    "ROLES",
    "TERMINAL",
    "TRAILING",
    "AntiBot",
    "ArchiveBytes",
    "ArchiveOverhead",
    "BodyCompletenessMetric",
    "BranchCoverage",
    "BranchDocumentMisses",
    "ChainLayout",
    "ChainRuns",
    "ChallengeHit",
    "DedupBytes",
    "Determinism",
    "DuplicateBranches",
    "ExportDeterminism",
    "ExportYield",
    "Fidelity",
    "FirstFailure",
    "LatencyGroup",
    "MissDistribution",
    "Planted",
    "PlantedCount",
    "PostBranchMisses",
    "Privacy",
    "Projection",
    "Ratio",
    "ReplayDeterminism",
    "RestorationRate",
    "Retention",
    "RoleRuns",
    "RoleStorage",
    "Run",
    "SamePathFidelity",
    "SiteExclusion",
    "SiteRuleCandidates",
    "SiteStats",
    "Sites",
    "Storage",
    "StorageCategory",
    "TreeDigest",
    "after_checkpoint",
    "anti_bot",
    "archive_overhead",
    "body_completeness_metric",
    "branch_coverage",
    "branch_document_misses",
    "canonical_json",
    "components",
    "archive_bytes",
    "duplicate_branches",
    "env_times",
    "export_determinism",
    "export_yield",
    "failed_on_model_call",
    "open_chain",
    "open_role",
    "percentile",
    "post_branch_misses",
    "privacy",
    "ratio",
    "read_dataset_manifest",
    "replay_determinism",
    "restoration",
    "restoration_rate",
    "retention",
    "run_tier",
    "same_path_fidelity",
    "site_rules",
    "sites",
    "storage",
    "storage_category",
    "tree_digest",
]
