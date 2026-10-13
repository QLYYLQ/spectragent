"""The metrics chain: record → same-path replay → branch → export → report (arch §4.16 "链").

Not a new way to run anything: each stage calls an existing entry point's
``main(argv)`` in this process — ``spectragent.core.benchmark_runner.main``
(``spectragent-bench``) for the run stages, ``spectragent.export.cli.main``
(``spectragent-export``) for the export, twice — and the last stage computes
the report (``report.py``).

**The spec** is a plain YAML (:class:`ChainSpec`, ``extra="forbid"``), not a
conscribe run root — so it never lives beside ``configs/*.yaml``, where every
file must compose and prepare. Relative paths in it (the root, each stage's
config) are relative to the working directory, as every af CLI reads them.

**The chain only writes paths.** Each stage's YAML is read as it is and
these fields alone are set before it is written to
``<root>/configs/<stage>.yaml`` (and run from there): the one
``trace_writer``'s ``trace_dir`` (``<root>/traces/<stage>``); for
``same_path`` / ``branch``, the one ``trace_branches`` loader's ``data_dir``
(``<root>/traces/record``); for the export, the one exporter's
``sources_root`` (``<root>/traces``), ``sources`` (``record/*``,
``branch/*``) and ``output_dir`` (``<root>/datasets/first``, then
``second``). A block missing, or there twice, is a :class:`ChainConfigError`
naming the stage and the block — before any stage runs. Everything else
(agent, tier, archivers, selector, branches, ``verify``, evaluator) is the
stage YAML's; a control group's YAML says ``archive: []`` itself.

**A stage's outcome.** A run stage that exits 0 or 1 (a task with an
infrastructure failure is data to measure) and wrote at least one run
directory ran; another exit code (2: its pre-flight refused the stage's
configuration and no task ran; 3: a guard vetoed the stage — its runs are not
fed to the next stage), ``SystemExit``, an exception or no run directory is
:class:`ChainStageError` naming the stage — the chain stops, the products so
far stay. An export that exits
non-zero (it then wrote nothing) is one too. So is an exception out of the
report stage — a defect or an unwritable report directory, never a damaged
run (the report skips one with its reason). A selected export whose
``datasets/`` directory already exists (an export never overwrites) is a
:class:`ChainConfigError` before any stage runs: the paid stages before it
would otherwise run for an export certain to refuse.
``chain.json`` records each stage's argv, exit code, run directories and
start and end time — provenance only; the report never reads it.

**The token limit** (``token_limit``; the user's ruling of 2026-10-14).
``unlimited``, the default, counts nothing: no stage is handed a limit, and
the stages run and are recorded as they always were. With a number, the
chain's total is every token the runs under the root recorded
(``metrics.role_token_use``, counting from each run's call records through
``core/trace/usage.py``): counted once before the first stage, so a chain
run stage by stage stays inside one limit, then increased by each run
stage's new runs (``stages.<stage>.token_use``). Each run stage gets what is
left (``spectragent-bench --token-limit``), which lets the task that reaches
it finish and starts no other. When a stage's sweep exits
``TOKEN_LIMIT_EXIT_CODE``, or the total reaches the limit, no later run or
export stage starts; the report still runs when selected and states the
stop itself (it counts from the traces too); ``chain.json`` records
``stopped_by_token_limit`` and :attr:`ChainResult.token_limit_stop` says the
same. Runs that had already reached the limit before the chain started stop
it before any run or export stage (the stop's ``stage`` is ``None``).

**Secrets.** ``spectragent-bench`` reads no ``.env`` and neither does the
chain: export the keys in the shell first (``set -a; . ./.env; set +a``).
A planted credential the report searches for is written in the spec as a
literal (``value``, the rehearsal) or as the name of an environment variable
(``env``, a live run) whose value is read from this process's environment.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from spectragent.flywheel.metrics import (
    DATASETS,
    ChainLayout,
    Planted,
    TokenUse,
    open_chain,
    open_role,
    role_token_use,
    token_use,
)
from spectragent.flywheel.report import FlywheelReport, build_report, write_report
from spectragent.schemas._types import UNLIMITED, Unlimited, limit_value

logger = logging.getLogger(__name__)

#: The stages, in the order a chain runs them.
STAGES: tuple[str, ...] = ("record", "control", "same_path", "branch", "export", "report")
#: The stages run by ``spectragent-bench``.
RUN_STAGES: tuple[str, ...] = ("record", "control", "same_path", "branch")
#: The stages whose tasks come from the recordings (a ``trace_branches`` loader).
BRANCH_STAGES: tuple[str, ...] = ("same_path", "branch")
#: The export's sources, relative to ``<root>/traces``: the recordings and the branches.
EXPORT_SOURCES: tuple[str, ...] = ("record/*", "branch/*")


class ChainConfigError(ValueError):
    """The chain spec or a stage's YAML cannot run as given — raised before any stage runs."""


