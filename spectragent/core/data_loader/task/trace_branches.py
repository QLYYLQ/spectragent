"""``trace_branches`` — expand recorded runs into branch tasks (``docs/architecture_design.md`` §4.10).

A branch task restores a recorded run to the state before its step *t* and
lets an agent decide that step again (``BranchLoop``, §4.1). This loader turns
a directory of recorded runs into such tasks: for every source run it
selects, every checkpoint step its ``step_selector`` picks among the
branchable ones, ``branches_per_step`` tasks with the id
``<source_run_id>/s<t>/b<i>``. Each task hands the run three things
(``TaskContext``): the environment part — the source's own setup context
plus ``replay_archive`` (the source run directory, so the browser replays
the recorded network); the Loop part — ``checkpoint``; and the branch part —
:class:`~spectragent.schemas.restoration.BranchProvenance`, recorded as
``TraceConfig.branch``.

It reads a source only through :class:`RunTrace`, so only what was committed
exists for it (an uncommitted step file is invisible); ``result.json`` only
when ``agent_outcome`` asks how the agent ended.

**The two ledgers.** Whether the source's trace is a complete record is a
hard condition: a trace that was never closed (``writing``) has no branchable
step, and a checkpoint past the step the trace is complete through
(``complete_through``: an evidence hole stopped the run there) is never one —
so an incomplete run still contributes every checkpoint before its hole. How
the agent ended is a filter, ``agent_outcome``, ``any`` by default: a run cut
off by a rate limit at step 25 still gives its first 25 checkpoints.

**Filters and preconditions.** ``agent_outcome`` and ``require_eval`` are
filters: a source that does not match is left out (logged at INFO). A source
that matches but cannot be branched — an unreadable trace (one of an older
format among them), a branch or replay run, a missing required archive or
environment fingerprint, no environment setup context, a run id another
source shares — is a precondition failure: ``on_unbranchable: raise`` (the
default) raises one :class:`BenchmarkDataError` listing every such source;
``skip`` leaves them out. Every source or step left out is in
:attr:`TraceBranchesTaskLoader.skipped` with its reason.

**Task variants.** With ``task_variants`` (a ``task-variants-1`` file,
:mod:`~spectragent.core.data_loader.task.task_variants`), each branch task
decides under another task that starts from the same screen: the file names
the checkpoints (``source_run_id``, ``step``) and each one's task text. A
variant at a step that is a branchable candidate the ``step_selector`` chose
becomes ``branches_per_step`` tasks ``<source_run_id>/s<t>/v<variant_id>/b<i>``
whose task text is the variant's, whose Loop input ``checkpoint`` names the
variant (so ``BranchLoop`` refuses ``history_source: source``, which replays
the source's prompts and so its task), and whose provenance carries a
:class:`~spectragent.schemas.restoration.BranchTaskVariant`; the setup
context, the replay archive and everything else stay the source's. A file
that does not read as that format fails the scan; a variant it lists that
cannot be used (listed twice, an empty task text, a run id no kept source
has, a step that is not a chosen candidate) is left out, in ``skipped`` under
its key ``<source_run_id>/s<t>/v<variant_id>``.

**Lazy.** The constructor touches no file: the CLI composes the whole YAML
once per task, and a sweep over a thousand sources must not rescan them a
thousand times. The sources are scanned on first use (``task_ids``,
``__getitem__``, ``get``, ``len``, ``get_task_context``, ``skipped``).

**Not a dataclass.** ``TaskLoaderBase`` loads in ``__post_init__``, before
the Composer injects receptors, so this loader uses a hand-written ``__init__``:
it takes the inherited ``subset`` / ``data_dir`` and the knobs, and never
calls the dataclass machinery. ``step_selector`` is an annotation-only
receptor with an in-Python default (``all``), the ``ArchiveNetworkSource
.miss_policy`` shape.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, TypeAdapter

from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task._registrar import TaskLoaderBase, TaskLoaderRegistrar
from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.core.data_loader.task.step_selector import (  # noqa: F401 — registration
    AllSteps,
    EveryKSteps,
    ExplicitSteps,
    LastStep,
    StepSelector,
    default_step_selector,
)
from spectragent.core.data_loader.task.task_variants import TaskVariantEntry, read_task_variants
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.models import TraceConfig, TraceManifest
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import (
    AGENT_OUTCOMES,
    AgentOutcomeFilter,
    agent_outcome_filtered_out,
    match_runs,
    not_closed_reason,
    past_complete_reason,
    source_pattern_problem,
)
from spectragent.schemas.restoration import BranchProvenance, BranchTaskVariant, Checkpoint
from spectragent.schemas.step_record import StepRecord
from spectragent.schemas.task import (
    REPLAY_ARCHIVE_KEY,
    TaskContext,
    TaskDefinition,
    setup_class_for_env,
)

logger = logging.getLogger(__name__)

#: The Loop-input key ``BranchLoop`` declares.
CHECKPOINT_KEY = "checkpoint"
_REQUIRE_EVAL: TypeAdapter[RequireEval] = TypeAdapter(RequireEval)


class TraceBranchesTaskLoader(TaskLoaderBase):
    """Branch tasks from recorded runs: ``<source_run_id>/s<t>/b<i>`` (module docstring).

    With ``task_variants``, ``<source_run_id>/s<t>/v<variant_id>/b<i>`` instead.
    """

    __registry_key__ = "trace_branches"
    __wiring__: ClassVar[dict[str, Any]] = {
        "step_selector": ("step_selector", ["all", "every_k", "explicit", "last"]),
    }

    # Annotation only: conscribe installs a ``WiredField`` and the Composer
    # injects the configured selector; ``__init__`` shadows it with the default.
    step_selector: StepSelector  # type: ignore[misc]

    # ── the knobs, as attributes (assigned in __init__) ──
    sources: tuple[str, ...]
    branches_per_step: int
    agent_outcome: AgentOutcomeFilter
    require_eval: RequireEval | None
    require_archive: tuple[str, ...]
    on_unbranchable: Literal["raise", "skip"]
    task_variants: str | None
    _contexts: dict[str, TaskContext]
    _skipped: dict[str, str]
    _scanned: bool

    def __init__(
        self,
        *,
        subset: Annotated[
            str,
            Field(description="A label recorded with every branch run; selects nothing."),
        ] = "All",
        data_dir: Annotated[
            str | None,
            Field(
                description=(
                    "The sources root: the directory the 'sources' globs are relative "
                    "to ('~' expanded). None: 'traces' in the working directory."
                ),
            ),
        ] = None,
        sources: Annotated[
            tuple[str, ...],
            Field(
                description=(
                    "Globs relative to the sources root naming recorded run "
                    "directories; the matches are sorted by path, and a match without "
                    "a manifest.json is not a run and is ignored. Default: "
                    "['*'], every run directly under the root."
                ),
            ),
        ] = ("*",),
        branches_per_step: Annotated[
            int,
            Field(ge=1, description="Branch tasks per selected checkpoint step (b0, b1, ...)."),
        ] = 1,
        agent_outcome: Annotated[
            AgentOutcomeFilter,
            Field(
                description=(
                    "Filter: keep sources whose agent ended this way, as their result.json "
                    "says (done, max_steps, error, timeout, vetoed); 'any' (the default) keeps "
                    "every outcome. Separate from the hard condition on the trace itself: a "
                    "checkpoint is always a step the source's trace is closed and complete "
                    "through."
                ),
            ),
        ] = "any",
        require_eval: Annotated[
            RequireEval | None,
            Field(
                description=(
                    "Filter: keep sources an evaluator graded 'label' (evaluator_id, "
                    "label: 1, slug: null — read from each source's evals/). None: no "
                    "evaluation needed."
                ),
            ),
        ] = None,
        require_archive: Annotated[
            tuple[str, ...],
            Field(
                description=(
                    "Precondition: archiver keys each source must have recorded, with "
                    "their capability's environment fingerprint committed. Default: "
                    "['browser.network'], what an offline browser branch "
                    "replays from; [] requires none."
                ),
            ),
        ] = ("browser.network",),
        on_unbranchable: Annotated[
            Literal["raise", "skip"],
            Field(
                description=(
                    "What a selected source that cannot be branched does: 'raise' one "
                    "error naming every such source before anything runs, or 'skip' it "
                    "(recorded with its reason in the loader's 'skipped')."
                ),
            ),
        ] = "raise",
        task_variants: Annotated[
            str | None,
            Field(
                description=(
                    "A task-variants file (JSON, format 'task-variants-1'; '~' expanded, "
                    "relative to the working directory): each entry gives a source run id, a "
                    "step and a different task that starts from that step's screen. Set, the "
                    "file names the checkpoints (among the candidates step_selector chose), "
                    "and every task is '<source_run_id>/s<t>/v<variant_id>/b<i>' with the "
                    "variant's task text; it needs the branch loop's history_source: "
                    "restored. None: every branch keeps its source's task."
                ),
            ),
        ] = None,
    ) -> None:
        # The receptor's in-Python default; the Composer injects the configured one.
        self.step_selector = default_step_selector()
        self.subset = subset
        self.data_dir = data_dir
        self.sources = tuple(sources)
        for pattern in self.sources:
            problem = source_pattern_problem(pattern)
            if problem is not None:
                raise ValueError(f"trace_branches: source glob {pattern!r} {problem}")
        if branches_per_step < 1:
            raise ValueError(
                f"trace_branches: branches_per_step must be >= 1, got {branches_per_step}"
            )
        if agent_outcome not in AGENT_OUTCOMES:
            raise ValueError(
                f"trace_branches: agent_outcome is one of {', '.join(AGENT_OUTCOMES)}, "
                f"got {agent_outcome!r}"
            )
        if on_unbranchable not in ("raise", "skip"):
            raise ValueError(
                f"trace_branches: on_unbranchable is 'raise' or 'skip', got {on_unbranchable!r}"
            )
        self.branches_per_step = branches_per_step
        self.agent_outcome = agent_outcome
        # The Composer hands a nested knob over as a dict; Python callers pass the config.
        self.require_eval = (
            require_eval
            if require_eval is None or isinstance(require_eval, RequireEval)
            else _REQUIRE_EVAL.validate_python(require_eval)
        )
        self.require_archive = tuple(require_archive)
        self.on_unbranchable = on_unbranchable
        self.task_variants = task_variants
        self._tasks = {}
        self._contexts = {}
        self._skipped = {}
        self._scanned = False

    # ── the loader surface, scanning on first use ────────────────────

    def __len__(self) -> int:
        self._scan_once()
        return len(self._tasks)

    def __getitem__(self, task_id: str) -> TaskDefinition:
        self._scan_once()
        return self._tasks[task_id]

    def get(self, task_id: str) -> TaskDefinition | None:
        self._scan_once()
        return self._tasks.get(task_id)

    def task_ids(self) -> list[str]:
        self._scan_once()
        return list(self._tasks)

    def get_task_context(self, task_id: str) -> TaskContext:
        """The branch task's three parts: env (source setup + ``replay_archive``), loop, branch.

        An unknown ``task_id`` yields an empty ``TaskContext()``, as for every loader.
        """
        self._scan_once()
        context = self._contexts.get(task_id)
        return context.model_copy(deep=True) if context is not None else TaskContext()

    @property
    def skipped(self) -> Mapping[str, str]:
        """What the scan left out, and why.

        Keyed by the source's path relative to the sources root (POSIX); a
        single selected step a source cannot branch at is keyed
        ``<path>#s<t>``; a task variant left out, by its
        ``<source_run_id>/s<t>/v<variant_id>``. Filtered sources are here too
        (``filtered: …``).
        """
        self._scan_once()
        return MappingProxyType(self._skipped)

    # ── scanning ─────────────────────────────────────────────────────

    def root(self) -> Path:
        """The sources root, absolute (``data_dir``, or ``traces`` in the working directory)."""
        return Path(self.data_dir if self.data_dir is not None else "traces").expanduser().resolve()

    def task_variants_path(self) -> Path | None:
        """The ``task_variants`` file, absolute, or ``None`` when every branch keeps its task."""
        if self.task_variants is None:
            return None
        return Path(self.task_variants).expanduser().resolve()

    def _scan_once(self) -> None:
        if self._scanned:
            return
        scan = _Scan(self)
        scan.run()
        self._tasks, self._contexts, self._skipped = scan.tasks, scan.contexts, scan.skipped
        self._scanned = True


class _Unbranchable(Exception):
    """A source that passed the filters but cannot be branched; the message says why."""


@dataclass(frozen=True)
class _Placement:
    """What every branch task of one source shares: where it is, its configuration and setup."""

    rel: str
    directory: Path
    run_id: str
    config: TraceConfig
    env: dict[str, Any]


class _Scan:
    """One scan of the sources root — what the loader publishes only when it succeeds."""

    def __init__(self, loader: TraceBranchesTaskLoader) -> None:
        self.loader = loader
        self.tasks: dict[str, TaskDefinition] = {}
        self.contexts: dict[str, TaskContext] = {}
        self.skipped: dict[str, str] = {}
        #: Source benchmark loaders built to re-resolve setup contexts, by (benchmark, subset).
        self._source_loaders: dict[tuple[str, str], Any] = {}
        #: The usable task variants still to place, by source run id, then step
        #: (``None``: no ``task_variants`` — every branch keeps its source's task).
        self._variants: dict[str, dict[int, list[TaskVariantEntry]]] | None = None
        #: Every readable matched source's run id → its paths, to say why a
        #: variant whose source was left out was left out.
        self._sources_by_run_id: dict[str, list[str]] = {}

    def run(self) -> None:
        loader = self.loader
        root = loader.root()
        variants_path = loader.task_variants_path()
        if variants_path is not None:
            self._variants = self._usable_variants(read_task_variants(variants_path))
        matched = match_runs(root, loader.sources)
        if not matched:
            logger.warning(
                "trace_branches: no recorded run under %s matches %s; no branch tasks",
                root,
                list(loader.sources),
            )
            self._leave_out_unplaced_variants(root)
            return

        unbranchable: dict[str, str] = {}
        kept: list[tuple[str, Path, RunTrace]] = []
        for rel, directory in matched:
            try:
                trace = RunTrace(directory)
                self._sources_by_run_id.setdefault(trace.run_id, []).append(rel)
                reason = self._filtered_out(trace)
            except TraceSchemaError as exc:
                unbranchable[rel] = f"not a readable trace: {exc}"
                continue
            except _Unbranchable as exc:
                unbranchable[rel] = str(exc)
                continue
            if reason is not None:
                logger.info("trace_branches: %s filtered out: %s", rel, reason)
                self.skipped[rel] = f"filtered: {reason}"
                continue
            kept.append((rel, directory, trace))

        owners: dict[str, list[str]] = {}
        for rel, _directory, trace in kept:
            owners.setdefault(trace.run_id, []).append(rel)
        for rel, directory, trace in kept:
            shared = [other for other in owners[trace.run_id] if other != rel]
            if shared:
                unbranchable[rel] = (
                    f"its run id {trace.run_id!r} is also the run id of {', '.join(shared)}: "
                    f"their branch task ids would collide"
                )
                continue
            try:
                self._expand(rel, directory, trace)
            except _Unbranchable as exc:
                unbranchable[rel] = str(exc)

        if unbranchable and loader.on_unbranchable == "raise":
            listing = "\n".join(f"  {rel}: {why}" for rel, why in sorted(unbranchable.items()))
            raise BenchmarkDataError(
                f"trace_branches: {len(unbranchable)} source(s) under {root} cannot be "
                f"branched (on_unbranchable: raise; 'skip' leaves them out):\n{listing}"
            )
        for rel, why in sorted(unbranchable.items()):
            logger.warning("trace_branches: skipping %s: %s", rel, why)
            self.skipped[rel] = why
        self._leave_out_unplaced_variants(root)

    # ── task variants ────────────────────────────────────────────────

    def _usable_variants(
        self, entries: list[TaskVariantEntry]
    ) -> dict[str, dict[int, list[TaskVariantEntry]]]:
        """*entries* by run id and step, in file order — less the ones no source can fix.

        A ``(source_run_id, step, variant_id)`` listed more than once is left
        out in every copy (nothing says which one is meant), and so is a
        variant whose task text is empty.
        """
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.key] = counts.get(entry.key, 0) + 1
        usable: dict[str, dict[int, list[TaskVariantEntry]]] = {}
        for entry in entries:
            if counts[entry.key] > 1:
                self.skipped[entry.key] = (
                    f"task variant {entry.variant_id!r} is listed {counts[entry.key]} times for "
                    f"run {entry.source_run_id!r} step {entry.step} in the task_variants file; "
                    f"variant ids must be unique per run and step"
                )
                continue
            if not entry.task_text.strip():
                self.skipped[entry.key] = (
                    f"task variant {entry.variant_id!r} has an empty task_text"
                )
                continue
            usable.setdefault(entry.source_run_id, {}).setdefault(entry.step, []).append(entry)
        return usable

    def _leave_out_unplaced_variants(self, root: Path) -> None:
        """Every variant no expanded source took, with why its source was not there."""
        if not self._variants:
            return
        for run_id, by_step in self._variants.items():
            rels = self._sources_by_run_id.get(run_id)
            if rels:
                why = "; ".join(
                    f"{rel}: {self.skipped.get(rel, 'left out')}" for rel in sorted(rels)
                )
                reason = f"its source run {run_id!r} is left out ({why})"
            else:
                reason = (
                    f"no readable source run under {root} matching {list(self.loader.sources)} "
                    f"has run id {run_id!r}"
                )
            for entries in by_step.values():
                for entry in entries:
                    self.skipped[entry.key] = reason
        self._variants = {}

    def _filtered_out(self, trace: RunTrace) -> str | None:
        """Why a filter leaves *trace* out, or ``None`` when it is kept."""
        loader = self.loader
        try:
            outcome = agent_outcome_filtered_out(trace, loader.agent_outcome)
        except TraceSchemaError as exc:
            raise _Unbranchable(f"its result.json does not read back: {exc}") from exc
        if outcome is not None:
            return outcome
        need = loader.require_eval
        if need is not None:
            try:
                verdicts = trace.evals()
            except TraceSchemaError as exc:
                raise _Unbranchable(f"its evaluations do not read back: {exc}") from exc
            if not need.satisfied_by(verdicts):
                pinned = f", slug {need.slug!r}" if need.slug is not None else ""
                return (
                    f"no graded verdict from {need.evaluator_id!r} with predicted_label "
                    f"{need.label}{pinned} (require_eval)"
                )
        return None

    def _expand(self, rel: str, directory: Path, trace: RunTrace) -> None:
        """Every branch task of one source that passed the filters."""
        try:
            config = trace.config
            trace.require_recording("trajectory", "observations", consumer="branch restoration")
            records = trace.step_records
        except RecordingScopeError as exc:
            raise _Unbranchable(str(exc)) from exc
        except TraceSchemaError as exc:
            raise _Unbranchable(f"its configuration or steps do not read back: {exc}") from exc
        if config.branch is not None:
            raise _Unbranchable(
                f"it is itself a branch run of {config.branch.source_run_id!r}: branch from "
                f"that source run instead (a branch's prefix has no model calls to replay)"
            )
        if config.task_setup is not None and REPLAY_ARCHIVE_KEY in config.task_setup.context:
            raise _Unbranchable("it is a replay run: branch from the recorded run it replayed")
        self._require_archive(trace, config)
        env = self._setup_context(config)

        manifest = trace.manifest
        unclosed = not_closed_reason(manifest)
        if unclosed is not None:
            self.skipped[rel] = f"no branchable step: {unclosed}"
            return
        by_step = {record.step: record for record in records}
        candidates = _candidates(by_step, manifest.complete_through)
        if not candidates:
            self.skipped[rel] = (
                "no branchable step (a committed policy step whose prefix is committed, "
                "whose observation was recorded and that the trace is complete through)"
            )
            return
        source = _Placement(rel, directory, trace.run_id, config, env)
        selected = self.loader.step_selector.select(candidates)
        if self._variants is not None:
            self._expand_variants(source, by_step, manifest, candidates, selected)
            return
        for step in selected:
            if step not in candidates:
                self.skipped[f"{rel}#s{step}"] = _why_not_a_candidate(by_step, step, manifest)
                continue
            for index in range(self.loader.branches_per_step):
                self._add_task(source, step, index)

    def _expand_variants(
        self,
        source: _Placement,
        by_step: Mapping[int, StepRecord],
        manifest: TraceManifest,
        candidates: list[int],
        selected: list[int],
    ) -> None:
        """The source's branch tasks under its task variants: the file names the steps.

        A variant's step must be a candidate the selector chose; otherwise
        the variant is left out with the reason, keyed by the variant.
        """
        assert self._variants is not None
        chosen = set(selected)
        selector = self.loader.step_selector.selector_id
        for step, entries in sorted(self._variants.pop(source.run_id, {}).items()):
            if step not in candidates:
                reason = _why_not_a_candidate(by_step, step, manifest)
            elif step not in chosen:
                reason = f"step {step} is a candidate step_selector {selector!r} did not choose"
            else:
                reason = None
            for entry in entries:
                if reason is not None:
                    self.skipped[entry.key] = f"{source.rel}: {reason}"
                    continue
                for index in range(self.loader.branches_per_step):
                    self._add_task(source, step, index, variant=entry)

    def _require_archive(self, trace: RunTrace, config: TraceConfig) -> None:
        negotiation = config.negotiation
        active = set(negotiation.active_archivers) if negotiation is not None else set()
        bindings = negotiation.archive_bindings if negotiation is not None else {}
        for key in self.loader.require_archive:
            if key not in active:
                bound = ", ".join(sorted(active)) or "none"
                raise _Unbranchable(
                    f"it did not record {key!r} (require_archive); archivers it bound: {bound}"
                )
            capability = bindings.get(key, key.partition(".")[0])
            try:
                fingerprint = trace.archive_run_file(capability)
            except TraceSchemaError as exc:
                raise _Unbranchable(f"its environment evidence does not read back: {exc}") from exc
            if fingerprint is None:
                raise _Unbranchable(
                    f"it has no committed environment fingerprint (run record) for "
                    f"capability {capability!r}, which {key!r} needs (require_archive)"
                )

    def _setup_context(self, config: TraceConfig) -> dict[str, Any]:
        """The source's environment setup context — never a redacted one (§4.10)."""
        setup = config.task_setup
        if setup is not None and not setup.redactions.entries:
            return dict(setup.context)
        why = (
            "it recorded none"
            if setup is None
            else f"the recorded one is redacted ({len(setup.redactions.entries)} value(s))"
        )
        if not config.benchmark or not config.task_id:
            raise _Unbranchable(
                f"no environment setup context: {why}, and it names no source benchmark task "
                f"to re-resolve it from"
            )
        key = (config.benchmark, config.subset)
        if key not in self._source_loaders:
            try:
                loader_cls = TaskLoaderRegistrar.get(config.benchmark)
                self._source_loaders[key] = (
                    loader_cls(subset=config.subset) if config.subset else loader_cls()
                )
            except Exception as exc:  # the source benchmark or its data is not here
                self._source_loaders[key] = exc
        source = self._source_loaders[key]
        label = f"{config.benchmark}/{config.subset or '-'}/{config.task_id}"
        if isinstance(source, Exception):
            raise _Unbranchable(
                f"no environment setup context: {why}, and re-resolving {label} failed: "
                f"{type(source).__name__}: {source}"
            )
        if source.get(config.task_id) is None:
            raise _Unbranchable(
                f"no environment setup context: {why}, and the source benchmark's loader "
                f"has no task {label}"
            )
        return dict(source.get_task_context(config.task_id).env)

    def _add_task(
        self,
        source: _Placement,
        step: int,
        index: int,
        *,
        variant: TaskVariantEntry | None = None,
    ) -> None:
        """One branch task: *source* at *step*, branch *index*, under *variant* when given."""
        run_id, rel, config, env = source.run_id, source.rel, source.config, source.env
        group_id = f"{run_id}/s{step}" if variant is None else variant.key
        task_id = f"{group_id}/b{index}"
        env_type = config.env_type or self.loader.env_type
        fields: dict[str, Any] = {
            "task_id": task_id,
            "benchmark": self.loader.benchmark_name,
            "env_type": env_type,
            "task_text": config.task if variant is None else variant.task_text,
            "difficulty": config.extra.get("difficulty"),
            "domain": config.extra.get("domain"),
            "tags": list(config.extra.get("tags") or []),
        }
        setup = _setup_for(env_type, env)
        if setup is not None:
            fields["setup"] = setup
        self.tasks[task_id] = TaskDefinition(**fields)
        source_dir = str(source.directory)
        self.contexts[task_id] = TaskContext(
            env={**env, REPLAY_ARCHIVE_KEY: source_dir},
            loop={
                CHECKPOINT_KEY: Checkpoint(
                    trace_dir=source_dir,
                    step=step,
                    task_variant=variant.variant_id if variant is not None else None,
                )
            },
            branch=BranchProvenance(
                source_run_id=run_id,
                source_trace=rel,
                source_benchmark=config.benchmark,
                source_subset=config.subset,
                source_task_id=config.task_id,
                checkpoint_step=step,
                branch_index=index,
                branch_group_id=group_id,
                task_variant=(
                    BranchTaskVariant(
                        variant_id=variant.variant_id,
                        task_text=variant.task_text,
                        source_task_text=config.task,
                        note=variant.note,
                    )
                    if variant is not None
                    else None
                ),
            ),
        )


