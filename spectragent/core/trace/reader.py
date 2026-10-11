"""RunTrace — the read side of a run directory.

Different consumers need different depths of data — ``exact_match`` reads
``result.json`` and nothing else, an image judge every screenshot, a
debugging session every prompt — so only the manifest is read eagerly and
everything else on first access (``docs/architecture_design.md`` §5.5).

**Every read goes through the manifest.** It is the commit point, so it is
also the reader's only directory: no accessor probes a file name or globs
a directory. A file written before the commit point — a crash between the
content files and the manifest rename, or anything dropped into the
directory by hand — is invisible to every accessor alike. One reader, one
answer to "did this happen".

**What comes back is the type that was written.** ``result`` is an
:class:`AgentResult`, steps are :class:`StepRecord` objects, a fragment is
its own class (rebuilt by :meth:`FragmentBase.from_trace`) or the
:class:`FragmentFailure` recorded in its place, an LLM call is a
:class:`RecordedLLMCall`. The reader used to return bare dicts, which meant
an evaluator learned the on-disk key names by opening a JSON file, and a
wrong guess (``rec["step_index"]``) survived every type check and failed at
run time.

**"Not there" and "not readable" never collapse.** Something the manifest
does not list reads as ``None`` / ``[]`` (and ``KeyError`` for the two
lookups by name); something it lists that cannot be read raises
:class:`TraceSchemaError` naming the file. The single exception is
``result.json``, which is not listed: absent means the run died before
writing it, and reads as ``None``.

**A trace only ever reads its own files.** The manifest and the extra
indexes are data, and traces are shared — attached to issues, uploaded with
papers. A path one of them lists is checked before anything opens it: it
must have the shape the WAL writes (``check_relative_path``) and must still
be inside the run directory once symlinks are resolved. Otherwise a crafted
manifest listing ``/home/me/.ssh/id_rsa`` as a screenshot would hand that
file to an image judge.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from functools import cached_property
from pathlib import Path
from typing import Any, TypeVar

from conscribe import discover
from pydantic import BaseModel, TypeAdapter, ValidationError

from spectragent.core.trace.encoding import (
    ARCHIVE_META_KEY,
    CONFIG_FILE,
    EVALS_DIR,
    MANIFEST_FILE,
    RESULT_FILE,
    archive_blob_path,
    check_relative_path,
    decode_fragment,
    output_format_path,
    sha256_hex,
)
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    ArchiveMeta,
    ArchiveRecord,
    ExtraFileIndex,
    ExtraFileRecord,
    FragmentFiles,
    RecordedLLMCall,
    StepManifestEntry,
    TraceConfig,
    TraceManifest,
)
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.eval_result import EvalResult
from spectragent.schemas.observation._registrar import (
    FragmentBase,
    ObservationRegistrar,
    fragment_field_name,
)
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.step_record import StepRecord

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

#: The first format with the per-step index, per-call records and the
#: failure marker. Accessors over those refuse anything older.
_INDEXED_FORMAT = 2

#: Where fragment classes live. A reader in an evaluation process may never
#: have imported the module that registers one — browser fragments register
#: when their watchdog's module is imported.
_FRAGMENT_PACKAGES = ("spectragent.schemas", "spectragent.core.capabilities")

#: The local name every capability's screenshot fragment carries
#: (``browser.screenshot``, ``desktop.screenshot``); negotiation lets at
#: most one be active per run.
_SCREENSHOT = "screenshot"

_JSON_OBJECT: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])


class RunTrace:
    """Lazy, manifest-driven, typed reader for one trace directory.

    Raises:
        TraceSchemaError: At construction, when ``manifest.json`` is missing
            or unreadable, leads outside the run directory, or declares a
            newer ``format_version`` than this build reads.
    """

    def __init__(self, trace_dir: Path) -> None:
        self._trace_dir = trace_dir
        # Resolved once: what every path the trace names must stay inside.
        self._root = trace_dir.resolve()
        self._manifest_path = trace_dir / MANIFEST_FILE
        self._manifest = _read_model(self._own_file(MANIFEST_FILE), TraceManifest)
        if self._manifest.format_version > TRACE_FORMAT_VERSION:
            raise TraceSchemaError(
                f"{trace_dir} is trace format version {self._manifest.format_version}; this "
                f"build reads up to version {TRACE_FORMAT_VERSION}. Read it with the "
                f"spectragent that wrote it.",
                path=self._manifest_path,
            )
        self._discovered = False
        self._indexes: dict[str, ExtraFileIndex] = {}
        self._archives: dict[str, list[ArchiveRecord]] = {}

    # ── shape unchanged since format 1: readable at any version ───

    @property
    def trace_dir(self) -> Path:
        return self._trace_dir

    @property
    def manifest(self) -> TraceManifest:
        return self._manifest

    @property
    def run_id(self) -> str:
        return self._manifest.run_id

    @property
    def step_count(self) -> int:
        """Number of completed steps — from the manifest, no I/O."""
        return self._manifest.last_step_completed + 1

    @cached_property
    def config(self) -> TraceConfig:
        """The configuration snapshot (``config.json``)."""
        return _read_model(self._own_file(CONFIG_FILE), TraceConfig)

    @cached_property
    def result(self) -> AgentResult | None:
        """The final result, or ``None`` when ``result.json`` is absent.

        ``None`` means exactly one thing: the run died before anything
        terminal was recorded. A file that exists but does not parse
        raises :class:`TraceSchemaError` instead — "written by an older
        schema" and "never finished" are different facts about a run, and
        returning ``None`` for both hides the first one from every
        evaluator downstream.
        """
        path = self._own_file(RESULT_FILE)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        value = _load_json(path, data, "an AgentResult")
        try:
            return AgentResult.model_validate(value)
        except ValidationError as exc:
            raise TraceSchemaError(
                _result_schema_message(path, _declared_schema(value), exc), path=path
            ) from exc

    @cached_property
    def step_records(self) -> list[StepRecord]:
        """Every committed step, in manifest order."""
        return [self._read_step(entry) for entry in self._manifest.steps]

    def step(self, idx: int) -> StepRecord | None:
        """One committed step record, or ``None`` when that step never committed.

        The WAL writes ``steps/NNN.json`` and *then* renames the manifest,
        so a crash in between leaves a complete, parseable step file for a
        step that did not happen; resolving through the manifest is what
        keeps this accessor agreeing with :attr:`step_records` and
        :attr:`step_count`. A step the manifest lists whose file is gone
        raises: that is a damaged trace, not an unfinished one.
        """
        entry = self._entries.get(idx)
        return self._read_step(entry) if entry is not None else None

    # ── format-2 shape: refused for older traces ──────────────

    def observation_fragment(self, step: int, key: str) -> FragmentBase | FragmentFailure | None:
        """The fragment *key* (fully qualified, ``browser.dom``) recorded at *step*.

        ``None`` when the step did not commit or recorded no such fragment.
        A capture that failed comes back as the :class:`FragmentFailure`
        recorded in its place — never as an empty fragment of the class the
        key names, which is what reading its JSON as that class would give.
        Otherwise the class registered under *key* rebuilds the fragment from
        its JSON and its artifact (:meth:`FragmentBase.from_trace`).
        """
        self._require_indexed("observation_fragment")
        entry = self._entries.get(step)
        files = entry.fragments.get(key) if entry is not None else None
        if files is None:
            return None
        path = self._listed(files.file)
        if files.failed:
            failure = _read_model(path, FragmentFailure)
            if failure.fragment_id != key:
                raise TraceSchemaError(
                    f"{path} records the failure of {failure.fragment_id!r}, but is filed "
                    f"under {key!r}",
                    path=path,
                )
            return failure

        cls = self._fragment_class(key)
        payload = _read_json_object(path)
        artifact = self._read_artifact(files.artifact) if files.artifact is not None else None
        try:
            return decode_fragment(key, cls, payload, artifact)
        except (ValueError, RecursionError) as exc:
            # ``RecursionError``: JSON nested deeper than a decoder can walk —
            # far past anything the writer emits, so not this encoding.
            raise TraceSchemaError(
                f"{path} does not decode as {cls.__name__} ({key!r}): {exc}", path=path
            ) from exc

    def screenshot(self, step: int) -> bytes | None:
        """The bytes of *step*'s screenshot, whichever capability took it.

        ``None`` when the step did not commit, carried no screenshot
        fragment, or its capture failed — :meth:`observation_fragment` says
        why it failed.
        """
        self._require_indexed("screenshot")
        entry = self._entries.get(step)
        files = self._screenshot_files(entry) if entry is not None else None
        if files is None or files.failed or files.artifact is None:
            return None
        return self._read_artifact(files.artifact)

    @cached_property
    def screenshots(self) -> list[Path]:
        """Every committed step's screenshot file, in step order.

        Paths rather than bytes because the evaluators that grade from
        images take the media type from the suffix, which the writer took
        from the image header (a JPEG run writes ``.jpg``). Read from the
        manifest's fragment index: a frame for a step that never committed
        is not a frame of this run.
        """
        self._require_indexed("screenshots")
        paths: list[Path] = []
        for entry in sorted(self._manifest.steps, key=lambda item: item.step):
            files = self._screenshot_files(entry)
            if files is None or files.failed or files.artifact is None:
                continue
            path = self._listed(files.artifact)
            _require_bytes(path)
            paths.append(path)
        return paths

    def llm_messages(self, step: int) -> list[RecordedLLMCall]:
        """Every LLM call of *step*, in start order.

        Parse retries, structured replies the validator refused (with
        ``raw_output``), truncations (with ``partial_output``) and calls cut
        off by a cancellation (prompt only) alike. ``[]`` when the step did
        not commit — its calls, if it made any, are in :attr:`trailing_calls`.
        """
        self._require_indexed("llm_messages")
        entry = self._entries.get(step)
        if entry is None:
            return []
        return [self._read_call(rel, step=step) for rel in entry.llm_calls]

    @cached_property
    def trailing_calls(self) -> list[RecordedLLMCall]:
        """Calls of a step the Loop never surrendered, in commit then start order."""
        self._require_indexed("trailing_calls")
        return [
            self._read_call(rel) for entry in self._manifest.trailing for rel in entry.llm_calls
        ]

    def extra_files(self, step: int) -> list[ExtraFileRecord]:
        """The extra files *step* committed; ``[]`` for none, or an uncommitted step."""
        self._require_indexed("extra_files")
        entry = self._entries.get(step)
        if entry is None or entry.extra_index is None:
            return []
        return list(self._read_index(entry.extra_index).files)

    @cached_property
    def trailing_extra_files(self) -> list[ExtraFileRecord]:
        """The extra files every ``commit_trailing`` committed, in commit order."""
        self._require_indexed("trailing_extra_files")
        return [
            record
            for entry in self._manifest.trailing
            if entry.extra_index is not None
            for record in self._read_index(entry.extra_index).files
        ]

    def read_extra(self, path: str) -> bytes:
        """The bytes of a committed extra file, checked against its index entry.

        Raises:
            KeyError: *path* is in no committed extra-file index. "Never
                committed" and "never written" are one fact to a reader.
            TraceSchemaError: The file is gone, its size or sha256 is not
                what the index recorded, or it lies outside the run
                directory (the error then names the index).
        """
        self._require_indexed("read_extra")
        found = self._extra_records.get(path)
        if found is None:
            raise KeyError(f"{path!r} is not a committed extra file of {self._trace_dir}")
        record, index = found
        file = self._listed(path, listed_in=index)
        data = _read_bytes(file)
        if len(data) != record.size or sha256_hex(data) != record.sha256:
            raise TraceSchemaError(
                f"{file} does not match its index entry: {len(data)} bytes, sha256 "
                f"{sha256_hex(data)}; recorded {record.size} bytes, sha256 {record.sha256}",
                path=file,
            )
        return data

    def output_schema(self, sha256: str) -> dict[str, Any]:
        """The requested reply schema an ``OutputFormatRef.sha256`` names.

        Raises:
            KeyError: *sha256* is not a committed output schema.
            TraceSchemaError: The file is gone, is not the bytes its name
                promises, or the manifest's entry leads outside the run
                directory.
        """
        self._require_indexed("output_schema")
        if sha256 not in self._manifest.output_formats:
            raise KeyError(f"{sha256!r} is not a committed output schema of {self._trace_dir}")
        path = self._listed(output_format_path(sha256))
        data = _read_bytes(path)
        if sha256_hex(data) != sha256:
            raise TraceSchemaError(
                f"{path} is content-addressed by {sha256}, but its bytes hash to "
                f"{sha256_hex(data)}",
                path=path,
            )
        return _parse(path, data, _JSON_OBJECT.validate_python, "JSON object")

    # ── environment evidence (env/): through the extra indexes ─

    def archive_files(self, step: int, archiver_id: str) -> list[ArchiveRecord]:
        """The step evidence of *archiver_id* that *step*'s commit took, in index order.

        Step-scoped files only (content-addressed ones included — a blob a
        step references is that step's evidence too); a run record is
        :meth:`archive_run_file`'s. ``[]`` when the step did not commit or
        its commit took nothing of that archiver. Bytes:
        ``read_extra(record.path)``.
        """
        self._require_indexed("archive_files")
        entry = self._entries.get(step)
        if entry is None or entry.extra_index is None:
            return []
        return [
            record
            for record in self._archive_records(entry.extra_index)
            if record.scope == "step" and record.archiver_id == archiver_id
        ]

    def iter_archive(
        self, archiver_id: str, *, include_trailing: bool = False
    ) -> Iterator[tuple[int | None, ArchiveRecord]]:
        """``(step, record)`` for every step file of *archiver_id*, in step order.

        With *include_trailing*, the step evidence the trailing commits took
        follows as ``(None, record)``, in ``manifest.trailing`` order — never
        under a step number: it belongs to no committed step. Run records
        are not yielded.

        The format check happens here, at the call, not at the first
        ``next()``: a format-1 trace is refused where it is asked about.
        """
        self._require_indexed("iter_archive")
        return self._iter_archive(archiver_id, include_trailing)

    def blob(self, sha256: str) -> bytes:
        """The bytes of a committed content-addressed archive file.

        ``read_extra(archive_blob_path(sha256))`` — once a committed index
        lists that path as an archive file (``meta["archive"]``); an extra
        file another producer put at that path is not one.

        Raises:
            KeyError: No committed index lists an archive file at that
                address.
            TraceSchemaError: As :meth:`read_extra`.
        """
        self._require_indexed("blob")
        path = archive_blob_path(sha256)
        if path not in self._archive_blobs:
            raise KeyError(
                f"{sha256!r} is not a committed content-addressed archive file of {self._trace_dir}"
            )
        return self.read_extra(path)

    def archive_lines(self, path: str, model: type[M]) -> list[M]:
        """A committed line-delimited JSON file, each non-blank line read back as *model*.

        For the evidence a capability writes one JSON object per line
        (``index.jsonl``, ``replay.jsonl``, …), in file order. Generic on
        purpose: the line types are the capability's (the trace layer knows
        none of them), and "what comes back is the type that was written"
        holds for its evidence too. The bytes come through
        :meth:`read_extra`, with its size and sha256 check.

        Raises:
            KeyError: *path* is not a committed extra file.
            TraceSchemaError: As :meth:`read_extra`; or a line is not JSON,
                or does not validate as *model* — naming the file and the
                1-based line.
        """
        self._require_indexed("archive_lines")
        data = self.read_extra(path)
        file = self._trace_dir / path
        lines: list[M] = []
        for number, raw in enumerate(data.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError) as exc:  # JSONDecodeError, UnicodeDecodeError
                raise TraceSchemaError(
                    f"{file} line {number} does not parse as JSON: {exc}", path=file
                ) from exc
            try:
                lines.append(model.model_validate(value))
            except ValidationError as exc:
                raise TraceSchemaError(
                    f"{file} line {number} does not parse as a {model.__name__}: {exc}",
                    path=file,
                ) from exc
        return lines

    def archive_run_file(self, capability_id: str) -> ArchiveRecord | None:
        """The highest committed version of *capability_id*'s run record, or ``None``.

        Searched across the step and the trailing indexes: a version is
        committed with whichever commit came next, and the version number
        in the index — not the commit order — says which is latest. The
        writer keeps that the newest capture: it never commits a record
        collected before the latest one as a later version. Bytes:
        ``read_extra(record.path)``.
        """
        self._require_indexed("archive_run_file")
        latest: ArchiveRecord | None = None
        for rel in self._extra_indexes:
            for record in self._archive_records(rel):
                if record.scope != "run" or record.capability_id != capability_id:
                    continue
                if latest is None or _run_version(record) > _run_version(latest):
                    latest = record
        return latest

    # ── after the run: not in the manifest ────────────────────

    def evals(self) -> dict[str, EvalResult]:
        """Every verdict in this run's ``evals/``: ``verdict_slug`` → :class:`EvalResult`, by slug.

        Verdicts are written after the run, each atomically, by
        ``EvaluationRunner`` (``evals/<verdict_slug>.json``) and are not in the
        manifest, so this is the one accessor that lists a directory. Only
        ``*.json`` files count (an interrupted atomic write leaves a
        ``tmp….tmp``); the slug is the file name without ``.json``. No
        ``evals/`` → ``{}``. Not cached: a verdict written after this
        ``RunTrace`` was opened is seen by the next call.

        Raises:
            TraceSchemaError: ``evals/`` or a file in it resolves outside the
                run directory (symlinks resolved — a trace only reads its own
                files), or a file does not read as an ``EvalResult`` (naming it).
        """
        directory = self._trace_dir / EVALS_DIR
        self._require_inside(directory, EVALS_DIR, listed_in=self._trace_dir)
        if not directory.is_dir():
            return {}
        verdicts: dict[str, EvalResult] = {}
        for path in directory.iterdir():
            if path.suffix != ".json":
                continue
            self._require_inside(path, f"{EVALS_DIR}/{path.name}", listed_in=directory)
            if not path.is_file():
                continue
            verdicts[path.stem] = _read_model(path, EvalResult)
        return dict(sorted(verdicts.items()))

    # ── internals ─────────────────────────────────────────────

    @cached_property
    def _entries(self) -> dict[int, StepManifestEntry]:
        return {entry.step: entry for entry in reversed(self._manifest.steps)}

    @cached_property
    def _extra_indexes(self) -> list[str]:
        """Every committed extra index: the steps' in manifest order, then the trailing ones'."""
        indexes = [entry.extra_index for entry in self._manifest.steps]
        indexes += [entry.extra_index for entry in self._manifest.trailing]
        return [rel for rel in indexes if rel is not None]

    @cached_property
    def _archive_blobs(self) -> frozenset[str]:
        """Paths of every committed content-addressed archive file."""
        return frozenset(
            record.path
            for rel in self._extra_indexes
            for record in self._archive_records(rel)
            if record.content_addressed
        )

    def _iter_archive(
        self, archiver_id: str, include_trailing: bool
    ) -> Iterator[tuple[int | None, ArchiveRecord]]:
        for entry in sorted(self._manifest.steps, key=lambda item: item.step):
            if entry.extra_index is None:
                continue
            for record in self._archive_records(entry.extra_index):
                if record.scope == "step" and record.archiver_id == archiver_id:
                    yield entry.step, record
        if not include_trailing:
            return
        for trailing in self._manifest.trailing:
            if trailing.extra_index is None:
                continue
            for record in self._archive_records(trailing.extra_index):
                if record.scope == "step" and record.archiver_id == archiver_id:
                    yield None, record

    def _archive_records(self, rel: str) -> list[ArchiveRecord]:
        """The archive files the extra index *rel* lists, in index order.

        An entry is an archive file when its ``meta`` carries
        ``ARCHIVE_META_KEY``; anything else in the index is some other
        producer's extra file and is skipped. Metadata that does not parse
        as :class:`ArchiveMeta` makes the index unreadable — the error
        names it.
        """
        records = self._archives.get(rel)
        if records is not None:
            return records
        index = self._read_index(rel)
        records = []
        for entry in index.files:
            if ARCHIVE_META_KEY not in entry.meta:
                continue
            try:
                meta = ArchiveMeta.model_validate(entry.meta[ARCHIVE_META_KEY])
            except ValidationError as exc:
                path = self._trace_dir / rel
                raise TraceSchemaError(
                    f"{path} lists {entry.path!r} with archive metadata that does not parse "
                    f"as an ArchiveMeta: {exc}",
                    path=path,
                ) from exc
            records.append(
                ArchiveRecord(
                    path=entry.path,
                    size=entry.size,
                    sha256=entry.sha256,
                    content_addressed=entry.content_addressed,
                    **meta.model_dump(),
                )
            )
        self._archives[rel] = records
        return records

    @cached_property
    def _extra_records(self) -> dict[str, tuple[ExtraFileRecord, Path]]:
        """Every committed extra file by path — its record, and the index that lists it."""
        records: dict[str, tuple[ExtraFileRecord, Path]] = {}
        for rel in self._extra_indexes:
            index = self._trace_dir / rel
            for record in self._read_index(rel).files:
                seen, _ = records.setdefault(record.path, (record, index))
                if (seen.size, seen.sha256) != (record.size, record.sha256):
                    raise TraceSchemaError(
                        f"{index} indexes {record.path!r} as {record.size} bytes, sha256 "
                        f"{record.sha256}; an earlier index recorded {seen.size} bytes, "
                        f"sha256 {seen.sha256}",
                        path=index,
                    )
        return records

    def _require_indexed(self, accessor: str) -> None:
        version = self._manifest.format_version
        if version < _INDEXED_FORMAT:
            raise TraceSchemaError(
                f"{self._trace_dir} is trace format version {version}; {accessor} reads the "
                f"format-{_INDEXED_FORMAT} layout (per-step fragment index, one record per LLM "
                f"call). An older trace cannot be read that way safely — its fragment failures "
                f"carry no marker — so re-run the task to record a current trace. Its config, "
                f"result and step records stay readable.",
                path=self._manifest_path,
            )

    def _own_file(self, name: str) -> Path:
        """A file every run directory names itself (``manifest.json``, …), inside it."""
        path = self._trace_dir / name
        self._require_inside(path, name, listed_in=path)
        return path

    def _listed(self, rel: str, *, listed_in: Path | None = None) -> Path:
        """The file a path in the manifest (or in the extra index *listed_in*) names.

        Checked before anything opens it: the shape the WAL writes
        (:func:`check_relative_path`), and inside the run directory once
        symlinks are resolved. A violation is the listing file's fault, so
        the error names it.
        """
        listing = listed_in if listed_in is not None else self._manifest_path
        try:
            check_relative_path(rel)
        except ValueError as exc:
            raise TraceSchemaError(
                f"{listing} lists a path a trace cannot hold: {exc}", path=listing
            ) from exc
        path = self._trace_dir / rel
        self._require_inside(path, rel, listed_in=listing)
        return path

    def _require_inside(self, path: Path, rel: str, *, listed_in: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root):
            raise TraceSchemaError(
                f"{listed_in} names {rel!r}, which resolves to {resolved}, outside the run "
                f"directory {self._trace_dir}. A trace only ever reads its own files.",
                path=listed_in,
            )

    def _read_step(self, entry: StepManifestEntry) -> StepRecord:
        path = self._listed(entry.step_file)
        record = _read_model(path, StepRecord)
        if record.step != entry.step:
            raise TraceSchemaError(
                f"{path} records step {record.step}, but the manifest commits it as step "
                f"{entry.step}",
                path=path,
            )
        return record

    def _read_call(self, rel: str, *, step: int | None = None) -> RecordedLLMCall:
        path = self._listed(rel)
        call = _read_model(path, RecordedLLMCall)
        if step is not None and call.step != step:
            raise TraceSchemaError(
                f"{path} records a call of step {call.step}, but is committed with step {step}",
                path=path,
            )
        return call

    def _read_index(self, rel: str) -> ExtraFileIndex:
        index = self._indexes.get(rel)
        if index is None:
            path = self._listed(rel)
            index = _read_model(path, ExtraFileIndex)
            # Refused here rather than at ``read_extra``: ``extra_files``
            # hands these paths to callers, who may open them directly.
            for record in index.files:
                try:
                    check_relative_path(record.path)
                except ValueError as exc:
                    raise TraceSchemaError(
                        f"{path} lists a path a trace cannot hold: {exc}", path=path
                    ) from exc
            self._indexes[rel] = index
        return index

    def _read_artifact(self, rel: str) -> bytes:
        path = self._listed(rel)
        data = _read_bytes(path)
        if not data:
            raise TraceSchemaError(f"{path} is an empty artifact", path=path)
        return data

    def _screenshot_files(self, entry: StepManifestEntry) -> FragmentFiles | None:
        keys = [key for key in entry.fragments if fragment_field_name(key) == _SCREENSHOT]
        if not keys:
            return None
        if len(keys) > 1:
            raise TraceSchemaError(
                f"{self._trace_dir} step {entry.step} records {len(keys)} screenshot "
                f"fragments ({sorted(keys)}); negotiation allows one",
                path=self._manifest_path,
            )
        return entry.fragments[keys[0]]

    def _fragment_class(self, key: str) -> type[FragmentBase]:
        """The class registered under *key*, importing the fragment packages once on a miss."""
        cls = _registered(key)
        if cls is None and not self._discovered:
            self._discovered = True
            discover(*_FRAGMENT_PACKAGES)
            cls = _registered(key)
        if cls is None:
            raise TraceSchemaError(
                f"{self._trace_dir} records fragment {key!r}, and no class is registered under "
                f"that key, even after importing {list(_FRAGMENT_PACKAGES)}. The module that "
                f"defines it must be importable by the reader.",
                path=self._manifest_path,
            )
        return cls


def _run_version(record: ArchiveRecord) -> int:
    """A run record's version — always set: :class:`ArchiveMeta` refuses a run record without one."""
    return record.run_version if record.run_version is not None else -1


