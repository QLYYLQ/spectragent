"""The flywheel's metrics and sections, from a chain's committed data (``docs/architecture_design.md`` §4.16).

**What is read, and how.** A chain root has a fixed layout
(:class:`ChainLayout`): runs under ``traces/<role>/`` for the roles
:data:`ROLES`, the two exports under ``datasets/{first,second}/``. A role's
runs are opened with :func:`open_role` — matched by ``core/trace/sources.py``
like every other offline consumer, read only through ``RunTrace``'s accessors
(only what was committed exists); a run that does not read back is skipped
with a reason worded as the export words it (``unreadable_reason``), never
fatal. The browser's evidence is read by the browser package's own functions
(``iter_index_lines``, ``iter_decisions``, ``archive_stats``); this module
only organises the calls. Datasets are read as the files an export published,
each through the model the export writes it with (``DatasetManifest``,
``CanonicalStepSample``). Two measures walk the dataset directories on
purpose, because an export's output is its files: privacy's dataset gate and
the dataset digests. Privacy's other gate is what was sent to the model, each
run's recorded requests read through ``RunTrace``; ``env/`` and the rest of a
trace stay on the machine that recorded them and are not searched (arch §5.6).

**Every number has its denominator.** A ratio over nothing is ``null`` with
the reason, never ``0`` — "not measured" must not read as "measured zero"
(the ``evaluator/summary.py`` rule). A percentile of no values raises. A
measure that skips a run's damaged evidence says so in its ``skipped``, and
every measure a recommendation rests on counts the distinct ``sites`` its data
came from: the report's breadth rule reads it.

**Empty branches.** A branch run that executed no action from its checkpoint
on (:func:`empty_branch`: its teacher call failed, its reply ran no action,
or the run ended before the teacher decided) says nothing about where the
teacher's choices lead. The branch-outcome measures leave it out and count
it apart.

**Token use.** :func:`token_use` / :func:`role_token_use` add up the tokens
the runs' model calls recorded (``core/trace/usage.py::recorded_usage``,
the count ``spectragent bench``'s token limit uses), naming every call whose
provider reported no usage: the chain's token limit and the report's
``token_use`` read them.

**Lifted, not copied** (§4.16 "共用而不是复制"): :class:`RestorationRate` /
:func:`restoration_rate` and :func:`components` were the branch live test's
helpers; :class:`DedupBytes` and :func:`percentile` were the archive overhead
benchmark's (its revisit half is the browser's
``archive_stats.revisit_saving``); the dataset digest is
``core/trace/digest.py``'s, the one the tests use to prove a reader left a run
untouched.
"""

import gzip
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, computed_field

from spectragent.core.capabilities.browser.archive_stats import (
    BodyCompleteness,
    NearMiss,
    body_completeness,
    challenge_documents,
    near_misses,
    revisit_saving,
    site_rule_candidates,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    IndexEntry,
    RecordedArchive,
    ReplayArchiveError,
    iter_index_lines,
    load_archive,
)
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLogEntry,
    iter_decisions,
)
from spectragent.core.capabilities.browser.replay.rule_snapshot import recorded_site_rules
from spectragent.core.capabilities.browser.settle import PageSettle, recorded_settle
from spectragent.core.trace.digest import TreeDigest, tree_digest
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.models import (
    CommittedFile,
    RecordedLLMCall,
    TraceConfig,
    TraceStatus,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.secret_patterns import find_secrets, iter_readable
from spectragent.core.trace.sources import match_runs, task_identity, unreadable_reason
from spectragent.core.trace.usage import recorded_calls, recorded_usage
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.dataset import DATASET_FORMAT, DatasetManifest
from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.sample import CanonicalStepSample
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: The run roles of a chain, in stage order (arch §4.16 "布局").
ROLES: tuple[str, ...] = ("record", "control", "same_path", "branch")
#: The two exports of a chain; export determinism compares them.
DATASETS: tuple[str, ...] = ("first", "second")
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
#: How a recording's agent ended when it wrote no ``result.json`` (the run died first).
NO_RESULT = "(no result)"
#: How a recording's agent ended when its ``result.json`` does not read back.
UNREADABLE_RESULT = "(unreadable result)"
#: Why a branch is empty (:func:`empty_branch`): a policy step's model call failed, and no
#: policy step ran an action.
EMPTY_MODEL_CALL_FAILED = "model_call_failed"
#: Why a branch is empty: the teacher's replies ran no action — a reply that did not parse
#: and a reply that named none are recorded alike (no action, no error phase).
EMPTY_NO_ACTION = "no_action"
#: Why a branch is empty: the run ended before the teacher decided — its restoration
#: ended it, or the checkpoint step's observation or prompt failed.
EMPTY_ENDED_BEFORE_DECISION = "ended_before_decision"
#: What reading a run's evidence can raise when the evidence is damaged.
EVIDENCE_ERRORS: tuple[type[Exception], ...] = (
    TraceSchemaError,
    ReplayArchiveError,
    ValueError,
    KeyError,
)
#: What reading a run's ``result.json`` can raise: it does not parse as a result, or it
#: cannot be read at all (permissions, I/O). Either way the agent's ending is unknown.
RESULT_ERRORS: tuple[type[Exception], ...] = (TraceSchemaError, OSError)
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
        if not self.config.recording.trajectory:
            return "(actions not recorded)"
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
    the file, relative to the run, and the error class) — a trace of an
    older format among them. Never raises for a damaged run.
    """
    if not directory.is_dir():
        return RoleRuns(role=role, present=False)
    runs: list[Run] = []
    skipped: dict[str, str] = {}
    for rel, run_dir in match_runs(directory, ["*"]):
        try:
            trace = RunTrace(run_dir)
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
    """The *q*-th percentile (0–100), nearest-rank on a sorted copy (the overhead benchmark's definition).

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


def site_count(runs: Iterable[Run]) -> int:
    """The distinct sites of *runs* — :data:`NO_SITE` is no site."""
    return len({run.site for run in runs} - {NO_SITE})


def agent_outcome(run: Run) -> str:
    """How *run*'s agent ended (``agent_done``), :data:`NO_RESULT` or :data:`UNREADABLE_RESULT`."""
    try:
        result = run.trace.result
    except RESULT_ERRORS:
        return UNREADABLE_RESULT
    return str(result.agent_done) if result is not None else NO_RESULT


def empty_branch(run: Run) -> str | None:
    """Why the branch run *run* executed no action from its checkpoint on, or ``None`` when it did.

    Its policy steps from the checkpoint on (the checkpoint step and any
    continue step) are read: one that ran an action makes the branch
    non-empty. Otherwise the reason is :data:`EMPTY_MODEL_CALL_FAILED` when a
    model call failed (``error_phase`` :data:`MODEL_CALL_PHASE`),
    :data:`EMPTY_NO_ACTION` when every such step ended without an error (the
    teacher's reply did not parse or named no action), and
    :data:`EMPTY_ENDED_BEFORE_DECISION` when there is no such step or it
    failed before the model was asked (restoration, observation, prompt).
    """
    run.trace.require_recording("trajectory", "errors", consumer="branch action analysis")
    checkpoint = run.checkpoint or 0
    policy = [r for r in run.steps if r.origin == "policy" and r.step >= checkpoint]
    if any(record.actions for record in policy):
        return None
    if any(record.error_phase == MODEL_CALL_PHASE for record in policy):
        return EMPTY_MODEL_CALL_FAILED
    if policy and all(not record.error_phase for record in policy):
        return EMPTY_NO_ACTION
    return EMPTY_ENDED_BEFORE_DECISION


# ── lifted: the restoration rate and the components that ran ──────────


class RestorationRate(_Model):
    """The restoration verification rate (恢复校验率) over a set of branch runs.

    ``ok`` of ``total`` restoration checks passed (one per restored step,
    plus the checkpoint step's). Failed checks by reason — a check with
    several reasons counts for each — and failed retargets by failure and
    method. ``sites``: the distinct sites of the runs that recorded a check.
    """

    ok: int = 0
    total: int = 0
    by_reason: dict[str, int] = {}
    retarget_failures: dict[str, dict[str, int]] = {}
    sites: int = 0

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
    sites: set[str] = set()
    for trace in traces:
        checked = 0
        for check in _checks(trace.step_records):
            checked += 1
            if check.verdict == "ok":
                ok += 1
            reasons.update(check.reasons)
            for action in check.actions:
                if action.failure is not None:
                    retargets.setdefault(action.failure, Counter())[action.method] += 1
        total += checked
        site = task_identity(trace.config).site if checked else None
        if site:
            sites.add(site)
    return RestorationRate(
        ok=ok,
        total=total,
        by_reason=_counts(reasons),
        retarget_failures={name: _counts(retargets[name]) for name in sorted(retargets)},
        sites=len(sites),
    )


def components(trace: RunTrace) -> dict[str, Any]:
    """The components the trace says ran: agent, loop, restorer, cleaner, serializer, backend, network source, model route.

    Read from ``TraceConfig.resolved`` (the composed configuration, redacted:
    a free-form value nobody marked trace-safe reads ``<omitted>``): a
    validation names the configuration it used (arch §4.16 "报告").
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


# ── lifted: the archive's dedup accounting ────────────────────────────


class DedupBytes(_Model):
    """Archive bytes before and after the run's two deduplications (the overhead benchmark's accounting).

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


