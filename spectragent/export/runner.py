"""``ExportRunner``: recorded runs in, a validated dataset directory out (arch §4.15 "流水线").

Not registered — the counterpart of ``EvaluationRunner``. Synchronous (the
rendering stage runs in one ``asyncio.run``), with no environment to drive and
no model to call except the exporter's thought rewriter under
``wait_actions: drop_when_capped``. It reaches the export's strategies only
through the configured :class:`~spectragent.export._registrar.Exporter` and
its receptors, never a concrete class:

1. **sources** — the runs ``sources`` matches under ``sources_root``, by
   relative path, read through ``RunTrace`` (``filters.open_sources``), then
   the filters, then decontamination (the benchmarks ``against`` lists;
   benchmark data read through the document's loaders, ``benchmarks.py``) and
   the split key (``filters``, ``split``), and each kept source's policy
   steps captured before their initial load finished
   (``filters.exclude_unloaded_steps``: rendered as context, never a sample,
   each listed in the manifest's ``steps.filtered``);
2. **canonical samples** — ``canonical.canonicalise`` per kept run (a branch
   run labelled by its source run's verdicts); under ``wait_actions:
   drop_when_capped`` the waits on capped captures leave them
   (``waits.capped_waits``, ``waits.without_waits``);
3. **translation** — ``exporter.translator.translate`` per sample;
4. **student rendering** — ``render.StudentRenderer`` per trajectory, after
   ``waits.WaitShaper`` (``drop_when_capped``) took the removed steps out of
   its history and rewrote the affected thoughts;
5. **format** — ``exporter.format`` per rendered sample, images stored once;
6. **validation** — ``validate``: images, coordinates, reference leaks,
   length, then credentials over every text byte about to be written;
7. **split, duplicates, manifest** (``schemas/dataset.py::DatasetManifest``,
   the model every reader of the dataset reads it back through), then the
   write.

Everything is written to ``<output_dir>.partial`` and renamed to
``output_dir`` only when every check passed; any failure removes the
``.partial`` directory — never half a dataset. An existing ``output_dir`` is
refused. No wall-clock field, absolute path or machine name reaches the
output, and two exports of the same input are byte-identical.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spectragent.agents._registrar import AgentRegistrar
from spectragent.core.archive import canonical_json
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.sources import (
    TaskIdentity,
    task_identity,
    unloaded_capture_reason,
    unreadable_reason,
)
from spectragent.export._registrar import Exporter, ExportRecord
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.canonical import canonicalise
from spectragent.export.errors import ExportConfigError, ExportEmptyError
from spectragent.export.filters import Source, filtered_out, open_sources
from spectragent.export.render import RenderedSample, StudentRenderer, TrajectoryStep
from spectragent.export.settings import thought_rewrite_problem
from spectragent.export.split import Decontaminator, Split, split_key, split_of
from spectragent.export.validate import (
    ApproxTokenEstimator,
    ReferenceScanner,
    check_coordinates,
    check_credentials,
    check_images,
    check_length,
)
from spectragent.export.waits import (
    REWRITE_PROMPT_DIGEST,
    REWRITE_PROMPT_VERSION,
    StepWaits,
    ThoughtRewriter,
    WaitShaper,
    capped_waits,
    without_waits,
)
from spectragent.schemas.dataset import (
    DATASET_FORMAT,
    BenchmarkEntry,
    CaptureLedger,
    DatasetManifest,
    DecontaminationLedger,
    ExportedSource,
    LeakScanLedger,
    RemovedActionEntry,
    RemovedStepEntry,
    RewriterEntry,
    SourceLedger,
    SplitLedger,
    StepLedger,
    StudentSummary,
    ThoughtLedger,
    WaitLedger,
)
from spectragent.schemas.observation.image import image_suffix
from spectragent.schemas.sample import CanonicalStepSample
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: Where a dataset is assembled before the rename that publishes it.
PARTIAL_SUFFIX = ".partial"
#: Suffixes by media type, for image bytes whose header names no known type.
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


class FileImageStore:
    """``image_store: files``: each distinct image once, as ``images/<sha256><suffix>``."""

    def __init__(self, dataset_root: Path) -> None:
        self._root = dataset_root
        self._stored: set[str] = set()

    def put(self, data: bytes, media_type: str) -> str:
        suffix = image_suffix(data) or _SUFFIXES.get(media_type, ".bin")
        path = f"images/{hashlib.sha256(data).hexdigest()}{suffix}"
        if path not in self._stored:
            target = self._root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self._stored.add(path)
        return path

    @property
    def stored(self) -> frozenset[str]:
        return frozenset(self._stored)


@dataclass(frozen=True)
class ExportReport:
    """What one export wrote.

    Attributes:
        output_dir: The dataset directory.
        samples: Split → number of samples.
        drops: Untranslatable reason → emitted steps not exported as samples.
        manifest: The dataset manifest, as written.
        thought_rewrites: Under ``wait_actions: drop_when_capped``, how this
            run got its rewritten thoughts: ``cache_hits`` (distinct rewrites
            read back from ``thought_rewrite_cache``) and ``model_calls``.
            Empty when the export has no rewriter (``wait_actions: keep``, or
            ``thought_source: none``). A fact of this run, not of the dataset,
            so it is not in the manifest (arch §4.15 "等待动作").
    """

    output_dir: Path
    samples: Mapping[str, int]
    drops: Mapping[str, int]
    manifest: DatasetManifest
    thought_rewrites: Mapping[str, int] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class _Kept:
    """A source the filters kept, with its task and split.

    ``unloaded``: its policy steps ``filters.exclude_unloaded_steps`` leaves
    out of the samples, each with the reason.
    """

    source: Source
    identity: TaskIdentity
    key: str
    split: Split
    unloaded: Mapping[int, str]


@dataclass
class _Selection:
    kept: list[_Kept]
    readable: dict[str, Source]
    skipped: dict[str, str]
    filtered: dict[str, str]
    decontaminator: Decontaminator
    #: ``<source path>#s<step>`` → why the step's sample was left out; filled
    #: while rendering, where it is known which steps have a sample.
    filtered_steps: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class _Prepared:
    """One run's samples, translated, and the waits removed from its steps."""

    source: Source
    samples: dict[int, CanonicalStepSample]
    waits: dict[int, StepWaits]

    def step(self, record: StepRecord, *, emit: bool = True) -> TrajectoryStep:
        """*record* as a step of a trajectory: without its removed waits, or removed."""
        waits = self.waits.get(record.step)
        if waits is not None and waits.removes_step:
            return TrajectoryStep(
                self.source.trace, self.source.rel, waits.record, None, emit, waits.removed
            )
        return TrajectoryStep(
            self.source.trace,
            self.source.rel,
            waits.record if waits is not None else record,
            self.samples.get(record.step),
            emit,
        )


