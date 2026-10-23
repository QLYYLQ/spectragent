"""``ExportRunner``: recorded runs in, a validated dataset directory out (arch §4.15 "流水线").

Not registered — the counterpart of ``EvaluationRunner``. Synchronous: there
is no model to wait for and no environment to drive. It reaches the export's
strategies only through the configured :class:`~spectragent.export._registrar.Exporter`
and its ``translator`` receptor, never a concrete class:

1. **sources** — the runs ``sources`` matches under ``sources_root``, by
   relative path, read through ``RunTrace`` (``filters.open_sources``), then
   the filters, then decontamination (the benchmarks ``against`` lists;
   benchmark data read through the document's loaders, ``benchmarks.py``) and
   the split key (``filters``, ``split``);
2. **canonical samples** — ``canonical.canonicalise`` per kept run (a branch
   run labelled by its source run's verdicts);
3. **translation** — ``exporter.translator.translate`` per sample;
4. **student rendering** — ``render.StudentRenderer`` per trajectory;
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
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.sources import TaskIdentity, task_identity, unreadable_reason
from spectragent.export._registrar import Exporter, ExportRecord
from spectragent.export.benchmarks import BenchmarkLoaders
from spectragent.export.canonical import canonicalise
from spectragent.export.errors import ExportConfigError, ExportEmptyError
from spectragent.export.filters import Source, filtered_out, open_sources
from spectragent.export.render import RenderedSample, StudentRenderer, TrajectoryStep
from spectragent.export.split import Decontaminator, Split, split_key, split_of
from spectragent.export.validate import (
    ApproxTokenEstimator,
    ReferenceScanner,
    check_coordinates,
    check_credentials,
    check_images,
    check_length,
)
from spectragent.schemas.dataset import (
    DATASET_FORMAT,
    BenchmarkEntry,
    CaptureLedger,
    DatasetManifest,
    DecontaminationLedger,
    ExportedSource,
    LeakScanLedger,
    SourceLedger,
    SplitLedger,
    StepLedger,
    StudentSummary,
    ThoughtLedger,
)
from spectragent.schemas.observation.image import image_suffix
from spectragent.schemas.sample import CanonicalStepSample

logger = logging.getLogger(__name__)

#: Where a dataset is assembled before the rename that publishes it.
PARTIAL_SUFFIX = ".partial"
#: Suffixes by media type, for image bytes whose header names no known type.
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def canonical_json(value: Any, *, indent: int | None = None) -> str:
    """*value* as JSON with sorted keys and fixed separators — byte-stable."""
    if indent is not None:
        return json.dumps(value, sort_keys=True, indent=indent, ensure_ascii=False)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
    """

    output_dir: Path
    samples: Mapping[str, int]
    drops: Mapping[str, int]
    manifest: DatasetManifest


@dataclass(frozen=True)
class _Kept:
    """A source the filters kept, with its task and split."""

    source: Source
    identity: TaskIdentity
    key: str
    split: Split


@dataclass
class _Selection:
    kept: list[_Kept]
    readable: dict[str, Source]
    skipped: dict[str, str]
    filtered: dict[str, str]
    decontaminator: Decontaminator


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
        renderer = StudentRenderer.from_config(
            Path(exporter.student_run_config).expanduser(),
            composer=self.composer,
            untranslatable=exporter.translator.untranslatable,
            thought_source=exporter.thought_source,
            thought_index_refs=exporter.thought_index_refs,
        )
        selection = self._select()
        rendered = asyncio.run(self._render(selection, renderer))
        if not rendered:
            raise ExportEmptyError(_empty_counts(selection, renderer))
        rendered = _mark_duplicates(rendered)
        if partial.exists():
            logger.info("removing %s, left by an earlier export that did not finish", partial)
            shutil.rmtree(partial)
        try:
            partial.mkdir(parents=True)
            manifest, samples = self._write(partial, selection, renderer, rendered)
            partial.rename(output)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return ExportReport(
            output_dir=output,
            samples=samples,
            drops=dict(sorted(renderer.stats.untranslatable.items())),
            manifest=manifest,
        )

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
            kept.append(_Kept(source, identity, key, split_of(key, exporter.split)))
        return _Selection(kept, readable, skipped, filtered, decontaminator)

    # ── 2-4. canonical samples, translation, rendering ───────

    async def _render(
        self, selection: _Selection, renderer: StudentRenderer
    ) -> list[RenderedSample]:
        exporter = self.exporter
        translated: dict[str, dict[int, CanonicalStepSample]] = {}

        def samples_of(source: Source, verdicts_from: Source | None = None) -> dict[int, Any]:
            if source.run_id not in translated:
                teacher = AgentRegistrar.get_or_none(source.config.agent_id)
                samples = canonicalise(
                    source.trace,
                    rel_path=source.rel,
                    thought_source=exporter.thought_source,
                    teacher_cls=teacher,
                    verdicts_from=verdicts_from.trace if verdicts_from is not None else None,
                )
                translator = exporter.translator
                translated[source.run_id] = {
                    sample.step: translator.translate(sample, source.trace).applied(sample)
                    for sample in samples
                }
            return translated[source.run_id]

        rendered: list[RenderedSample] = []
        for kept in selection.kept:
            source = kept.source
            renderer.check_source(source.trace, source.rel)
            branch = source.config.branch
            steps: list[TrajectoryStep] = []
            if branch is not None:
                origin = selection.readable[branch.source_run_id]
                renderer.check_source(origin.trace, origin.rel)
                prefix = samples_of(origin)
                steps += [
                    TrajectoryStep(origin.trace, origin.rel, record, prefix.get(record.step), False)
                    for record in origin.trace.step_records
                    if record.step < branch.checkpoint_step
                    and record.step <= origin.trace.manifest.complete_through
                ]
                own = samples_of(source, verdicts_from=origin)
                steps += [
                    TrajectoryStep(source.trace, source.rel, record, own.get(record.step))
                    for record in source.trace.step_records
                    if record.origin == "policy"
                    and record.step >= branch.checkpoint_step
                    and record.step <= source.trace.manifest.complete_through
                ]
            else:
                own = samples_of(source)
                steps += [
                    TrajectoryStep(source.trace, source.rel, record, own.get(record.step))
                    for record in source.trace.step_records
                    if record.step <= source.trace.manifest.complete_through
                ]
            rendered.extend(await renderer.render(steps))
        return rendered

    # ── 5-7. format, validate, write ─────────────────────────

    def _write(
        self,
        root: Path,
        selection: _Selection,
        renderer: StudentRenderer,
        rendered: Sequence[RenderedSample],
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
        written = canonical_json(
            self._manifest(selection, renderer, rendered, samples, per_source, scanner).model_dump(
                mode="json"
            ),
            indent=2,
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


def _empty_counts(selection: _Selection, renderer: StudentRenderer) -> dict[str, int]:
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
    return {reason: n for reason, n in counts.items() if n}


def _by_key(kept: _Kept) -> str:
    return kept.key


__all__ = [
    "PARTIAL_SUFFIX",
    "ExportReport",
    "ExportRunner",
    "FileImageStore",
    "canonical_json",
    "exporter_settings",
    "translator_settings",
]