# ── metric: archive overhead ──────────────────────────────────────────


class LatencyGroup(_Model):
    """Per-step environment-side time of one group of runs (policy steps from step 1)."""

    role: str
    runs: int
    steps: int
    #: Distinct sites of the runs that contributed a measured step.
    sites: int
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


class SettleWait(_Model):
    """How long one role's observations waited for the page to settle (arch §4.14).

    Read from the settle record every browser capture carries
    (``BrowserCaptureFragment.settle``), on every committed step that has
    one — restored and terminal observations included: each is a capture.
    The wait is part of the environment-side time the latency groups
    measure; this says how much of it, and how often the cap ended it.
    """

    role: str
    runs: int
    #: Committed observations whose capture recorded a settle wait.
    observations: int
    #: Distinct sites of the runs that contributed one.
    sites: int
    p50_ms: float | None
    p95_ms: float | None
    #: Observations the cap ended the wait on, over the observations with a record.
    capped: Ratio
    #: Observations whose page state could not be read (captured at once).
    unknown: int
    skipped: dict[str, str] = {}


class ArchiveOverhead(_Model):
    """Archive overhead (归档开销): archived (``record``) against control, and the bytes.

    ``settle_wait``: the observations' settle wait, one entry per role that
    ran, in stage order — part of the time the groups measure, shown apart.
    """

    archived: LatencyGroup
    control: LatencyGroup | None
    p50_overhead_ms: float | None
    p95_overhead_ms: float | None
    reason: str | None = None
    bytes: ArchiveBytes
    settle_wait: list[SettleWait] = []


def observation_settles(run: Run) -> list[PageSettle]:
    """The settle record of every committed observation of *run* that has one, in step order.

    Raises:
        One of :data:`EVIDENCE_ERRORS`: a fragment does not read back.
    """
    records: list[PageSettle] = []
    for entry in run.trace.manifest.steps:
        recorded = [key for key, files in entry.fragments.items() if not files.failed]
        record = recorded_settle(
            lambda key, step=entry.step: run.trace.observation_fragment(step, key), recorded
        )
        if record is not None:
            records.append(record)
    return records


def settle_wait(role: RoleRuns) -> SettleWait:
    """The settle wait of *role*'s observations (arch §4.14): p50 / p95 and the cap's share."""
    waits: list[int] = []
    capped = unknown = 0
    contributing: list[Run] = []
    skipped: dict[str, str] = {}
    for run in role.runs:
        try:
            records = observation_settles(run)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its observations do not read back ({type(exc).__name__})"
            continue
        if records:
            contributing.append(run)
        waits.extend(record.waited_ms for record in records)
        capped += sum(1 for record in records if record.outcome == "capped")
        unknown += sum(1 for record in records if record.outcome == "unknown")
    return SettleWait(
        role=role.role,
        runs=len(role.runs),
        observations=len(waits),
        sites=site_count(contributing),
        p50_ms=percentile(waits, 50) if waits else None,
        p95_ms=percentile(waits, 95) if waits else None,
        capped=ratio(
            capped,
            len(waits),
            empty="no observation recorded a settle wait (the wait off, or recorded before it)",
        ),
        unknown=unknown,
        skipped=dict(sorted(skipped.items())),
    )


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
    measured = {run.rel: env_times(run) for run in role.runs}
    values = [value for run in role.runs for value in measured[run.rel]]
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
        sites=site_count(run for run in role.runs if measured[run.rel]),
        tier=tier,
        p50_ms=percentile(values, 50) if values else None,
        p95_ms=percentile(values, 95) if values else None,
    )
    return group, problem


def archive_overhead(
    record: RoleRuns, control: RoleRuns, *, roles: Sequence[RoleRuns] = ()
) -> ArchiveOverhead:
    """Archive overhead (arch §4.16): latency against the control group, and ``env/`` bytes.

    Latency: p50 / p95 of :func:`env_times` in each group, overhead = archived
    minus control; ``null`` with the reason when the control group was not
    run or is empty, a group has no measured step, or the two groups (or the
    runs within one) are not at one tier. The settle wait (:func:`settle_wait`)
    of each of *roles* that ran rides along.
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
        settle_wait=[settle_wait(role) for role in roles if role.present],
    )


# ── metric: body completeness ─────────────────────────────────────────


class BodyCompletenessMetric(_Model):
    """Body completeness (body 完整率) over the recordings' ``browser.network`` records."""

    runs: int
    #: Distinct sites of the runs that archived at least one record.
    sites: int
    records: int
    by_state: dict[str, int]
    #: Records with a body or an explicit state, over all records.
    complete: Ratio
    #: Records with a body, over those that need one (``redirect`` / ``websocket`` left out).
    with_body: Ratio
    skipped: dict[str, str] = {}