def _registered(key: str) -> type[FragmentBase] | None:
    try:
        cls = ObservationRegistrar.get(key)
    except KeyError:
        return None
    return cls if isinstance(cls, type) and issubclass(cls, FragmentBase) else None


def _read_bytes(path: Path) -> bytes:
    """A committed file's bytes; a missing one is a damaged trace, not an unfinished run.

    Every other ``OSError`` — permissions, I/O — propagates: it says nothing
    about the trace itself.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise TraceSchemaError(
            f"{path} is committed but missing on disk — the trace is damaged, not unfinished",
            path=path,
        ) from exc


def _require_bytes(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise TraceSchemaError(
            f"{path} is committed but missing on disk — the trace is damaged, not unfinished",
            path=path,
        ) from exc
    if size == 0:
        raise TraceSchemaError(f"{path} is an empty artifact", path=path)


def _load_json(path: Path, data: bytes, what: str) -> Any:
    """Bytes → JSON value: strict UTF-8, then the standard library's parser.

    Not pydantic's ``validate_json``: its parser refuses nesting past 200
    levels, while pydantic-core's serializer — what the writer uses —
    emits up to ~254. A DOM page about a hundred elements deep (exactly the
    pages the encoder's depth budget exists for) committed without error
    and then could never be read back, as "Invalid JSON". ``json.loads``
    is bounded by the interpreter's recursion limit instead, far past
    anything the writer can produce; a file deeper than even that is
    reported like any other unreadable file.
    """
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, UnicodeDecodeError
        raise TraceSchemaError(f"{path} does not parse as {what}: {exc}", path=path) from exc


def _parse(path: Path, data: bytes, validate: Callable[[Any], T], what: str) -> T:
    """The one normalisation: whatever cannot be read as *what* names the file.

    Invalid UTF-8, invalid JSON, an empty or torn file (:func:`_load_json`)
    and a schema mismatch (pydantic's ``ValidationError``) alike.
    """
    value = _load_json(path, data, f"a {what}")
    try:
        return validate(value)
    except ValidationError as exc:
        raise TraceSchemaError(f"{path} does not parse as a {what}: {exc}", path=path) from exc


def _read_model(path: Path, model: type[M]) -> M:
    """Read a committed JSON file as *model* — the reader's single entry for JSON."""
    return _parse(path, _read_bytes(path), model.model_validate, model.__name__)


def _read_json_object(path: Path) -> dict[str, Any]:
    return _parse(path, _read_bytes(path), _JSON_OBJECT.validate_python, "JSON object")


def _declared_schema(value: Any) -> Any:
    """``schema_version`` as the parsed file declares it."""
    return value.get("schema_version") if isinstance(value, dict) else None


def _current_result_schema() -> str:
    """The one ``AgentResult.schema_version`` this build can read.

    Read off the model rather than restated here, so bumping the schema
    cannot leave a stale version number in an error message.
    """
    return str(AgentResult.model_fields["schema_version"].default)


def _result_schema_message(path: Path, found: Any, exc: ValidationError) -> str:
    """Say *why* a result.json is unreadable, in that file's own terms.

    The field-level pydantic errors are only useful when the versions
    agree. When they do not, they are actively misleading: a 2.0 trace
    fails on ``agent_success`` being an extra key, which reads as a typo
    rather than as a schema this reader retired.
    """
    current = _current_result_schema()
    if found is not None and found != current:
        return (
            f"{path} predates AgentResult schema {current}: it declares "
            f"schema_version {found!r}, whose fields this reader cannot map. "
            f"The trace is not readable and re-running the evaluation pass "
            f"will not change that — re-run the task to record a current "
            f"trace, or read the JSON by hand."
        )
    return f"{path} does not parse as an AgentResult (schema {current}): {exc}"