class ChainStageError(RuntimeError):
    """A stage failed; the chain stopped there (earlier stages' products stay)."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"stage {stage!r}: {message}")
        self.stage = stage


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TasksSpec(_Spec):
    """The task selection handed to the recording stages as CLI flags."""

    benchmark: str | None = None
    subset: str | None = None
    task_ids: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)


class StageSpec(_Spec):
    """A ``spectragent-bench`` stage: its YAML, the per-task timeout, whether it evaluates."""

    config: Path
    timeout: float = Field(default=300.0, gt=0)
    evaluate: bool = False


class ExportStageSpec(_Spec):
    """The export stage: its YAML and how many times to export (2 measures determinism)."""

    config: Path
    repeat: Literal[1, 2] = 2


class PlantedSecret(_Spec):
    """A credential the report counts: a literal (``value``) or an environment variable's name (``env``)."""

    label: str = Field(min_length=1)
    value: str | None = Field(default=None, min_length=1)
    env: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_source(self) -> PlantedSecret:
        if (self.value is None) == (self.env is None):
            raise ValueError(f"planted {self.label!r} needs exactly one of 'value' and 'env'")
        return self


class ReportSpec(_Spec):
    planted: list[PlantedSecret] = Field(default_factory=list)


class ChainSpec(_Spec):
    """A chain (arch §4.16): where its products go, what evidence it is, and its stages."""

    name: str = Field(min_length=1)
    root: Path
    evidence: Literal["live", "rehearsal"]
    tasks: TasksSpec = Field(default_factory=TasksSpec)
    token_limit: Annotated[int, Field(ge=1)] | Unlimited = Field(
        default=UNLIMITED,
        description=(
            "The most tokens the chain's run stages may use together, counted from the model "
            "calls every run under the root recorded (the provider's total, or prompt plus "
            "completion; a call that reported no usage adds nothing and is named). Each finished "
            "task adds its tokens; once the total reaches the limit, the task that reached it "
            "finishes, no later task or stage starts, and the report is still computed from what "
            "exists and says the chain stopped at its token limit. A number of at least 1, or "
            "'unlimited' (the default): no limit, and nothing is counted."
        ),
    )
    record: StageSpec
    control: StageSpec | None = None
    same_path: StageSpec | None = None
    branch: StageSpec | None = None
    export: ExportStageSpec | None = None
    report: ReportSpec = Field(default_factory=ReportSpec)

    @model_validator(mode="after")
    def _branch_stages_never_evaluate(self) -> ChainSpec:
        for stage in BRANCH_STAGES:
            spec = getattr(self, stage)
            if spec is not None and spec.evaluate:
                raise ValueError(
                    f"{stage}.evaluate: a trace_branches sweep has no eval loader (arch §4.10); "
                    f"it always runs with --no-eval"
                )
        return self

    @classmethod
    def load(cls, path: Path) -> ChainSpec:
        """Read a chain spec YAML.

        Raises:
            ChainConfigError: The file does not read, is not a mapping, or does
                not validate (an unknown key included).
        """
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ChainConfigError(f"chain spec {path} does not read: {exc}") from exc
        if not isinstance(raw, dict):
            raise ChainConfigError(f"chain spec {path} is not a YAML mapping")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ChainConfigError(f"chain spec {path} is not valid: {exc}") from exc

    def configured(self) -> list[str]:
        """The stages this spec configures, in chain order (``report`` always)."""
        return [stage for stage in STAGES if stage == "report" or getattr(self, stage) is not None]


def resolve_planted(
    planted: Sequence[PlantedSecret], environ: Mapping[str, str] | None = None
) -> list[Planted]:
    """The planted credentials with their values: a literal as written, a variable from *environ*.

    A variable that is not set gives ``value=None`` — the report then says it
    could not search for it, naming the variable, never inventing a value.
    """
    env = os.environ if environ is None else environ
    resolved: list[Planted] = []
    for item in planted:
        if item.value is not None:
            resolved.append(Planted(label=item.label, value=item.value, source="value"))
        else:
            assert item.env is not None
            resolved.append(
                Planted(label=item.label, value=env.get(item.env) or None, source=f"env:{item.env}")
            )
    return resolved