def body_completeness_metric(record: RoleRuns) -> BodyCompletenessMetric:
    """Body completeness (arch §4.16): every record's final state, by ``archive_stats``."""
    total = BodyCompleteness()
    runs = 0
    with_records: list[Run] = []
    skipped: dict[str, str] = {}
    for run in record.runs:
        try:
            found = body_completeness(line for _step, line in run.index_lines)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its network index does not read back ({type(exc).__name__})"
            continue
        total = total + found
        runs += 1
        if found.records:
            with_records.append(run)
    explicit = total.with_body + total.no_body_needed + total.explicit_no_body
    return BodyCompletenessMetric(
        runs=runs,
        sites=site_count(with_records),
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


# ── metric: same-path replay fidelity ─────────────────────────────────


class FirstFailure(_Model):
    """The first verified step of a run that did not pass, and why."""

    run: str
    step: int | None
    reasons: list[str] = []
    note: str | None = None


class Fidelity(_Model):
    """Same-path replay fidelity of a set of runs."""

    runs: int
    #: Runs that did not end cleanly: an incomplete or unclosed trace, or an
    #: agent that ended in error, a timeout or a veto (:func:`ended_unclean`).
    errored_runs: int
    verified_steps: int
    #: The restorer's verdict: checks that were ``ok``.
    restorer: Ratio
    #: URL compared and equal, and the screenshot's sha256 equal to the source run's same step.
    #: The URL is the restorer's own evidence (``url_match``), so the replay's page-URL rules apply.
    strict: Ratio
    #: Screenshot bytes equal, independent of whether URL evidence was collected.
    exact_screenshot: Ratio
    #: Verified steps with an actual URL comparison, not an absent measurement.
    url_coverage: Ratio
    #: Verified steps with an actual element-signature comparison.
    signature_coverage: Ratio
    #: Verified steps with an actual media-state comparison (not an absent one).
    media_coverage: Ratio
    #: Verified steps that failed ``media_mismatch``.
    media_mismatches: int = 0
    #: Media alignment records by outcome, over the verified steps.
    media_alignment: dict[str, int] = {}
    #: Runs with a restoration check at every step from zero through the checkpoint.
    runs_reaching_checkpoint: Ratio
    #: Runs with complete checkpoint coverage and every check passed.
    runs_passing: Ratio
    first_failures: list[FirstFailure] = []
    #: Runs left out of the image equality measures, and why: a screenshot it compares does not read back.
    skipped: dict[str, str] = {}


class SamePathFidelity(_Model):
    """Same-path replay fidelity (原路径回放保真), overall and by site."""

    overall: Fidelity
    by_site: dict[str, Fidelity]
    #: Distinct sites with at least one verified step.
    sites: int
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
    exact_ok: int = 0
    exact_compared: int = 0
    urls: int = 0
    signatures: int = 0
    media: int = 0
    media_mismatches: int = 0
    media_alignment: Counter[str] = field(default_factory=Counter)
    reaching: int = 0
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
                self.strict_ok,
                self.strict_compared,
                empty="no step with both screenshots and URL comparison evidence",
            ),
            exact_screenshot=ratio(
                self.exact_ok, self.exact_compared, empty="no step with both screenshots"
            ),
            url_coverage=ratio(self.urls, self.verified, empty="no verified step"),
            signature_coverage=ratio(self.signatures, self.verified, empty="no verified step"),
            media_coverage=ratio(self.media, self.verified, empty="no verified step"),
            media_mismatches=self.media_mismatches,
            media_alignment=_counts(self.media_alignment),
            runs_reaching_checkpoint=ratio(self.reaching, self.runs, empty="no same-path run"),
            runs_passing=ratio(self.passing, self.runs, empty="no same-path run"),
            first_failures=sorted(self.failures, key=lambda item: item.run),
            skipped=dict(sorted(self.skipped.items())),
        )


def _exact_screenshots(
    run: Run, source: Run, checked: Sequence[StepRecord]
) -> tuple[int, int, int, int]:
    """Screenshot and URL-plus-screenshot equality: two ``(passed, compared)`` pairs.

    A step is compared when it and its source run's same step both have a
    screenshot. The second pair additionally requires URL comparison evidence;
    an absent URL measurement never counts as a match.

    Raises:
        _UnreadableScreenshot: A screenshot it compares does not read back.
    """
    passed = compared = strict_passed = strict_compared = 0
    for record in checked:
        ours = _screenshot_sha256(run, record.step)
        theirs = _screenshot_sha256(source, record.step, whose=f"its source {source.pointer}: ")
        if ours is None or theirs is None:
            continue
        compared += 1
        check = record.restoration
        urls = [e.url_match for e in check.evidence if e.url_match is not None] if check else []
        if ours == theirs:
            passed += 1
        if urls:
            strict_compared += 1
            strict_passed += int(all(urls) and ours == theirs)
    return passed, compared, strict_passed, strict_compared


def same_path_fidelity(chain: ChainRuns) -> SamePathFidelity:
    """Same-path replay fidelity (arch §4.16), from the ``same_path`` role.

    Every verified step (each restored step and the checkpoint step, each
    with a ``restoration`` check): the restorer's verdict, and the strict
    measure — URL evidence says it is equal, and the screenshot's sha256
    equals the source run's at the same step (the source resolved by
    ``source_trace`` under ``traces/record``). Every run counts, one that did
    not end cleanly included (``errored_runs``: :func:`ended_unclean`); a run
    with no verified step does not pass. A run
    one of whose compared screenshots (or its source's) does not read back is
    left out of the image equality measures only, with the reason in ``skipped``: the
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
        errored = ended_unclean(run)
        checked = [record for record in run.steps if record.restoration is not None]
        first: FirstFailure | None = None
        verified = ok = strict_ok = strict_compared = exact_ok = exact_compared = 0
        urls = signatures = media = media_mismatches = 0
        alignment: Counter[str] = Counter()
        for record in checked:
            check = record.restoration
            assert check is not None
            verified += 1
            urls += int(any(e.url_match is not None for e in check.evidence))
            signatures += int(any(e.signature_match is not None for e in check.evidence))
            media += int(
                any(e.media is not None and e.media.comparisons is not None for e in check.evidence)
            )
            media_mismatches += int("media_mismatch" in check.reasons)
            for item in check.evidence:
                if item.media is not None:
                    alignment.update(a.outcome for a in item.media.alignment or [])
            if check.verdict == "ok":
                ok += 1
            elif first is None:
                first = FirstFailure(run=run.pointer, step=record.step, reasons=list(check.reasons))
        if not checked:
            first = FirstFailure(
                run=run.pointer, step=None, note="no verified step (the run ended before it)"
            )
        branch = run.config.branch
        next_unchecked = 0
        if branch is not None:
            for step in sorted({r.step for r in checked if r.step <= branch.checkpoint_step}):
                if step != next_unchecked:
                    break
                next_unchecked += 1
        reaching = branch is not None and next_unchecked > branch.checkpoint_step
        if first is None and not reaching:
            first = FirstFailure(
                run=run.pointer,
                step=next_unchecked if branch is not None else None,
                note="no restoration check at this step before the checkpoint"
                if branch is not None
                else "no checkpoint provenance to establish complete path coverage",
            )
        unreadable: str | None = None
        if source is not None:
            try:
                exact_ok, exact_compared, strict_ok, strict_compared = _exact_screenshots(
                    run, source, checked
                )
            except _UnreadableScreenshot as exc:
                unreadable = f"{exc}: left out of the image equality measures"
        for tally in tallies:
            tally.runs += 1
            tally.errored += int(errored)
            tally.verified += verified
            tally.ok += ok
            tally.strict_ok += strict_ok
            tally.strict_compared += strict_compared
            tally.exact_ok += exact_ok
            tally.exact_compared += exact_compared
            tally.urls += urls
            tally.signatures += signatures
            tally.media += media
            tally.media_mismatches += media_mismatches
            tally.media_alignment.update(alignment)
            tally.reaching += int(reaching)
            if unreadable is not None:
                tally.skipped[run.rel] = unreadable
            if first is None:
                tally.passing += 1
            else:
                tally.failures.append(first)
    return SamePathFidelity(
        overall=overall.result(),
        by_site={site: by_site[site].result() for site in sorted(by_site)},
        sites=sum(1 for site, tally in by_site.items() if site != NO_SITE and tally.verified),
        unmatched=sorted(unmatched),
    )


# ── metric: restoration verification rate ─────────────────────────────


def restoration(branch: RoleRuns) -> RestorationRate:
    """The restoration verification rate (arch §4.16), over the ``branch`` role."""
    return restoration_rate(run.trace for run in branch.runs)


# ── metric: branch outcome coverage ───────────────────────────────────


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


class EmptyBranches(_Model):
    """Branches that executed no action from their checkpoint on, by reason (:func:`empty_branch`)."""

    total: int = 0
    by_reason: dict[str, int] = {}


class BranchCoverage(_Model):
    """Branch outcome coverage (分叉结果覆盖): from each branch's checkpoint on.

    ``branches`` and ``empty`` count every branch whose replay log reads
    back; everything else is over the branches that acted — an empty branch
    did nothing the archive could fail to serve.
    """

    branches: int
    #: Of ``branches``, the ones that executed no action from the checkpoint on.
    empty: EmptyBranches
    #: Distinct sites of the branches that acted.
    sites: int
    #: Branches that acted with no main-frame document miss at or after the checkpoint.
    zero_document_miss: Ratio
    document_misses: int
    #: Every other miss in the same range (sub-frame documents, beacons, subresources).
    other_misses: MissDistribution
    skipped: dict[str, str] = {}


def branch_coverage(branch: RoleRuns) -> BranchCoverage:
    """Branch outcome coverage (arch §4.16), over the ``branch`` role's replay logs.

    A main-frame document miss is a ``diverged`` decision (the replay's own
    definition). Misses before the checkpoint belong to restoration (its
    ``document_miss`` reason), not here. An empty branch (:func:`empty_branch`)
    is counted in ``empty`` and left out of everything else.
    """
    clean = branches = documents = 0
    acted: list[Run] = []
    empty: Counter[str] = Counter()
    others = _MissTally()
    skipped: dict[str, str] = {}
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
            reason = empty_branch(run)
        except RecordingScopeError as exc:
            skipped[run.rel] = str(exc)
            continue
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its replay log does not read back ({type(exc).__name__})"
            continue
        branches += 1
        if reason is not None:
            empty[reason] += 1
            continue
        acted.append(run)
        diverged = sum(weight for _step, entry, weight in decisions if entry.diverged)
        documents += diverged
        clean += int(diverged == 0)
        for step, entry, weight in decisions:
            if entry.kind == "miss" and not entry.diverged:
                others.add(run, step, entry, weight)
    return BranchCoverage(
        branches=branches,
        empty=EmptyBranches(total=sum(empty.values()), by_reason=_counts(empty)),
        sites=site_count(acted),
        zero_document_miss=ratio(
            clean, len(acted), empty="no branch run that acted" if branches else "no branch run"
        ),
        document_misses=documents,
        other_misses=others.result(),
        skipped=skipped,
    )


# ── metric: replay determinism ────────────────────────────────────────


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
    """Replay determinism (回放确定性), by role (``same_path`` and ``branch``)."""

    by_role: dict[str, Determinism]
    #: Distinct sites of the runs in a comparable pair of terminal screenshots, either role.
    sites: int


def _policy_actions(run: Run) -> str:
    return canonical_json(
        [
            [action.name, action.params]
            for record in run.steps
            if record.origin == "policy"
            for action in record.actions
        ]
    )


def _determinism(role: RoleRuns) -> tuple[Determinism, set[str]]:
    """One role's :class:`Determinism`, and the sites of the runs in a comparable terminal pair."""
    # Each run's terminal and checkpoint-step screenshot digests, by input and by checkpoint.
    terminals: dict[tuple[str, int, str], list[tuple[str | None, str]]] = {}
    checkpoints: dict[tuple[str, int], list[str | None]] = {}
    skipped: dict[str, str] = {}
    for run in role.runs:
        branch = run.config.branch
        if branch is None:
            continue
        terminal = next((r.step for r in run.steps if r.origin == "terminal"), None)
        try:
            run.trace.require_recording("trajectory", "observations", consumer="replay determinism")
            final = _screenshot_sha256(run, terminal)
            restored = _screenshot_sha256(run, branch.checkpoint_step)
        except (_UnreadableScreenshot, RecordingScopeError) as exc:
            skipped[run.rel] = str(exc)
            continue
        terminals.setdefault(
            (branch.source_trace, branch.checkpoint_step, _policy_actions(run)), []
        ).append((final, run.site))
        checkpoints.setdefault((branch.source_trace, branch.checkpoint_step), []).append(restored)
    pairs = identical = without = unpaired = groups = 0
    compared: set[str] = set()
    for members in terminals.values():
        if len(members) < 2:
            unpaired += len(members)
            continue
        groups += 1
        known = [(digest, site) for digest, site in members if digest is not None]
        without += len(members) - len(known)
        if len(known) >= 2:
            compared.update(site for _digest, site in known)
        for i, (first, _site) in enumerate(known):
            for second, _other in known[i + 1 :]:
                pairs += 1
                identical += int(first == second)
    same_checkpoint = checkpoints_compared = 0
    for digests in checkpoints.values():
        known_checkpoints = [digest for digest in digests if digest is not None]
        if len(known_checkpoints) < 2:
            continue
        checkpoints_compared += 1
        same_checkpoint += int(len(set(known_checkpoints)) == 1)
    determinism = Determinism(
        runs=len(role.runs) - len(skipped),
        groups=groups,
        unpaired=unpaired,
        without_terminal=without,
        identical=ratio(
            identical, pairs, empty="no two runs of one input with terminal screenshots"
        ),
        checkpoint_identical=ratio(
            same_checkpoint,
            checkpoints_compared,
            empty="no checkpoint with two branches' screenshots",
        ),
        skipped=dict(sorted(skipped.items())),
    )
    return determinism, compared - {NO_SITE}


