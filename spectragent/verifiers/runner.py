"""VerificationRunner — judge recorded runs offline, each verdict into the run's own store.

The offline attachment point (``docs/architecture_design.md`` §4.10.1
"只有两个挂接点"): a benchmark's verifiers that need no live environment, run
over committed traces once the runs are over — by ``spectragent bench`` after
its sweep and by ``spectragent eval`` over whatever traces it is given. A
separate driver from the one that produces traces: that one needs a browser,
API keys and hours; this one needs a directory. You can point it at traces
recorded last month, on another machine, by a stack that no longer exists.

Every verdict is written into its run's verdict store
(``verdicts/<slug>.json``, ``core/trace/verdicts.py``), the one place verdicts
are kept: no copy anywhere else. A verifier that cannot judge a trace writes
nothing — the trace is still there to judge again — and the report says why.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.verdicts import write_verdict
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.restoration import BranchProvenance, BranchTaskVariant
from spectragent.schemas.verdict import JudgeUsage, Judgment, Verdict, VerifierIdentity
from spectragent.verifiers.base import OfflineVerifier, VerifierLifecycle, attachment_of
from spectragent.verifiers.errors import NoReferenceError, VerificationError
from spectragent.verifiers.trace_view import TraceView

logger = logging.getLogger(__name__)


def discover_traces(base_dir: Path) -> list[Path]:
    """Every committed trace directory directly under *base_dir*.

    A directory without ``manifest.json`` is not a trace — it is a
    half-written one, or something else entirely — and is skipped rather
    than crashing the pass.
    """
    if not base_dir.is_dir():
        return []
    return sorted(
        child
        for child in base_dir.iterdir()
        if child.is_dir() and (child / "manifest.json").is_file()
    )


@dataclass(frozen=True)
class VerificationOutcome:
    """One verifier on one trace: the verdict it wrote, or why it wrote none.

    Attributes:
        trace_dir: The run.
        task_id: The task the run ran (its directory name when the trace
            does not read back).
        verifier: Who was asked.
        verdict: The verdict written into the run's store.
        error: Why there is none: the verifier could not judge the trace, or
            the verdict could not be written.
        evidence: What a failed attempt left (prompts, partial replies).
        usage: What a failed attempt's judge model spent, as reported.
    """

    trace_dir: Path
    task_id: str
    verifier: VerifierIdentity
    verdict: Verdict | None = None
    error: str | None = None
    evidence: dict[str, JsonValue] | None = None
    usage: JudgeUsage | None = None

    @property
    def slug(self) -> str:
        return self.verifier.slug


@dataclass(frozen=True)
class VerificationReport:
    """Every verifier on every trace of one pass, in trace order then verifier order."""

    outcomes: tuple[VerificationOutcome, ...] = ()

    @property
    def errors(self) -> list[VerificationOutcome]:
        return [outcome for outcome in self.outcomes if outcome.error is not None]


@dataclass(frozen=True)
class _Reference:
    """What one trace is judged against, for the verifiers that need a reference row.

    ``row`` is the reference when there is one; ``refusal`` says, for a
    verifier id, why there is none; ``evidence`` is what every verdict on the
    trace records about it (the source task a branch was judged against, the
    task variant a branch ran).
    """

    row: EvalReference | None = None
    refusal: Callable[[str], Exception] | None = None
    evidence: dict[str, JsonValue] = field(default_factory=dict)


class VerificationRunner:
    """Run offline verifiers over traces, writing each verdict into its run's store.

    Args:
        verifiers: The verifiers, each attached offline (needing no live
            environment).
        reference_loader: The benchmark's reference data (its
            ``reference_loader()``); needed only by a verifier that needs a
            reference row.
        verify_reference_identity: Check that each trace ran the benchmark
            and subset of *reference_loader* before reading its row
            (``spectragent eval``, which is given traces it did not produce).
            A branch run is always checked against its source's.

    Raises:
        ValueError: A verifier is attached live, needs a reference row and
            there is no reference data, or shares its slug with another (one
            verdict file for two verifiers) — before any trace is read.
    """

    def __init__(
        self,
        *,
        verifiers: Sequence[OfflineVerifier],
        reference_loader: BaseEvalLoader | None = None,
        verify_reference_identity: bool = False,
    ) -> None:
        for verifier in verifiers:
            if attachment_of(verifier) != "offline":
                raise ValueError(
                    f"verifier {verifier.verifier_id!r} needs the live environment, so it judges "
                    f"at the scoring point, never from a recorded trace"
                )
            if "reference" in verifier.needs and reference_loader is None:
                raise ValueError(
                    f"verifier {verifier.verifier_id!r} judges against the task's reference row, "
                    f"and the benchmark has no reference data"
                )
        self._verifiers = list(verifiers)
        # Read once, up front: a verifier whose identity cannot be read is
        # refused here, never recorded under a guessed one.
        self._identities = [verifier.identity() for verifier in self._verifiers]
        slugs = [identity.slug for identity in self._identities]
        if repeated := sorted({slug for slug in slugs if slugs.count(slug) > 1}):
            raise ValueError(
                f"verifier(s) {', '.join(repeated)} listed more than once with the same "
                f"settings: each would overwrite the other's verdict"
            )
        self._loader = reference_loader
        self._verify_reference_identity = verify_reference_identity

    @property
    def identities(self) -> list[VerifierIdentity]:
        """Who judges, in verifier order."""
        return list(self._identities)

    async def run(self, trace_dirs: Sequence[Path]) -> VerificationReport:
        """Judge every trace with every verifier, then release the verifiers' resources.

        Only the traces given are judged: no task of the benchmark that was
        not run is counted as anything.
        """
        try:
            outcomes: list[VerificationOutcome] = []
            for trace_dir in trace_dirs:
                outcomes.extend(await self._judge(trace_dir))
            return VerificationReport(tuple(outcomes))
        finally:
            await self.aclose()

    async def aclose(self, *, timeout: float = 5.0) -> None:
        """Release what each verifier owns (its judge model's client), each once."""
        seen: set[int] = set()
        for verifier in self._verifiers:
            if id(verifier) in seen or not isinstance(verifier, VerifierLifecycle):
                continue
            seen.add(id(verifier))
            # Judge verifiers delegate to close_models, which isolates cleanup
            # diagnostics from verdicts and preserves external cancellation.
            try:
                await verifier.aclose(timeout=timeout)
            except Exception:
                logger.exception("Verifier cleanup failed for %s", verifier.verifier_id)

    # ── internals ───────────────────────────────────────────────────

    async def _judge(self, trace_dir: Path) -> list[VerificationOutcome]:
        """Every verifier on one trace; a trace that does not read back fails them all."""
        try:
            trace = RunTrace(trace_dir)
            view = TraceView(trace)
            task_id = view.task_id
            reference = self._reference(trace, view)
        except Exception as exc:
            logger.exception("Verification cannot read %s", trace_dir)
            task_id = _task_id_or_dirname(trace_dir)
            message = f"{type(exc).__name__}: {exc}"
            return [
                VerificationOutcome(trace_dir, task_id, identity, error=message)
                for identity in self._identities
            ]
        return [
            await self._judge_with(verifier, identity, trace, task_id, reference)
            for verifier, identity in zip(self._verifiers, self._identities, strict=True)
        ]

    async def _judge_with(
        self,
        verifier: OfflineVerifier,
        identity: VerifierIdentity,
        trace: RunTrace,
        task_id: str,
        reference: _Reference,
    ) -> VerificationOutcome:
        """One verifier on one trace: judged, timed, written — or the error, with what it left."""
        needs_row = "reference" in verifier.needs
        started_at, started = time.time(), time.monotonic()
        try:
            if needs_row and reference.row is None:
                assert reference.refusal is not None
                raise reference.refusal(verifier.verifier_id)
            judgment = await verifier.verify(trace, reference.row if needs_row else None)
            if not isinstance(judgment, Judgment):
                raise TypeError(f"verify() returned {type(judgment).__name__}, not a Judgment")
            evidence = dict(reference.evidence)
            if not needs_row:
                # The source task's row was not read by this verifier.
                evidence.pop("source_reference", None)
            verdict = Verdict.judged(
                identity,
                "offline",
                judgment,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                evidence=evidence,
            )
            write_verdict(trace.trace_dir, verdict)
        except Exception as exc:
            logger.exception("Verifier %s failed on %s", identity.slug, trace.trace_dir)
            return VerificationOutcome(
                trace.trace_dir,
                task_id,
                identity,
                error=f"{type(exc).__name__}: {exc}",
                evidence=exc.evidence if isinstance(exc, VerificationError) else None,
                usage=exc.usage if isinstance(exc, VerificationError) else None,
            )
        return VerificationOutcome(trace.trace_dir, task_id, identity, verdict=verdict)

    def _reference(self, trace: RunTrace, view: TraceView) -> _Reference:
        """What *trace* is judged against: its task's reference row, or why there is none.

        A branch run is judged against its source task's row
        (``BranchProvenance``), and keeps its own identity. A task-variant
        branch ran a task no benchmark reference describes, so no row is
        ever looked up for it: a verifier that judges against one cannot
        judge it (:class:`NoReferenceError`), one that needs none judges the
        variant's own text.
        """
        branch = trace.config.branch
        if branch is not None and branch.task_variant is not None:
            variant = branch.task_variant
            return _Reference(
                refusal=lambda verifier_id: _no_variant_reference(variant, branch, verifier_id),
                evidence={"task_variant": _variant_record(variant, branch)},
            )
        if self._loader is None or not any("reference" in v.needs for v in self._verifiers):
            return _Reference()
        if branch is not None:
            task_id = branch.source_task_id
            checks = (("benchmark_name", branch.source_benchmark), ("subset", branch.source_subset))
            label = "Branch reference"
        else:
            task_id = view.task_id
            checks = (
                (("benchmark_name", view.benchmark), ("subset", view.subset))
                if self._verify_reference_identity
                else ()
            )
            label = "Trace reference"
        for attribute, expected in checks:
            actual = getattr(self._loader, attribute, None)
            if not expected or actual != expected:
                problem = ValueError(
                    f"{label} {attribute}={expected!r} does not match the reference data's "
                    f"{actual!r}"
                )
                return _Reference(refusal=lambda _id, problem=problem: problem)
        row = self._loader.get(task_id)
        if row is None:
            missing = KeyError(
                f"Trace {trace.trace_dir} ran task_id {task_id!r}, which is not in the reference "
                f"data {getattr(self._loader, 'benchmark_name', '?')!r} (subset "
                f"{getattr(self._loader, 'subset', '?')!r}). The trace and the reference set "
                f"disagree about which split ran."
            )
            return _Reference(refusal=lambda _id: missing)
        evidence: dict[str, JsonValue] = {}
        if branch is not None:
            evidence["source_reference"] = {
                "benchmark": branch.source_benchmark,
                "subset": branch.source_subset,
                "task_id": branch.source_task_id,
            }
        return _Reference(row=row, evidence=evidence)


def _no_variant_reference(
    variant: BranchTaskVariant, branch: BranchProvenance, verifier_id: str
) -> NoReferenceError:
    source = "/".join(
        part or "-"
        for part in (branch.source_benchmark, branch.source_subset, branch.source_task_id)
    )
    return NoReferenceError(
        f"task variant {variant.variant_id!r}: no reference for this task. The branch ran a "
        f"variant of source task {source}, whose reference describes the source's task, not "
        f"this one; {verifier_id!r} judges against a benchmark reference, so this trace is not "
        f"judged by it"
    )


def _variant_record(variant: BranchTaskVariant, branch: BranchProvenance) -> dict[str, JsonValue]:
    """What a verdict on a task-variant branch says about its task: the variant, no reference."""
    return {
        **variant.model_dump(mode="json"),
        "source_task": {
            "benchmark": branch.source_benchmark,
            "subset": branch.source_subset,
            "task_id": branch.source_task_id,
        },
        "reference": None,
    }


def _task_id_or_dirname(trace_dir: Path) -> str:
    try:
        return TraceView(RunTrace(trace_dir)).task_id
    except Exception:
        return trace_dir.name


__all__ = [
    "VerificationOutcome",
    "VerificationReport",
    "VerificationRunner",
    "discover_traces",
]