# ── the path plumbing ─────────────────────────────────────────────────


def _load_stage_yaml(stage: str, path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChainConfigError(f"stage {stage!r}: {path} does not read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChainConfigError(f"stage {stage!r}: {path} is not a YAML mapping")
    return raw


def _one(
    stage: str, path: Path, raw: Mapping[str, Any], layer: str, key: str, value: str
) -> dict[str, Any]:
    """The one entry of *layer* whose *key* is *value* — or a ``ChainConfigError`` naming the stage."""
    entries = [
        entry
        for entry in raw.get(layer) or []
        if isinstance(entry, dict) and entry.get(key) == value
    ]
    if len(entries) != 1:
        found = "no" if not entries else f"{len(entries)}"
        raise ChainConfigError(
            f"stage {stage!r}: {path} has {found} '{layer}' block with {key}: {value} — the chain "
            f"sets its paths and needs exactly one"
        )
    return entries[0]


def run_stage_config(stage: str, path: Path, layout: ChainLayout) -> dict[str, Any]:
    """*path*'s YAML with this chain's paths written in (module docstring); nothing else changes."""
    raw = _load_stage_yaml(stage, path)
    writer = _one(stage, path, raw, "hook", "hook_id", "trace_writer")
    writer["trace_dir"] = str(layout.traces(stage))
    if stage in BRANCH_STAGES:
        loader = _one(stage, path, raw, "task_loader", "benchmark_name", "trace_branches")
        loader["data_dir"] = str(layout.traces("record"))
    return raw


def export_stage_config(path: Path, layout: ChainLayout, dataset: str) -> dict[str, Any]:
    """The export YAML with the chain's sources and *dataset*'s output directory written in."""
    raw = _load_stage_yaml("export", path)
    exporters = [entry for entry in raw.get("exporter") or [] if isinstance(entry, dict)]
    if len(exporters) != 1:
        raise ChainConfigError(
            f"stage 'export': {path} has {len(exporters)} 'exporter' blocks — the chain sets "
            f"their paths and needs exactly one"
        )
    exporter = exporters[0]
    exporter["sources_root"] = str(layout.traces_dir)
    exporter["sources"] = list(EXPORT_SOURCES)
    exporter["output_dir"] = str(layout.dataset(dataset))
    return raw


# ── running ───────────────────────────────────────────────────────────


@dataclass
class StageOutcome:
    """What one stage did (``chain.json``'s entry)."""

    stage: str
    argv: list[list[str]] = field(default_factory=list)
    exit_codes: list[int | None] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    error: str | None = None
    #: The tokens the stage's new runs recorded — counted for a run stage only when the
    #: chain has a token limit.
    token_use: TokenUse | None = None

    def record(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "argv": self.argv,
            "exit_codes": self.exit_codes,
            "runs": self.runs,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
        }
        if self.token_use is not None:
            entry["token_use"] = self.token_use.model_dump(mode="json")
        return entry


@dataclass
class TokenLimitStop:
    """The chain stopped at its token limit (``chain.json``'s ``stopped_by_token_limit``)."""

    #: The run stage whose sweep reached the limit; ``None`` when the runs under the root had
    #: reached it before this run of the chain started.
    stage: str | None
    #: Every token the runs under the root recorded, when the chain stopped.
    tokens_used: int
    token_limit: int
    #: The selected run and export stages this run of the chain did not start.
    stages_not_started: list[str]
    #: Every recorded call under the root whose provider reported no usage (counted, no tokens).
    calls_without_usage: list[str]

    def record(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "tokens_used": self.tokens_used,
            "token_limit": self.token_limit,
            "stages_not_started": self.stages_not_started,
            "calls_without_usage": self.calls_without_usage,
        }

    def describe(self) -> str:
        """One line for a person: where, how many tokens of how many, what did not start."""
        where = (
            f"in stage {self.stage!r}"
            if self.stage is not None
            else "before this run of the chain started"
        )
        not_started = ", ".join(self.stages_not_started) or "none"
        without = len(self.calls_without_usage)
        named = " (named in chain.json)" if without else ""
        return (
            f"the runs under the chain root recorded {self.tokens_used} tokens, reaching the token "
            f"limit of {self.token_limit} {where}; stages not started: {not_started}; "
            f"{without} call(s) reported no usage{named}"
        )


@dataclass
class _TokenAccount:
    """The chain's tokens against its numeric limit: what the runs under the root recorded."""

    limit: int
    used: int
    calls_without_usage: list[str]

    @classmethod
    def opened(cls, layout: ChainLayout, limit: int) -> _TokenAccount:
        """The account of every run already under *layout*'s root (every role)."""
        chain = open_chain(layout)
        uses = [role_token_use(runs) for runs in chain.roles.values()]
        return cls(
            limit=limit,
            used=sum(use.tokens for use in uses),
            calls_without_usage=[call for use in uses for call in use.calls_without_usage],
        )

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    @property
    def reached(self) -> bool:
        return self.used >= self.limit

    def add(self, use: TokenUse | None) -> None:
        if use is not None:
            self.used += use.tokens
            self.calls_without_usage += use.calls_without_usage

    def stop(self, stage: str | None) -> TokenLimitStop:
        return TokenLimitStop(
            stage=stage,
            tokens_used=self.used,
            token_limit=self.limit,
            stages_not_started=[],
            calls_without_usage=list(self.calls_without_usage),
        )


@dataclass
class ChainResult:
    """What a chain run did: each stage it ran, the report when it computed one, a token-limit stop."""

    layout: ChainLayout
    stages: dict[str, StageOutcome]
    report: FlywheelReport | None = None
    #: Set when the chain stopped at its token limit (``chain.json``'s
    #: ``stopped_by_token_limit``).
    token_limit_stop: TokenLimitStop | None = None


#: Runs one CLI in this process: ``argv`` in, exit code out.
Entry = Callable[[list[str]], int]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_dirs(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {path.name for path in directory.iterdir() if (path / "manifest.json").is_file()}


def _invoke(stage: str, entry: Entry, argv: list[str]) -> int:
    try:
        return entry(argv)
    except SystemExit as exc:
        raise ChainStageError(stage, f"the CLI exited before running: {exc.code}") from exc
    except Exception as exc:
        raise ChainStageError(stage, f"{type(exc).__name__}: {exc}") from exc


def _bench_entry() -> Entry:
    from spectragent.core.benchmark_runner import main

    return main


def _export_entry() -> Entry:
    from spectragent.export.cli import main

    return main


def selected_stages(spec: ChainSpec, stages: Sequence[str] | None) -> list[str]:
    """The stages to run: all configured, or *stages* in chain order.

    Raises:
        ChainConfigError: *stages* names an unknown stage or one the spec
            does not configure.
    """
    configured = spec.configured()
    if stages is None:
        return configured
    unknown = sorted(set(stages) - set(STAGES))
    if unknown:
        raise ChainConfigError(f"unknown stage(s) {unknown}; the stages are {list(STAGES)}")
    missing = sorted(set(stages) - set(configured))
    if missing:
        raise ChainConfigError(f"stage(s) {missing} are not configured in the chain spec")
    return [stage for stage in STAGES if stage in stages]


def _write_record(
    layout: ChainLayout,
    spec: ChainSpec,
    outcomes: Mapping[str, StageOutcome],
    account: _TokenAccount | None = None,
    stop: TokenLimitStop | None = None,
) -> None:
    previous: dict[str, Any] = {}
    if layout.chain_record.is_file():
        try:
            loaded = json.loads(layout.chain_record.read_text(encoding="utf-8"))
            previous = loaded.get("stages") or {} if isinstance(loaded, dict) else {}
        except ValueError:
            previous = {}
    merged = {**previous, **{stage: outcome.record() for stage, outcome in outcomes.items()}}
    document: dict[str, Any] = {
        "name": spec.name,
        "evidence": spec.evidence,
        "token_limit": spec.token_limit,
        "stages": merged,
    }
    if account is not None:
        document["tokens_used"] = account.used
        document["stopped_by_token_limit"] = stop.record() if stop is not None else None
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.chain_record.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_chain(
    spec: ChainSpec,
    *,
    stages: Sequence[str] | None = None,
    log_level: str = "INFO",
    bench: Entry | None = None,
    export: Entry | None = None,
) -> ChainResult:
    """Run *spec*'s stages (all, or *stages*) in order; the report last.

    *bench* / *export* replace the in-process CLIs (a test's seam); by
    default they are ``spectragent-bench``'s and ``spectragent-export``'s
    ``main``. With a numeric ``token_limit``, the chain stops where its runs'
    tokens reach it (module docstring): no later run or export stage starts,
    and :attr:`ChainResult.token_limit_stop` says where.

    Raises:
        ChainConfigError: Before anything runs — the stage selection, a
            stage YAML without (or with two of) a block whose paths the chain
            sets, or a selected export whose dataset directory exists.
        ChainStageError: A stage failed; ``chain.json`` says which.
    """
    selected = selected_stages(spec, stages)
    layout = ChainLayout(spec.root.expanduser().resolve())
    effective: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for stage in selected:
        if stage in RUN_STAGES:
            stage_spec: StageSpec = getattr(spec, stage)
            effective[stage] = [
                (
                    layout.configs_dir / f"{stage}.yaml",
                    run_stage_config(stage, stage_spec.config, layout),
                )
            ]
        elif stage == "export":
            assert spec.export is not None
            datasets = DATASETS[: spec.export.repeat]
            existing = [name for name in datasets if layout.dataset(name).exists()]
            if existing:
                raise ChainConfigError(
                    f"stage 'export': {', '.join(str(layout.dataset(n)) for n in existing)} "
                    f"exists — an export never overwrites a dataset; remove it to export again "
                    f"(refused before any stage runs, so no stage is paid for in vain)"
                )
            effective[stage] = [
                (
                    layout.configs_dir / f"export_{dataset}.yaml",
                    export_stage_config(spec.export.config, layout, dataset),
                )
                for dataset in datasets
            ]
    bench_main = bench or _bench_entry()
    export_main = export or _export_entry()
    outcomes: dict[str, StageOutcome] = {}
    result = ChainResult(layout=layout, stages=outcomes)
    layout.configs_dir.mkdir(parents=True, exist_ok=True)
    limit = limit_value(spec.token_limit)
    account = _TokenAccount.opened(layout, limit) if limit is not None else None
    paid = [stage for stage in selected if stage != "report"]
    if account is not None and account.reached and paid:
        # Runs from an earlier run of the chain already used the limit up.
        result.token_limit_stop = account.stop(None)
        logger.warning(
            "chain: the runs under %s already recorded %d tokens, at or over the token limit of "
            "%d: no run or export stage starts",
            layout.root,
            account.used,
            account.limit,
        )
    for stage in selected:
        stop = result.token_limit_stop
        if stop is not None and stage != "report":
            stop.stages_not_started.append(stage)
            _write_record(layout, spec, outcomes, account, stop)
            continue
        outcome = outcomes[stage] = StageOutcome(stage=stage, started_at=_now())
        try:
            if stage in RUN_STAGES:
                try:
                    reached = _run_stage(
                        stage,
                        spec,
                        layout,
                        effective[stage][0],
                        outcome,
                        bench_main,
                        log_level,
                        remaining=account.remaining if account is not None else None,
                    )
                finally:
                    if account is not None:
                        account.add(outcome.token_use)
                if account is not None and (reached or account.reached):
                    result.token_limit_stop = account.stop(stage)
                    logger.warning(
                        "chain: stage %s brought the runs' tokens to %d of the token limit %d: "
                        "no later run or export stage starts",
                        stage,
                        account.used,
                        account.limit,
                    )
            elif stage == "export":
                _export_stage(effective[stage], outcome, export_main, log_level)
            else:
                result.report = _report_stage(spec, layout)
        except ChainStageError as exc:
            outcome.error = str(exc)
            raise
        finally:
            outcome.ended_at = _now()
            _write_record(layout, spec, outcomes, account, result.token_limit_stop)
    return result


def _write_yaml(path: Path, raw: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(dict(raw), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _run_stage(
    stage: str,
    spec: ChainSpec,
    layout: ChainLayout,
    config: tuple[Path, dict[str, Any]],
    outcome: StageOutcome,
    entry: Entry,
    log_level: str,
    *,
    remaining: int | None = None,
) -> bool:
    """Run one ``spectragent-bench`` stage; whether its sweep stopped at the token limit.

    *remaining* is what is left of the chain's token limit (``None``: the
    chain has none), handed to the bench as ``--token-limit``; with it, the
    tokens the stage's new runs recorded are counted into
    ``outcome.token_use`` — before any failure is raised, so ``chain.json``
    has them either way.
    """
    path, raw = config
    _write_yaml(path, raw)
    stage_spec: StageSpec = getattr(spec, stage)
    argv = ["--config", str(path), "--timeout", f"{stage_spec.timeout:g}", "--log-level", log_level]
    if stage in ("record", "control"):
        tasks = spec.tasks
        if tasks.benchmark:
            argv += ["--benchmark", tasks.benchmark]
        if tasks.subset:
            argv += ["--subset", tasks.subset]
        for task_id in tasks.task_ids or []:
            argv += ["--task-id", task_id]
        if tasks.limit is not None:
            argv += ["--limit", str(tasks.limit)]
    if stage_spec.evaluate and stage not in BRANCH_STAGES:
        argv += ["--evals-dir", str(layout.evals(stage))]
    else:
        argv.append("--no-eval")
    if remaining is not None:
        argv += ["--token-limit", str(remaining)]
    directory = layout.traces(stage)
    before = _run_dirs(directory)
    if before:
        logger.warning("chain: %s already holds runs; this stage adds to them", directory)
    outcome.argv.append(argv)
    code = _invoke(stage, entry, argv)
    outcome.exit_codes.append(code)
    new = sorted(_run_dirs(directory) - before)
    outcome.runs = [f"traces/{stage}/{name}" for name in new]
    if remaining is not None:
        outcome.token_use = _new_runs_token_use(layout, stage, new)
    from spectragent.core.benchmark_runner import TOKEN_LIMIT_EXIT_CODE, VETO_EXIT_CODE

    if code == VETO_EXIT_CODE:
        raise ChainStageError(
            stage,
            f"spectragent-bench exited {code}: a guard vetoed the stage, so the chain stops "
            f"here (the stage's summary and the vetoed run's trace say why)",
        )
    reached = remaining is not None and code == TOKEN_LIMIT_EXIT_CODE
    if code not in (0, 1) and not reached:
        raise ChainStageError(stage, f"spectragent-bench exited {code}")
    if not new:
        raise ChainStageError(
            stage, f"spectragent-bench exited {code} and wrote no run under {directory}"
        )
    return reached


def _new_runs_token_use(layout: ChainLayout, stage: str, names: Sequence[str]) -> TokenUse:
    """The tokens the runs *names* of *stage* recorded (a run that does not read back is named)."""
    role = open_role(layout.traces(stage), stage)
    wanted = set(names)
    skipped = {
        f"traces/{stage}/{rel}": reason for rel, reason in role.skipped.items() if rel in wanted
    }
    return token_use([run for run in role.runs if run.rel in wanted], skipped)


def build_spec_report(spec: ChainSpec) -> FlywheelReport:
    """The report of *spec*'s chain root: its evidence basis, planted credentials, name and token limit.

    What the chain's report stage and ``spectragent-flywheel report --spec``
    both compute, so the two give the same bytes.

    Raises:
        DatasetFormatError: A published dataset does not read back as this
            build's format (``build_report``).
    """
    return build_report(
        ChainLayout(spec.root.expanduser().resolve()),
        evidence=spec.evidence,
        planted=resolve_planted(spec.report.planted),
        name=spec.name,
        token_limit=spec.token_limit,
    )


def _report_stage(spec: ChainSpec, layout: ChainLayout) -> FlywheelReport:
    """Compute and write the report; anything it raises is this stage's failure, named.

    ``build_report`` skips a damaged run with its reason rather than raising,
    so what reaches here is a defect or an I/O error (the report directory
    cannot be written) — recorded in ``chain.json`` like any stage's.
    """
    try:
        report = build_spec_report(spec)
        write_report(report, layout.report_dir)
    except Exception as exc:
        raise ChainStageError("report", f"{type(exc).__name__}: {exc}") from exc
    return report


def _export_stage(
    configs: Sequence[tuple[Path, dict[str, Any]]],
    outcome: StageOutcome,
    entry: Entry,
    log_level: str,
) -> None:
    # An output that already existed was refused before any stage ran (``run_chain``); one
    # that appears since is the exporter's own refusal (a non-zero exit, nothing written).
    for path, raw in configs:
        _write_yaml(path, raw)
        argv = ["--config", str(path), "--log-level", log_level]
        outcome.argv.append(argv)
        code = _invoke("export", entry, argv)
        outcome.exit_codes.append(code)
        if code != 0:
            raise ChainStageError("export", f"spectragent-export exited {code} (it wrote nothing)")


__all__ = [
    "BRANCH_STAGES",
    "EXPORT_SOURCES",
    "RUN_STAGES",
    "STAGES",
    "ChainConfigError",
    "ChainResult",
    "ChainSpec",
    "ChainStageError",
    "ExportStageSpec",
    "PlantedSecret",
    "ReportSpec",
    "StageOutcome",
    "StageSpec",
    "TasksSpec",
    "TokenLimitStop",
    "build_spec_report",
    "export_stage_config",
    "resolve_planted",
    "run_chain",
    "run_stage_config",
    "selected_stages",
]