def replay_determinism(chain: ChainRuns) -> ReplayDeterminism:
    """Replay determinism (arch §4.16): terminal and checkpoint screenshots within one input.

    A run one of whose compared screenshots does not read back is left out of
    its role's comparisons, with the reason in ``skipped`` (arch §4.16:
    damaged evidence is skipped only in the measures that read it).
    """
    by_role: dict[str, Determinism] = {}
    sites: set[str] = set()
    for role in ("same_path", "branch"):
        by_role[role], compared = _determinism(chain.role(role))
        sites |= compared
    return ReplayDeterminism(by_role=by_role, sites=len(sites))


# ── metric: export determinism ────────────────────────────────────────


class ExportDeterminism(_Model):
    """Export determinism (导出确定性): the two exports' tree digests (``core/trace/digest.py``)."""

    first: TreeDigest | None
    second: TreeDigest | None
    identical: bool | None
    reason: str | None = None


def export_determinism(layout: ChainLayout) -> ExportDeterminism:
    """Export determinism (arch §4.16): ``datasets/first`` and ``datasets/second`` digest alike.

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


# ── metric: privacy ───────────────────────────────────────────────────


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
    """How often one planted credential appears at a gate; ``null`` when its value was not available."""

    label: str
    source: str
    occurrences: int | None
    #: The gate's items holding it at least once: model requests, or dataset files.
    items: int | None
    reason: str | None = None


class PrivacyGate(_Model):
    """What the privacy measure read at one gate, and what it found there."""

    #: Items read: recorded model requests, or files of the published datasets.
    items: int
    planted: list[PlantedCount]
    #: ``SECRET_PATTERNS`` matches, by label.
    shapes: dict[str, int]
    #: Items holding at least one match, by label.
    shape_items: dict[str, int]


class Privacy(_Model):
    """Privacy (隐私): planted credentials and credential shapes at the two gates.

    What was sent to the model (every run's recorded requests) and the
    exported datasets (every file under ``datasets/``). ``env/`` and the rest
    of a trace stay on the machine that recorded them (arch §5.6) and are not
    scanned.
    """

    model_requests: PrivacyGate
    datasets: PrivacyGate
    #: Runs whose recorded requests were read, every role.
    runs: int
    #: Distinct sites of those runs.
    sites: int
    #: Runs whose requests could not be read, keyed ``traces/<role>/<run>``: the
    #: trace does not read back, or one of its recorded calls does not.
    skipped: dict[str, str] = {}


#: An image sent inline: a ``data:`` URL with a base64 payload.
_BASE64_DATA_URL = re.compile(r"data:[^,]*;base64,", re.IGNORECASE)


class _GateTally:
    """Planted values and credential shapes over one gate's items."""

    def __init__(self, planted: Sequence[Planted]) -> None:
        self._planted = planted
        self.items = 0
        self._occurrences: Counter[str] = Counter()
        self._holding: Counter[str] = Counter()
        self._shapes: Counter[str] = Counter()
        self._shape_items: Counter[str] = Counter()

    def add(self, texts: Iterable[str]) -> None:
        """One item, as the texts it is made of; each is searched on its own."""
        self.items += 1
        found: Counter[str] = Counter()
        shapes: Counter[str] = Counter()
        for text in texts:
            for item in self._planted:
                if item.value:
                    found[item.label] += text.count(item.value)
            shapes.update(label for label, _match in find_secrets(text))
        self._occurrences.update(found)
        self._holding.update(label for label, count in found.items() if count)
        self._shapes.update(shapes)
        self._shape_items.update(set(shapes))

    def result(self) -> PrivacyGate:
        return PrivacyGate(
            items=self.items,
            planted=[
                PlantedCount(
                    label=item.label,
                    source=item.source,
                    occurrences=self._occurrences[item.label] if item.value else None,
                    items=self._holding[item.label] if item.value else None,
                    reason=None
                    if item.value
                    else f"{item.source} is not set: nothing to search for",
                )
                for item in self._planted
            ],
            shapes=_counts(self._shapes),
            shape_items=_counts(self._shape_items),
        )


def request_texts(call: RecordedLLMCall) -> list[str]:
    """Every text a recorded request sent the model: the strings of its messages.

    An image sent inline (a base64 ``data:`` URL) is left out: it is not text
    a search can read, and no credential shape can occur in base64's
    alphabet. An image sent by URL is text like any other.
    """
    texts: list[str] = []
    stack: list[Any] = (
        [message.model_dump(mode="json") for message in call.messages]
        if call.kind == "chat"
        else [call.request.model_dump(mode="json", by_alias=True, exclude={"assets"})]
    )
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if not _BASE64_DATA_URL.match(value):
                texts.append(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return texts


def privacy(chain: ChainRuns, planted: Sequence[Planted]) -> Privacy:
    """Privacy (arch §4.16): the two gates — what was sent to the model, and the datasets.

    The model requests are every readable run's recorded calls, every role,
    read through ``RunTrace`` (``core/trace/usage.py::recorded_calls``), and searched in
    the texts their messages hold (:func:`request_texts`). The datasets are
    every file under ``datasets/``, gzip peeled (``iter_readable``): an
    export's output is its files. ``env/`` and the rest of a trace are not
    searched: they stay on this machine (arch §5.6). Counts each planted
    value's occurrences and the items holding it, and each
    ``SECRET_PATTERNS`` shape's hits, per gate — labels and counts only,
    never a value. A run whose trace or one of whose calls does not read
    back is skipped whole, with the reason.
    """
    requests = _GateTally(planted)
    datasets = _GateTally(planted)
    read: list[Run] = []
    skipped: dict[str, str] = {}
    for role in ROLES:
        runs = chain.role(role)
        skipped.update({f"traces/{role}/{rel}": reason for rel, reason in runs.skipped.items()})
        for run in runs.runs:
            try:
                calls = [request_texts(call) for call in recorded_calls(run.trace)]
            except RecordingScopeError as exc:
                skipped[run.pointer] = str(exc)
                continue
            except (*EVIDENCE_ERRORS, OSError) as exc:
                skipped[run.pointer] = (
                    f"a recorded model call does not read back ({type(exc).__name__})"
                )
                continue
            read.append(run)
            for texts in calls:
                requests.add(texts)
    for _path, text in iter_readable(chain.layout.datasets_dir):
        datasets.add([text])
    return Privacy(
        model_requests=requests.result(),
        datasets=datasets.result(),
        runs=len(read),
        sites=site_count(read),
        skipped=dict(sorted(skipped.items())),
    )


# ── token use ─────────────────────────────────────────────────────────


class TokenUse(_Model):
    """The tokens some runs' model calls recorded (``core/trace/usage.py``), with every call that reported none.

    ``calls`` counts every recorded call. A call whose provider reported no
    usage adds no tokens and is named in ``calls_without_usage``
    (``traces/<role>/<run>: step S call K (status)``), never read as zero
    without a word. A run whose trace or recorded calls do not read back
    is left out of the counts and named in ``skipped`` with the reason.
    """

    runs: int
    tokens: int
    calls: int
    calls_without_usage: list[str]
    skipped: dict[str, str]
    #: Runs with readable subtotals but incomplete persisted measurement coverage.
    incomplete: dict[str, str] = {}


def token_use(runs: Iterable[Run], skipped: Mapping[str, str] | None = None) -> TokenUse:
    """What *runs* recorded (``recorded_usage``); *skipped* (pointer → reason) are already left out."""
    left_out = dict(skipped or {})
    counted = tokens = calls = 0
    without: list[str] = []
    incomplete: dict[str, str] = {}
    for run in runs:
        try:
            usage = recorded_usage(run.trace)
        except (*EVIDENCE_ERRORS, OSError) as exc:
            left_out[run.pointer] = (
                f"a recorded model call does not read back ({type(exc).__name__})"
            )
            continue
        if usage.tokens is None or usage.calls is None:
            left_out[run.pointer] = "the recording plan did not select metrics"
            continue
        counted += 1
        tokens += usage.tokens
        calls += usage.calls
        if not usage.recording_complete:
            detail = "recording coverage is incomplete; counts are known subtotals"
            if usage.missing_steps:
                detail += f" (missing steps: {', '.join(map(str, usage.missing_steps))})"
            incomplete[run.pointer] = detail
        without += [f"{run.pointer}: {call.describe()}" for call in usage.without_usage]
    return TokenUse(
        runs=counted,
        tokens=tokens,
        calls=calls,
        calls_without_usage=without,
        skipped=dict(sorted(left_out.items())),
        incomplete=dict(sorted(incomplete.items())),
    )


def role_token_use(role: RoleRuns) -> TokenUse:
    """What every run of *role* recorded; a run that does not read back is named in ``skipped``."""
    return token_use(
        role.runs, {f"traces/{role.role}/{rel}": reason for rel, reason in role.skipped.items()}
    )


# ── sections ──────────────────────────────────────────────────────────


class ChallengeHit(_Model):
    """One recorded document an anti-bot challenge answered."""

    site: str
    run: str
    step: int | None
    status: int
    url: str
    signals: list[str]


class AntiBot(_Model):
    """Anti-bot / Cloudflare: sites, runs and steps whose documents carried challenge marks."""

    runs_checked: int
    #: Distinct sites of the recordings checked.
    sites: int
    hits: list[ChallengeHit]
    blocked_sites: list[str]
    skipped: dict[str, str] = {}


def anti_bot(record: RoleRuns) -> AntiBot:
    """Anti-bot (arch §4.16): ``archive_stats.challenge_documents`` over the recordings."""
    hits: list[ChallengeHit] = []
    checked: list[Run] = []
    skipped: dict[str, str] = {}
    for run in record.runs:
        try:
            found = challenge_documents(run.trace)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its network archive does not load ({type(exc).__name__})"
            continue
        checked.append(run)
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
        runs_checked=len(checked),
        sites=site_count(checked),
        hits=hits,
        blocked_sites=sorted({hit.site for hit in hits}),
        skipped=skipped,
    )


class SiteStats(_Model):
    """One site's numbers across the chain (arch §4.16 "站点")."""

    tasks: int
    record_runs: int
    #: The recordings' trace statuses (the manifest's ledger: complete, incomplete, writing).
    record_status: dict[str, int]
    #: How the recordings' agents ended (``result.json``'s ``agent_done``; ``(no result)`` when
    #: it was never written, ``(unreadable result)`` when it does not read back).
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
    #: Responses the replays (same-path and branch) served calling the JSONP callback name
    #: the page asked for instead of the recorded one (the replay log's ``callback_rewrite``).
    callback_rewrites: int
    #: Requests the replays answered for a consumed record without a body with an available
    #: record of identical body (the replay log's ``substituted``), by how the source request
    #: ended (``source_outcome``: page-cancelled and recorder-missed bodies apart). Like the
    #: ``exhausted`` reuse, never counted as a hit.
    substitutions: dict[str, int]
    #: Responses the replays served again after their sequence was exhausted
    #: (``repeat_last``; the replay log's ``exhausted``), never counted as a hit.
    reuses: int


class SiteExclusion(_Model):
    site: str
    reasons: list[str]


class Sites(_Model):
    """Per-site statistics and the exclusion list, in ``ExportFilters.exclude_sites`` shape."""

    by_site: dict[str, SiteStats]
    exclusions: list[SiteExclusion]
    exclude_sites: list[str]
    #: The exclusion list as an export ``filters`` fragment, ready to paste.
    yaml: str
    #: Replayed runs (``traces/<role>/<run>``) left out of their site's miss counts and branch
    #: share, and why: their replay log does not read back.
    skipped: dict[str, str] = {}


#: How an agent's run ended when it did not end cleanly (``result.json``'s ``agent_done``).
UNCLEAN_ENDINGS: frozenset[AgentDoneStatus] = frozenset(
    {AgentDoneStatus.ERROR, AgentDoneStatus.TIMEOUT, AgentDoneStatus.VETOED}
)


def ended_unclean(run: Run) -> bool:
    """Whether *run* did not end cleanly, by either ledger.

    Its trace is not ``complete`` (the manifest's ledger: incomplete, or
    never closed), or its agent ended in error, a timeout or a veto — or
    left no ``result.json`` that reads back (the agent's ledger).
    """
    if run.trace.manifest.status is not TraceStatus.COMPLETE:
        return True
    try:
        result = run.trace.result
    except RESULT_ERRORS:
        return True
    return result is None or result.agent_done in UNCLEAN_ENDINGS


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
    """By site (arch §4.16): counts, outcomes, fidelity, restoration, coverage, misses, rewrites,
    substitutions and reuses.

    Excluded: a site whose recordings met a challenge (*blocked*), whose
    same-path fidelity (the restorer's) is below *exclude_below*, or one of
    whose recordings' environment failed at step 0 (an observation or an
    action failed, or no step committed). A recording that failed on its
    model call is counted apart (``record_model_failures``) and excludes
    nothing. A recording whose ``result.json`` does not read back counts as
    ``(unreadable result)``; a replayed run whose replay log does not read
    back is left out of the miss counts and the branch share (``skipped``);
    an empty branch (:func:`empty_branch`) is left out of the branch share.
    """
    record = chain.role("record")
    names = sorted(
        {run.site for run in record.runs}
        | {run.site for role in ("same_path", "branch") for run in chain.role(role).runs}
    )
    by_site: dict[str, SiteStats] = {}
    exclusions: list[SiteExclusion] = []
    skipped: dict[str, str] = {}
    for site in names:
        recorded = [run for run in record.runs if run.site == site]
        statuses = Counter(run.trace.manifest.status.value for run in recorded)
        outcomes = Counter(agent_outcome(run) for run in recorded)
        diagnosed = [
            run
            for run in recorded
            if run.config.recording.errors and 0 not in run.trace.manifest.missing_steps
        ]
        for run in recorded:
            if run not in diagnosed:
                skipped[run.pointer] = "step errors were not recorded; failure counts omit this run"
        failed0 = sum(1 for run in diagnosed if _failed_at_step0(run))
        branches = [run for run in chain.role("branch").runs if run.site == site]
        rate = restoration_rate(run.trace for run in branches)
        clean = counted = documents = others = rewrites = reuses = 0
        substituted: Counter[str] = Counter()
        for role in ("same_path", "branch"):
            for run in chain.role(role).runs:
                if run.site != site:
                    continue
                try:
                    decisions = run.decisions
                    acted = role != "branch" or empty_branch(run) is None
                except RecordingScopeError as exc:
                    skipped[run.pointer] = str(exc)
                    continue
                except EVIDENCE_ERRORS as exc:
                    skipped[run.pointer] = (
                        f"its replay log does not read back ({type(exc).__name__})"
                    )
                    continue
                documents += sum(w for _s, e, w in decisions if e.diverged)
                others += sum(w for _s, e, w in decisions if e.kind == "miss" and not e.diverged)
                rewrites += sum(w for _s, e, w in decisions if e.callback_rewrite is not None)
                reuses += sum(w for _s, e, w in decisions if e.kind == "exhausted")
                for _s, e, w in decisions:
                    if e.kind == "substituted":
                        substituted[str(e.source_outcome)] += w
                if role == "branch" and acted:
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
            record_model_failures=sum(1 for run in diagnosed if failed_on_model_call(run)),
            blocked=site in blocked,
            same_path=same_path,
            restoration=ratio(rate.ok, rate.total, empty="no restoration check on this site"),
            branch_zero_document_miss=ratio(
                clean, counted, empty="no branch run of this site that acted"
            ),
            document_misses=documents,
            other_misses=others,
            callback_rewrites=rewrites,
            substitutions=_counts(substituted),
            reuses=reuses,
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
    return Sites(
        by_site=by_site,
        exclusions=exclusions,
        exclude_sites=excluded,
        yaml=snippet,
        skipped=dict(sorted(skipped.items())),
    )


class DuplicateBranches(_Model):
    """Duplicate branches (重复分叉率): checkpoint-step actions that repeat an earlier branch of the group."""

    branches: int
    #: Branches whose checkpoint step ran no action: nothing to compare — the export flags only
    #: samples, and a step without an action is none. Left out of ``rate``.
    not_compared: int
    #: Distinct sites of the branches compared.
    sites: int
    groups: int
    duplicates: int
    #: Duplicates over the branches compared.
    rate: Ratio
    #: The first export's ``duplicates`` (its samples flagged ``duplicate_of``), when it exists.
    dataset_duplicates: int | None
    skipped: dict[str, str] = {}


def duplicate_branches(
    branch: RoleRuns, dataset_manifest: DatasetManifest | None
) -> DuplicateBranches:
    """Duplicate branches (arch §4.16): the export's ``duplicate_of`` rule, on executed actions.

    Within one ``branch_group_id``, in ``branch_index`` order, a branch whose
    checkpoint step ran the same actions (canonical JSON of names and
    params) as an earlier one is a duplicate. A branch whose checkpoint step
    ran no action is not compared (``not_compared``): a failed teacher call
    is no duplicate, and the export never flags such a step.
    """
    groups: dict[str, list[Run]] = {}
    for run in branch.runs:
        provenance = run.config.branch
        if provenance is not None:
            groups.setdefault(provenance.branch_group_id, []).append(run)
    duplicates = branches = not_compared = 0
    compared: list[Run] = []
    skipped: dict[str, str] = {}
    for members in groups.values():
        seen: set[str] = set()
        for run in sorted(
            members, key=lambda r: r.config.branch.branch_index if r.config.branch else 0
        ):
            try:
                run.trace.require_recording("trajectory", consumer="duplicate branch analysis")
            except RecordingScopeError as exc:
                skipped[run.rel] = str(exc)
                continue
            branches += 1
            record = run.step(run.checkpoint or 0)
            if record is None or not record.actions:
                not_compared += 1
                continue
            compared.append(run)
            target = canonical_json([[a.name, a.params] for a in record.actions])
            if target in seen:
                duplicates += 1
            seen.add(target)
    return DuplicateBranches(
        branches=branches,
        not_compared=not_compared,
        sites=site_count(compared),
        groups=len(groups),
        duplicates=duplicates,
        rate=ratio(
            duplicates,
            len(compared),
            empty="no branch whose checkpoint step ran an action" if branches else "no branch run",
        ),
        dataset_duplicates=dataset_manifest.duplicates if dataset_manifest is not None else None,
        skipped=skipped,
    )


class PostBranchMisses(_Model):
    """POST branch misses (POST 分叉的 miss): POST misses from each branch's checkpoint on."""

    branches: int
    #: Distinct sites of the branches counted.
    sites: int
    document_misses: int
    other_misses: int
    #: Branches with at least one POST miss.
    branches_with_post_miss: Ratio
    skipped: dict[str, str] = {}


def post_branch_misses(branch: RoleRuns) -> PostBranchMisses:
    """POST branch misses (arch §4.16): documents and the rest apart."""
    documents = others = with_miss = 0
    counted: list[Run] = []
    skipped: dict[str, str] = {}
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its replay log does not read back ({type(exc).__name__})"
            continue
        counted.append(run)
        posts = [
            (e, w) for _s, e, w in decisions if e.kind == "miss" and e.method.upper() == "POST"
        ]
        documents += sum(w for e, w in posts if e.resource_type == "Document")
        others += sum(w for e, w in posts if e.resource_type != "Document")
        with_miss += int(any(w > 0 for _e, w in posts))
    return PostBranchMisses(
        branches=len(counted),
        sites=site_count(counted),
        document_misses=documents,
        other_misses=others,
        branches_with_post_miss=ratio(with_miss, len(counted), empty="no branch run"),
        skipped=skipped,
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
    """Storage growth (存储增长) and the compression evaluation's input, by role, plus the datasets."""

    by_role: dict[str, RoleStorage]
    datasets: dict[str, int]
    #: Distinct sites of the runs whose files were counted, every role.
    sites: int


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
    "call_metrics": "metrics",
    "output_schema": "messages",
    "prompt_image": "messages:images",
}


def storage_category(file: CommittedFile) -> str:
    """The storage section's category of one committed file (arch §4.16 "存储").

    ``observations:<fragment>`` / ``artifacts:<fragment>``, ``env:run`` (a
    capability's run record), ``env:blobs`` (content-addressed evidence),
    ``env:<archiver>``, ``extra`` (an extra file that is no evidence), and
    ``run`` / ``steps`` / ``messages`` (call records and reply schemas) /
    ``messages:images`` (the images prompts sent inline, each stored once)
    by kind.
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


def _role_storage(role: RoleRuns) -> tuple[RoleStorage, list[Run]]:
    """One role's storage, and the runs whose files were counted."""
    totals: dict[str, list[int]] = {}
    steps = runs = 0
    counted: list[Run] = []
    skipped: dict[str, str] = {}
    for run in role.runs:
        try:
            found = _run_storage(run.trace)
        except (*EVIDENCE_ERRORS, OSError) as exc:
            skipped[run.rel] = f"a listed file does not read back ({type(exc).__name__})"
            continue
        runs += 1
        steps += len(run.steps)
        counted.append(run)
        for category, counts in found.items():
            slot = totals.setdefault(category, [0, 0, 0, 0])
            for index, value in enumerate(counts):
                slot[index] += value
    total = sum(slot[1] for slot in totals.values())
    storage = RoleStorage(
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
    return storage, counted


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def storage(chain: ChainRuns) -> Storage:
    """Storage (arch §4.16): bytes by category per role, per run and per step; gzip ratios.

    A run's files are the ones ``RunTrace.committed_files`` lists (never a
    walk of the directory), each path once, read through ``read_committed``
    (:func:`storage_category` files them); bytes already compressed —
    screenshots, the WARC, gzip captures, by their leading bytes — are
    reported as size only.
    """
    by_role: dict[str, RoleStorage] = {}
    counted: list[Run] = []
    for role in ROLES:
        if chain.role(role).present:
            by_role[role], runs = _role_storage(chain.role(role))
            counted += runs
    return Storage(
        by_role=by_role,
        datasets={
            name: _tree_bytes(chain.layout.dataset(name))
            for name in DATASETS
            if chain.layout.dataset(name).is_dir()
        },
        sites=site_count(counted),
    )


class Projection(_Model):
    tasks: int
    bytes: float


class Retention(_Model):
    """Retention and capacity: bytes per run of each role and per dataset sample, and a projection."""

    per_run: dict[str, float | None]
    per_sample: float | None
    #: Of each role, runs per recorded task (the chain's own ratio).
    runs_per_task: dict[str, float | None]
    per_task: float | None
    projections: list[Projection]


def retention(
    chain: ChainRuns, stored: Storage, samples: int | None, tasks: Sequence[int]
) -> Retention:
    """Retention and capacity (arch §4.16): per-run and per-sample bytes, projected to *tasks* tasks.

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
    """Export yield (导出产出) from the first dataset: samples, drops, skips, ``action_failed`` share."""

    present: bool
    samples: dict[str, int] = {}
    untranslatable: dict[str, int] = {}
    steps_skipped: dict[str, int] = {}
    sources_exported: int = 0
    sources_skipped: dict[str, str] = {}
    sources_filtered: dict[str, str] = {}
    duplicates: int | None = None
    action_failed: Ratio


class DatasetFormatError(ValueError):
    """A published dataset's file does not read back as this build's format.

    Its ``manifest.json`` (:class:`~spectragent.schemas.dataset.DatasetManifest`)
    or a ``<split>.samples.jsonl`` line (``{"sample": CanonicalStepSample, ...}``).
    Raised rather than read as empty counts: a renamed key must not turn into
    a measured zero. The message names the file relative to the chain root.
    """


def read_dataset_manifest(layout: ChainLayout, name: str = "first") -> DatasetManifest | None:
    """An export's ``manifest.json`` (the first by default), or ``None`` when it has none.

    Read through the model the export writes it with
    (``schemas/dataset.py::DatasetManifest``).

    Raises:
        DatasetFormatError: The file exists but does not read back as the
            dataset format this build writes.
    """
    path = layout.dataset(name) / "manifest.json"
    if not path.is_file():
        return None
    try:
        return DatasetManifest.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        raise DatasetFormatError(
            f"datasets/{name}/manifest.json does not read back as dataset format "
            f"{DATASET_FORMAT!r}: {exc}"
        ) from exc


def dataset_samples(layout: ChainLayout, name: str = "first") -> list[CanonicalStepSample]:
    """Every sample an export published (the first by default), split by split.

    Each ``<split>.samples.jsonl`` line is ``{"sample": ..., "render": ...}``;
    the sample is read through the model the export dumps
    (``schemas/sample.py::CanonicalStepSample``), the rendering is not read.

    Raises:
        DatasetFormatError: A line does not parse, or its sample does not
            validate; the message names the file and the line.
    """
    samples: list[CanonicalStepSample] = []
    root = layout.dataset(name)
    for path in sorted(root.glob("*.samples.jsonl")):
        rel = f"datasets/{name}/{path.name}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
                if not isinstance(document, dict):
                    raise ValueError("a line is a JSON object")
                samples.append(CanonicalStepSample.model_validate(document["sample"]))
            except (ValueError, KeyError) as exc:  # JSONDecodeError and ValidationError included
                raise DatasetFormatError(
                    f"{rel} line {number} does not read back as a sample of dataset format "
                    f"{DATASET_FORMAT!r}: {exc}"
                ) from exc
    return samples


def export_yield(layout: ChainLayout, manifest: DatasetManifest | None) -> ExportYield:
    """Export yield (arch §4.16), read from the published files of ``datasets/first``.

    ``action_failed``: samples whose ``labels.step_status`` is
    ``action_failed`` over all samples (:func:`dataset_samples`) — whether
    the export should keep or drop them.

    Raises:
        DatasetFormatError: A sample line does not read back.
    """
    if manifest is None:
        return ExportYield(present=False, action_failed=ratio(0, 0, empty="no first export"))
    samples = dataset_samples(layout)
    total = len(samples)
    failed = sum(1 for sample in samples if sample.labels.step_status == "action_failed")
    return ExportYield(
        present=True,
        samples=dict(sorted(manifest.split.samples.items())),
        untranslatable=dict(sorted(manifest.steps.untranslatable.items())),
        steps_skipped=dict(sorted(manifest.steps.skipped.items())),
        sources_exported=len(manifest.sources.exported),
        sources_skipped=dict(sorted(manifest.sources.skipped.items())),
        sources_filtered=dict(sorted(manifest.sources.filtered.items())),
        duplicates=manifest.duplicates,
        action_failed=ratio(failed, total, empty="the export holds no sample"),
    )


class NearMissFieldSet(_Model):
    """One combination of fields that differed together, and in how many near misses."""

    query: list[str]
    body: list[str]
    #: The bodies differed where a digest hides which fields.
    opaque_body: bool
    misses: int


class NearMissEndpoint(_Model):
    """One endpoint's near misses (``archive_stats.site_rule_candidates``)."""

    host: str
    method: str
    path: str
    misses: int
    #: Per query parameter / body field, how many of those misses it differed in.
    query: dict[str, int]
    body: dict[str, int]
    #: Misses whose bodies differed where a digest hides which fields.
    opaque_body: int
    #: Each distinct combination of differing fields, most frequent first.
    field_sets: list[NearMissFieldSet]


class SiteRuleCandidates(_Model):
    """Replay misses an endpoint rule would have matched (``archive_stats``), by endpoint."""

    runs: int
    #: Distinct sites of the replayed runs counted.
    sites: int
    #: Most near misses first (ties by host, path and method).
    endpoints: list[NearMissEndpoint]
    #: Replayed runs (``traces/<role>/<run>``) left out, and why.
    skipped: dict[str, str] = {}


def site_rules(chain: ChainRuns) -> SiteRuleCandidates:
    """Near misses in every replayed run (``same_path`` and ``branch``), by endpoint and field.

    Each replay is compared with its source run (``source_trace`` under
    ``traces/record``), loaded with the rules the replay recorded it used
    (``recorded_site_rules``), so a field the replay's own rules ignored is
    never reported as differing. A source is loaded once per rule version.
    A replay whose source is not in the chain, or whose replay log, rule
    snapshot or source archive does not read back, is left out with the
    reason.
    """
    found: list[NearMiss] = []
    counted: list[Run] = []
    skipped: dict[str, str] = {}
    sources: dict[tuple[str, str], RecordedArchive | str] = {}
    for role in ("same_path", "branch"):
        for run in chain.role(role).runs:
            source = chain.source_of(run)
            if source is None:
                skipped[run.pointer] = "its source run is not under traces/record"
                continue
            try:
                rules = recorded_site_rules(run.trace)
            except EVIDENCE_ERRORS as exc:
                skipped[run.pointer] = (
                    f"its rule snapshot does not read back ({type(exc).__name__})"
                )
                continue
            key = (source.rel, rules.sha256)
            if key not in sources:
                try:
                    sources[key] = load_archive(source.trace, rules)
                except EVIDENCE_ERRORS as exc:
                    sources[key] = (
                        f"its source archive {source.pointer} does not read back "
                        f"({type(exc).__name__})"
                    )
            recorded = sources[key]
            if isinstance(recorded, str):
                skipped[run.pointer] = recorded
                continue
            try:
                found.extend(near_misses(run.decisions, recorded))
            except EVIDENCE_ERRORS as exc:
                skipped[run.pointer] = f"its replay log does not read back ({type(exc).__name__})"
                continue
            counted.append(run)
    return SiteRuleCandidates(
        runs=len(counted),
        sites=site_count(counted),
        endpoints=[
            NearMissEndpoint(
                host=candidate.host,
                method=candidate.method,
                path=candidate.path,
                misses=candidate.misses,
                query=dict(candidate.query),
                body=dict(candidate.body),
                opaque_body=candidate.opaque_body,
                field_sets=[
                    NearMissFieldSet(
                        query=list(fields.query),
                        body=list(fields.body),
                        opaque_body=fields.opaque_body,
                        misses=fields.misses,
                    )
                    for fields in candidate.field_sets
                ],
            )
            for candidate in site_rule_candidates(found)
        ],
        skipped=dict(sorted(skipped.items())),
    )


class BranchDocumentMisses(_Model):
    """The recording-coverage decision's input: the branches' main-frame document misses.

    Over the branches that acted (an empty branch is left out, as in
    :func:`branch_coverage`), and how many of their misses were GETs of URLs
    the recording never fetched.
    """

    #: Branches counted: their replay log reads back and they acted.
    branches: int
    #: Distinct sites of the branches counted.
    sites: int
    document_misses: int
    get_no_match: Ratio
    skipped: dict[str, str] = {}


def branch_document_misses(branch: RoleRuns) -> BranchDocumentMisses:
    """Of the branches' document misses (from the checkpoint on), the GETs nothing recorded matched."""
    total = unrecorded = 0
    counted: list[Run] = []
    skipped: dict[str, str] = {}
    for run in branch.runs:
        try:
            decisions = after_checkpoint(run)
            empty = empty_branch(run)
        except RecordingScopeError as exc:
            skipped[run.rel] = str(exc)
            continue
        except EVIDENCE_ERRORS as exc:
            skipped[run.rel] = f"its replay log does not read back ({type(exc).__name__})"
            continue
        if empty is not None:
            continue
        counted.append(run)
        for _step, entry, weight in decisions:
            if not entry.diverged:
                continue
            total += weight
            if entry.method.upper() == "GET" and entry.reason == "no_match":
                unrecorded += weight
    return BranchDocumentMisses(
        branches=len(counted),
        sites=site_count(counted),
        document_misses=total,
        get_no_match=ratio(unrecorded, total, empty="no document miss in any branch"),
        skipped=skipped,
    )


def exported_sites(chain: ChainRuns, manifest: DatasetManifest | None) -> int:
    """The distinct sites of the runs an export exported (its manifest's sources, under ``traces/``).

    A source the chain does not hold (exported from elsewhere) has no site here.
    """
    if manifest is None:
        return 0
    found: list[Run] = []
    for source in manifest.sources.exported:
        role, _, rel = source.path.partition("/")
        run = chain.roles[role].by_rel().get(rel) if role in chain.roles else None
        if run is not None:
            found.append(run)
    return site_count(found)


__all__ = [
    "DATASETS",
    "EMPTY_ENDED_BEFORE_DECISION",
    "EMPTY_MODEL_CALL_FAILED",
    "EMPTY_NO_ACTION",
    "ENVIRONMENT_PHASES",
    "EVIDENCE_ERRORS",
    "MODEL_CALL_PHASE",
    "NO_ACTION",
    "NO_RESULT",
    "NO_SITE",
    "NearMissEndpoint",
    "NearMissFieldSet",
    "RESULT_ERRORS",
    "ROLES",
    "TERMINAL",
    "TRAILING",
    "UNCLEAN_ENDINGS",
    "UNREADABLE_RESULT",
    "AntiBot",
    "ArchiveBytes",
    "ArchiveOverhead",
    "BodyCompletenessMetric",
    "BranchCoverage",
    "BranchDocumentMisses",
    "ChainLayout",
    "ChainRuns",
    "ChallengeHit",
    "DatasetFormatError",
    "DedupBytes",
    "Determinism",
    "DuplicateBranches",
    "EmptyBranches",
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
    "PrivacyGate",
    "Projection",
    "Ratio",
    "ReplayDeterminism",
    "RestorationRate",
    "Retention",
    "RoleRuns",
    "RoleStorage",
    "Run",
    "SamePathFidelity",
    "SettleWait",
    "SiteExclusion",
    "SiteRuleCandidates",
    "SiteStats",
    "Sites",
    "Storage",
    "StorageCategory",
    "TokenUse",
    "after_checkpoint",
    "agent_outcome",
    "anti_bot",
    "archive_bytes",
    "archive_overhead",
    "body_completeness_metric",
    "branch_coverage",
    "branch_document_misses",
    "canonical_json",
    "components",
    "dataset_samples",
    "duplicate_branches",
    "empty_branch",
    "ended_unclean",
    "env_times",
    "export_determinism",
    "export_yield",
    "exported_sites",
    "failed_on_model_call",
    "observation_settles",
    "open_chain",
    "open_role",
    "percentile",
    "post_branch_misses",
    "privacy",
    "ratio",
    "read_dataset_manifest",
    "replay_determinism",
    "request_texts",
    "restoration",
    "restoration_rate",
    "retention",
    "role_token_use",
    "run_tier",
    "same_path_fidelity",
    "settle_wait",
    "site_count",
    "site_rules",
    "sites",
    "storage",
    "storage_category",
    "token_use",
]