def _candidates(by_step: Mapping[int, StepRecord], complete_through: int) -> list[int]:
    """Committed policy steps whose prefix is committed, whose observation was recorded, and
    that the trace is complete through."""
    candidates: list[int] = []
    step = 0
    while step in by_step and step <= complete_through:
        record = by_step[step]
        if record.origin == "policy" and record.error_phase != "observe":
            candidates.append(step)
        step += 1
    return candidates


def _why_not_a_candidate(
    by_step: Mapping[int, StepRecord], step: int, manifest: TraceManifest
) -> str:
    if step not in by_step:
        committed = sorted(by_step)
        last = f"its last committed step is {committed[-1]}" if committed else "it has no step"
        return f"step {step} is not committed ({last})"
    missing = next((k for k in range(step) if k not in by_step), None)
    if missing is not None:
        return f"step {step}'s prefix is not committed (step {missing} is missing)"
    past = past_complete_reason(manifest, step)
    if past is not None:
        return past
    record = by_step[step]
    if record.origin != "policy":
        return f"step {step} is a {record.origin} step, not one the agent decided"
    return f"step {step}'s observation was not recorded (error_phase {record.error_phase!r})"


def _setup_for(env_type: str, env: Mapping[str, Any]) -> Any:
    """``TaskDefinition.setup`` mirroring the env part, when the env type has a setup variant.

    Decorative for this loader — what runs is ``get_task_context(...).env`` —
    but it keeps a branch task's definition from claiming a default setup.
    """
    try:
        variant = setup_class_for_env(env_type)
    except ValueError:
        return None
    return variant.model_validate({**env, "setup_type": variant.setup_type_value()})


__all__ = ["TraceBranchesTaskLoader"]