@dataclass
class _Rendering:
    """What the rendering stage leaves for the manifest besides the samples."""

    prepared: dict[str, _Prepared] = dataclasses.field(default_factory=dict)
    shaper: WaitShaper | None = None
    rewriter: ThoughtRewriter | None = None


class ExportRunner:
    """Run one configured export (module docstring).

    Args:
        exporter: The composed exporter — its job fields and ``translator``.
        composer: Composes the student's run configuration (the CLI's own,
            built from ``collect_registrars()``).
        benchmarks: The benchmark loaders the export document configures
            (decontamination and the leak scan read through them).
    """

    def __init__(
        self, exporter: Exporter, *, composer: ConfigComposer, benchmarks: BenchmarkLoaders
    ) -> None:
        self.exporter = exporter
        self.composer = composer
        self.benchmarks = benchmarks

    @classmethod
    def from_run(cls, run: ComposedRun, *, composer: ConfigComposer, origin: str) -> ExportRunner:
        """The export a composed document configures: its one exporter and its benchmark loaders.

        Raises:
            ExportConfigError: *origin* (the document, for the message)
                configures no exporter or more than one.
        """
        exporters = run.instances("exporter")
        if len(exporters) != 1:
            raise ExportConfigError(
                f"{origin} must configure exactly one exporter, it configures {len(exporters)}"
            )
        return cls(exporters[0], composer=composer, benchmarks=BenchmarkLoaders.from_run(run))

    def run(self) -> ExportReport:
        """Export, validate and publish the dataset.

        Raises:
            ExportConfigError: ``output_dir`` exists, or the configuration
                cannot run as given.
            ExportEmptyError: Nothing to export (the counts say why).
            ExportError: A refused step, a failed check, a capture mismatch …
                — nothing is left behind.
        """
        exporter = self.exporter
        output = Path(exporter.output_dir).expanduser()
        if output.exists():
            raise ExportConfigError(
                f"output_dir {exporter.output_dir} exists; an export never overwrites a "
                f"dataset — remove it or choose another"
            )
        partial = output.with_name(output.name + PARTIAL_SUFFIX)
        rewriter = self._rewriter()
        renderer = StudentRenderer.from_config(
            Path(exporter.student_run_config).expanduser(),
            composer=self.composer,
            untranslatable=exporter.translator.untranslatable,
            thought_source=exporter.thought_source,
            thought_index_refs=exporter.thought_index_refs,
        )
        selection = self._select()
        rendering = _Rendering(
            shaper=WaitShaper(rewriter) if exporter.wait_actions == "drop_when_capped" else None,
            rewriter=rewriter,
        )
        rendered = asyncio.run(self._render(selection, renderer, rendering))
        if not rendered:
            raise ExportEmptyError(_empty_counts(selection, renderer, rendering))
        rendered = _mark_duplicates(rendered)
        if partial.exists():
            logger.info("removing %s, left by an earlier export that did not finish", partial)
            shutil.rmtree(partial)
        try:
            partial.mkdir(parents=True)
            manifest, samples = self._write(partial, selection, renderer, rendered, rendering)
            partial.rename(output)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return ExportReport(
            output_dir=output,
            samples=samples,
            drops=dict(sorted(renderer.stats.untranslatable.items())),
            manifest=manifest,
            thought_rewrites=(
                {"cache_hits": rewriter.cache_hits, "model_calls": rewriter.model_calls}
                if rewriter is not None
                else {}
            ),
        )

    def _rewriter(self) -> ThoughtRewriter | None:
        """The thought rewriter ``wait_actions: drop_when_capped`` needs, or ``None``.

        The exporter checked the rule when it was built; the receptor may have
        been filled after that, so it is checked again on what arrived.

        Raises:
            ExportConfigError: ``thought_rewrite_llms`` / ``thought_rewrite_cache``
                disagree with ``wait_actions`` and ``thought_source``.
        """
        exporter = self.exporter
        models = list(exporter.thought_rewrite_llms)
        problem = thought_rewrite_problem(
            wait_actions=exporter.wait_actions,
            thought_source=exporter.thought_source,
            models=len(models),
            cache=exporter.thought_rewrite_cache,
        )
        if problem is not None:
            raise ExportConfigError(f"{exporter.exporter_id}: {problem}")
        if not models or exporter.thought_rewrite_cache is None:
            return None
        return ThoughtRewriter(models[0], Path(exporter.thought_rewrite_cache).expanduser())

    # ── 1. sources ───────────────────────────────────────────

    def _select(self) -> _Selection:
        exporter = self.exporter
        sources, skipped = open_sources(exporter.root(), exporter.sources)
        readable = {source.run_id: source for source in sources}
        filtered: dict[str, str] = {}
        selected: list[tuple[Source, TaskIdentity]] = []
        for source in sources:
            try:
                reason = filtered_out(source, exporter.filters, readable)
            except TraceSchemaError as exc:
                logger.warning("export: skipping %s, it does not read back: %s", source.rel, exc)
                skipped[source.rel] = unreadable_reason(source.trace.trace_dir, exc)
                continue
            if reason is not None:
                filtered[source.rel] = reason
                continue
            selected.append((source, task_identity(source.config)))

        decontaminator = Decontaminator.configure(exporter.decontamination, loaders=self.benchmarks)
        if not decontaminator.decontaminated:
            logger.info(
                "decontamination.against lists no benchmark: sources are not checked against "
                "any evaluation benchmark (the manifest says decontaminated: false)"
            )
        kept: list[_Kept] = []
        for source, identity in selected:
            reason = decontaminator.reason(identity)
            if reason is not None:
                filtered[source.rel] = reason
                continue
            key = split_key(identity, exporter.split) if identity.task_id or identity.site else None
            if key is None:
                skipped[source.rel] = (
                    f"no split key (split.by: {exporter.split.by} — the run recorded no "
                    f"{'start_url' if exporter.split.by == 'site' else 'task id'})"
                )
                continue
            try:
                unloaded = self._unloaded(source)
            except TraceSchemaError as exc:
                logger.warning("export: skipping %s, it does not read back: %s", source.rel, exc)
                skipped[source.rel] = unreadable_reason(source.trace.trace_dir, exc)
                continue
            kept.append(_Kept(source, identity, key, split_of(key, exporter.split), unloaded))
        return _Selection(kept, readable, skipped, filtered, decontaminator)

    def _unloaded(self, source: Source) -> dict[int, str]:
        """*source*'s policy steps captured before their initial load finished, with the reason.

        Empty when ``filters.exclude_unloaded_steps`` is off. Only a step whose
        recorded capture state says ``loaded: false`` is in it
        (``core/trace/sources.py::unloaded_capture_reason``); one with no such
        evidence is exported.

        Raises:
            TraceSchemaError: A recorded capture state does not read back.
        """
        if not self.exporter.filters.exclude_unloaded_steps:
            return {}
        trace = source.trace
        unloaded: dict[int, str] = {}
        for record in trace.step_records:
            if record.origin != "policy" or record.step > trace.manifest.complete_through:
                continue
            reason = unloaded_capture_reason(trace, record.step)
            if reason is not None:
                unloaded[record.step] = f"{reason} (exclude_unloaded_steps)"
        return unloaded

    # ── 2-4. canonical samples, translation, rendering ───────

    async def _render(
        self, selection: _Selection, renderer: StudentRenderer, rendering: _Rendering
    ) -> list[RenderedSample]:
        try:
            return await self._render_trajectories(selection, renderer, rendering)
        finally:
            if rendering.rewriter is not None:
                await rendering.rewriter.aclose()

    async def _render_trajectories(
        self, selection: _Selection, renderer: StudentRenderer, rendering: _Rendering
    ) -> list[RenderedSample]:
        exporter = self.exporter
        drop_waits = exporter.wait_actions == "drop_when_capped"

        def prepared(source: Source, verdicts_from: Source | None = None) -> _Prepared:
            if source.run_id not in rendering.prepared:
                teacher = AgentRegistrar.get_or_none(source.config.agent_id)
                samples = canonicalise(
                    source.trace,
                    rel_path=source.rel,
                    thought_source=exporter.thought_source,
                    teacher_cls=teacher,
                    verdicts_from=verdicts_from.trace if verdicts_from is not None else None,
                )
                waits = capped_waits(source.trace) if drop_waits else {}
                translator = exporter.translator
                translated: dict[int, CanonicalStepSample] = {}
                for sample in samples:
                    step_waits = waits.get(sample.step)
                    if step_waits is not None:
                        if step_waits.removes_step:
                            continue
                        sample = without_waits(sample, step_waits)
                    translated[sample.step] = translator.translate(sample, source.trace).applied(
                        sample
                    )
                rendering.prepared[source.run_id] = _Prepared(source, translated, waits)
            return rendering.prepared[source.run_id]

        def own_step(kept: _Kept, own: _Prepared, record: StepRecord) -> TrajectoryStep:
            """One of *kept*'s own steps: a sample unless a step filter leaves it out.

            A step that left the trajectory (only waits on a capped capture)
            has no sample, so no filter applies to it.
            """
            source = kept.source
            sample = own.samples.get(record.step)
            why = kept.unloaded.get(record.step) if sample is not None else None
            if why is not None:
                # Rendered into the context as it happened; never a sample.
                selection.filtered_steps[f"{source.rel}#s{record.step}"] = why
            return own.step(record, emit=why is None)

        rendered: list[RenderedSample] = []
        for kept in selection.kept:
            source = kept.source
            renderer.check_source(source.trace, source.rel)
            branch = source.config.branch
            steps: list[TrajectoryStep] = []
            if branch is not None:
                origin = selection.readable[branch.source_run_id]
                renderer.check_source(origin.trace, origin.rel)
                prefix = prepared(origin)
                steps += [
                    prefix.step(record, emit=False)
                    for record in origin.trace.step_records
                    if record.step < branch.checkpoint_step
                    and record.step <= origin.trace.manifest.complete_through
                ]
                own = prepared(source, verdicts_from=origin)
                steps += [
                    own_step(kept, own, record)
                    for record in source.trace.step_records
                    if record.origin == "policy"
                    and record.step >= branch.checkpoint_step
                    and record.step <= source.trace.manifest.complete_through
                ]
            else:
                own = prepared(source)
                steps += [
                    own_step(kept, own, record)
                    for record in source.trace.step_records
                    if record.step <= source.trace.manifest.complete_through
                ]
            if rendering.shaper is not None:
                steps = await rendering.shaper.shape(steps)
            rendered.extend(await renderer.render(steps))
        return rendered

    # ── 5-7. format, validate, write ─────────────────────────

    def _write(
        self,
        root: Path,
        selection: _Selection,
        renderer: StudentRenderer,
        rendered: Sequence[RenderedSample],
        rendering: _Rendering,
    ) -> tuple[DatasetManifest, dict[str, int]]:
        exporter = self.exporter
        kept_by_rel = {kept.source.rel: kept for kept in selection.kept}
        store = FileImageStore(root)
        scanner = ReferenceScanner(self.benchmarks)
        estimator = (
            ApproxTokenEstimator.from_budget(exporter.length)
            if exporter.length is not None
            else None
        )
        records: dict[str, list[str]] = {}
        sample_lines: dict[str, list[str]] = {}
        per_source: Counter[str] = Counter()
        for item in rendered:
            kept = kept_by_rel[item.source]
            record = exporter.format(item, store)
            check_images(record, item, root)
            check_coordinates(item, renderer.coordinate_space)
            identity = kept.identity
            scanner.check(
                item,
                benchmark=identity.benchmark,
                subset=identity.subset,
                task_id=identity.task_id,
            )
            if estimator is not None and exporter.length is not None:
                check_length(item, estimator, exporter.length.max_tokens)
            records.setdefault(kept.split, []).append(canonical_json(record.record))
            sample_lines.setdefault(kept.split, []).append(
                canonical_json(_sample_line(item, record, kept.split))
            )
            per_source[item.source] += 1

        samples = {split: len(lines) for split, lines in sorted(records.items())}
        written = json.dumps(
            self._manifest(
                selection, renderer, rendered, samples, per_source, scanner, rendering
            ).model_dump(mode="json"),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        # Read back from the bytes written, so the report's manifest is exactly the
        # one on disk — through the model every reader uses.
        manifest = DatasetManifest.model_validate_json(written)
        files: dict[str, bytes] = {}
        for split, lines in records.items():
            files[f"{split}.jsonl"] = ("\n".join(lines) + "\n").encode("utf-8")
            files[f"{split}.samples.jsonl"] = ("\n".join(sample_lines[split]) + "\n").encode(
                "utf-8"
            )
        files["manifest.json"] = (written + "\n").encode("utf-8")
        check_credentials(files)
        for name in sorted(files):
            (root / name).write_bytes(files[name])
        return manifest, samples

    def _manifest(
        self,
        selection: _Selection,
        renderer: StudentRenderer,
        rendered: Sequence[RenderedSample],
        samples: Mapping[str, int],
        per_source: Mapping[str, int],
        scanner: ReferenceScanner,
        rendering: _Rendering,
    ) -> DatasetManifest:
        exporter = self.exporter
        stats = renderer.stats
        decontaminator = selection.decontaminator
        return DatasetManifest(
            format=DATASET_FORMAT,
            exporter=exporter_settings(exporter),
            translator=translator_settings(exporter.translator),
            student=StudentSummary(
                config_sha256=renderer.config_sha256,
                agent_id=renderer.agent_id,
                loop_id=renderer.loop_id,
                coordinate_space=renderer.coordinate_space,
            ),
            sources=SourceLedger(
                exported=[
                    ExportedSource(
                        path=kept.source.rel,
                        run_id=kept.source.run_id,
                        config_hash=kept.source.trace.manifest.config_hash,
                        samples=per_source.get(kept.source.rel, 0),
                    )
                    for kept in selection.kept
                ],
                skipped=dict(sorted(selection.skipped.items())),
                filtered=dict(sorted(selection.filtered.items())),
            ),
            steps=StepLedger(
                untranslatable=dict(sorted(stats.untranslatable.items())),
                skipped=dict(sorted(stats.skipped.items())),
                filtered=dict(sorted(selection.filtered_steps.items())),
            ),
            samples=sum(samples.values()),
            quantization_px={str(error): n for error, n in sorted(stats.quantization.items())},
            thoughts=ThoughtLedger(
                source=exporter.thought_source,
                fields=dict(sorted(stats.thought_fields.items())),
                dropped_index_refs=stats.thoughts_dropped,
            ),
            step_info=dict(sorted(stats.step_info.items())),
            capture=CaptureLedger(unchecked=sorted(stats.capture_unchecked)),
            split=SplitLedger(
                by=exporter.split.by,
                validation_fraction=exporter.split.validation_fraction,
                salt=exporter.split.salt,
                samples=dict(samples),
                keys={kept.key: kept.split for kept in sorted(selection.kept, key=_by_key)},
            ),
            decontamination=DecontaminationLedger(
                decontaminated=decontaminator.decontaminated,
                against=[
                    BenchmarkEntry(benchmark=ref.benchmark, subset=ref.subset)
                    for ref in decontaminator.against
                ],
                match=decontaminator.match,
                excluded=sum(
                    1
                    for reason in selection.filtered.values()
                    if reason.startswith("decontaminated")
                ),
            ),
            leak_scan=LeakScanLedger(
                compared=scanner.compared,
                no_eval_loader=dict(sorted(scanner.no_eval_loader.items())),
                no_reference_answer=dict(sorted(scanner.no_reference_answer.items())),
            ),
            duplicates=sum(
                1 for item in rendered if item.sample.branch and item.sample.branch.duplicate_of
            ),
            wait_actions=_wait_ledger(exporter.wait_actions, rendering, rendered),
        )


def exporter_settings(exporter: Exporter) -> dict[str, Any]:
    """The exporter's knobs as the manifest records them — no path (they are machine-specific)."""
    return {
        "exporter_id": exporter.exporter_id,
        "sample": exporter.sample,
        "image_store": exporter.image_store,
        "sources": list(exporter.sources),
        "filters": dataclasses.asdict(exporter.filters),
        "split": dataclasses.asdict(exporter.split),
        "decontamination": dataclasses.asdict(exporter.decontamination),
        "thought_source": exporter.thought_source,
        "thought_index_refs": exporter.thought_index_refs,
        "wait_actions": exporter.wait_actions,
        "length": dataclasses.asdict(exporter.length) if exporter.length is not None else None,
    }


def translator_settings(translator: Any) -> dict[str, Any]:
    """The translator's key and its scalar knobs."""
    knobs = {
        name: value
        for name, value in sorted(vars(translator).items())
        if not name.startswith("_") and isinstance(value, str | int | float | bool | type(None))
    }
    return {"translator_id": translator.translator_id, **knobs}


def _sample_line(item: RenderedSample, record: ExportRecord, split: Split) -> dict[str, Any]:
    """One line of ``<split>.samples.jsonl``: the canonical sample and how it was rendered."""
    return {
        "sample": item.sample.model_dump(mode="json"),
        "render": {
            "split": split,
            "segment": item.segment,
            "context_steps": list(item.context_steps),
            "step_info": item.step_info,
            "target": item.target,
            "target_actions": [action.model_dump(mode="json") for action in item.target_actions],
            "quantization_px": list(item.quantization),
            "thought": item.thought.model_dump(mode="json") if item.thought else None,
            "thought_dropped": item.thought_dropped,
            "claim_not_written": item.claim_not_written,
            "images": list(record.images),
        },
    }


def _mark_duplicates(rendered: Sequence[RenderedSample]) -> list[RenderedSample]:
    """Flag later branch samples whose checkpoint-step target repeats an earlier one in the group.

    Compared on the translated actions (names and params, not the thought);
    flagged with ``duplicate_of = <the earlier sample_id>``, never removed.
    """
    first: dict[tuple[str, str], str] = {}
    marked: list[RenderedSample] = []
    for item in rendered:
        branch = item.sample.branch
        if branch is None or item.sample.step != branch.checkpoint_step:
            marked.append(item)
            continue
        target = canonical_json(
            [action.model_dump(mode="json") for action in item.sample.actions or []]
        )
        key = (branch.branch_group_id, target)
        earlier = first.setdefault(key, item.sample.sample_id)
        if earlier == item.sample.sample_id:
            marked.append(item)
            continue
        sample = item.sample.model_copy(
            update={"branch": branch.model_copy(update={"duplicate_of": earlier})}
        )
        marked.append(dataclasses.replace(item, sample=sample))
    return marked


def _wait_ledger(
    policy: str, rendering: _Rendering, rendered: Sequence[RenderedSample]
) -> WaitLedger:
    """The manifest's ``wait_actions``: every removal once per run; the exported samples' rewrites."""
    removed_steps: list[RemovedStepEntry] = []
    removed_actions: list[RemovedActionEntry] = []
    for prepared in sorted(rendering.prepared.values(), key=lambda p: p.source.rel):
        source = prepared.source
        for step in sorted(prepared.waits):
            waits = prepared.waits[step]
            indices = [removed.action_index for removed in waits.removed]
            if waits.removes_step:
                removed_steps.append(
                    RemovedStepEntry(
                        source=source.rel, run_id=source.run_id, step=step, action_indices=indices
                    )
                )
                continue
            removed_actions += [
                RemovedActionEntry(
                    source=source.rel, run_id=source.run_id, step=step, action_index=index
                )
                for index in indices
            ]
    outcomes = Counter(
        removal.thought.outcome
        for item in rendered
        if (removal := item.sample.wait_removal) is not None and removal.thought is not None
    )
    rewriter = rendering.rewriter
    asked = rewriter is not None and (rewriter.cache_hits or rewriter.model_calls)
    return WaitLedger(
        policy=policy,
        removed_steps=removed_steps,
        removed_actions=removed_actions,
        rewritten=outcomes["rewritten"],
        reviewed_unchanged=outcomes["reviewed_unchanged"],
        rewriter=(
            RewriterEntry(
                **rewriter.identity.model_dump(),
                prompt_version=REWRITE_PROMPT_VERSION,
                prompt_digest=REWRITE_PROMPT_DIGEST,
            )
            if rewriter is not None and asked
            else None
        ),
    )


def _empty_counts(
    selection: _Selection, renderer: StudentRenderer, rendering: _Rendering
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    counts["sources skipped"] = len(selection.skipped)
    for reason in selection.filtered.values():
        counts[
            "sources decontaminated" if reason.startswith("decontaminated") else "sources filtered"
        ] += 1
    for reason, n in renderer.stats.untranslatable.items():
        counts[f"steps untranslatable ({reason})"] += n
    for reason, n in renderer.stats.skipped.items():
        counts[f"steps {reason.replace('_', ' ')}"] += n
    counts["steps filtered"] = len(selection.filtered_steps)
    for prepared in rendering.prepared.values():
        counts["steps that only waited on a capped capture"] += sum(
            1 for waits in prepared.waits.values() if waits.removes_step
        )
    return {reason: n for reason, n in counts.items() if n}


def _by_key(kept: _Kept) -> str:
    return kept.key


__all__ = [
    "PARTIAL_SUFFIX",
    "ExportReport",
    "ExportRunner",
    "FileImageStore",
    "exporter_settings",
    "translator_settings",
]
