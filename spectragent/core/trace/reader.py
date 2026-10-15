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

**A recorded prompt comes back whole.** A call record holds a reference
where each image its prompt sent inline stood; the image is stored once per
run under ``messages/images/``. Every accessor that returns recorded calls
puts each image's text back — checked against its hash, on every read —
before the record is validated, so the prompt is byte for byte what was
written (``docs/architecture_design.md`` §5.3).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, TypeVar

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
    prompt_image_digest,
    prompt_image_text,
    restore_prompt_images,
    sha256_hex,
)
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    ArchiveMeta,
    ArchiveRecord,
    CommittedFile,
    CommittedKind,
    ExtraFileIndex,
    ExtraFileRecord,
    FragmentFiles,
    PromptImageRef,
    RecordedCallMetrics,
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
from spectragent.schemas.recording import RecordingTiming
from spectragent.schemas.step_record import StepRecord

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

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

    Mutable records and collections are independent snapshots. Callers can
    edit their view without changing later reads, recording-scope checks,
    committed-file membership, or integrity metadata. Private caches retain
    the validated evidence, so internal lookups never copy an entire manifest.

    Raises:
        TraceSchemaError: At construction, when ``manifest.json`` is missing
            or unreadable, leads outside the run directory, or declares a
            ``format_version`` other than the one this build reads.
    """

    def __init__(self, trace_dir: Path) -> None:
        self._trace_dir = trace_dir
        # Resolved once: what every path the trace names must stay inside.
        self._root = trace_dir.resolve()
        self._manifest_path = trace_dir / MANIFEST_FILE
        path = self._own_file(MANIFEST_FILE)
        document = _load_json(path, _read_bytes(path), "a TraceManifest")
        # Checked on the parsed JSON, before the model: an older manifest's
        # ``status`` is not a value the current one knows, and the version is
        # the honest thing to report — not a field error.
        _require_current_format(path, document)
        self._manifest = _validated(path, document, TraceManifest.model_validate, "TraceManifest")
        self._discovered = False
        self._indexes: dict[str, ExtraFileIndex] = {}
        self._archives: dict[str, list[ArchiveRecord]] = {}

    # ── identity, configuration, result, steps ───────────────

    @property
    def trace_dir(self) -> Path:
        return self._trace_dir

    @property
    def manifest(self) -> TraceManifest:
        """An independent snapshot; edits cannot change the reader's committed index."""
        return self._manifest.model_copy(deep=True)

    @property
    def run_id(self) -> str:
        return self._manifest.run_id

    @property
    def step_count(self) -> int:
        """Number of committed steps, including saved steps after a recording gap."""
        return len(self._manifest.steps)

    @property
    def config(self) -> TraceConfig:
        """The configuration snapshot (``config.json``)."""
        return self._config.model_copy(deep=True)

    @cached_property
    def _config(self) -> TraceConfig:
        return _read_model(self._own_file(CONFIG_FILE), TraceConfig)

    def require_recording(
        self,
        *channels: Literal["metrics", "errors", "trajectory", "observations"],
        consumer: str,
    ) -> None:
        """Refuse omitted content without declaring an intentionally light recording damaged."""
        missing = tuple(name for name in channels if not getattr(self._config.recording, name))
        if missing:
            raise RecordingScopeError(consumer, missing)

    @property
    def result(self) -> AgentResult | None:
        """The final result, or ``None`` when ``result.json`` is absent.

        ``None`` means exactly one thing: the run died before anything
        terminal was recorded. A file that exists but does not parse
        raises :class:`TraceSchemaError` instead — "written by an older
        schema" and "never finished" are different facts about a run, and
        returning ``None`` for both hides the first one from every
        evaluator downstream.
        """
        result = self._result
        return result.model_copy(deep=True) if result is not None else None

    @cached_property
    def _result(self) -> AgentResult | None:
        data = self._result_data
        if data is None:
            return None
        path = self._trace_dir / RESULT_FILE
        value = _load_json(path, data, "an AgentResult")
        try:
            return AgentResult.model_validate(value)
        except ValidationError as exc:
            raise TraceSchemaError(
                _result_schema_message(path, _declared_schema(value), exc), path=path
            ) from exc

    @property
    def step_records(self) -> list[StepRecord]:
        """Every committed step, in manifest order."""
        return [record.model_copy(deep=True) for record in self._step_records]

    @cached_property
    def _step_records(self) -> list[StepRecord]:
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

    # ── observations, calls, extra files ─────────────────────

    def observation_fragment(self, step: int, key: str) -> FragmentBase | FragmentFailure | None:
        """The fragment *key* (fully qualified, ``browser.dom``) recorded at *step*.

        ``None`` when the step did not commit or recorded no such fragment.
        A capture that failed comes back as the :class:`FragmentFailure`
        recorded in its place — never as an empty fragment of the class the
        key names, which is what reading its JSON as that class would give.
        Otherwise the class registered under *key* rebuilds the fragment from
        its JSON and its artifact (:meth:`FragmentBase.from_trace`).
        """
        self.require_recording("observations", consumer="observation reading")
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
        self.require_recording("observations", consumer="screenshot reading")
        entry = self._entries.get(step)
        files = self._screenshot_files(entry) if entry is not None else None
        if files is None or files.failed or files.artifact is None:
            return None
        return self._read_artifact(files.artifact)

    @property
    def screenshots(self) -> list[Path]:
        """Every committed step's screenshot file, in step order.

        Paths rather than bytes because the evaluators that grade from
        images take the media type from the suffix, which the writer took
        from the image header (a JPEG run writes ``.jpg``). Read from the
        manifest's fragment index: a frame for a step that never committed
        is not a frame of this run.
        """
        return list(self._screenshots)

    @cached_property
    def _screenshots(self) -> list[Path]:
        self.require_recording("observations", consumer="screenshot reading")
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
        not commit or never made a call. A sealed step whose commit failed
        stays associated with its original step; only calls of a step the
        loop never surrendered appear in :attr:`trailing_calls`.
        Each prompt holds its inline images again, exactly as they were sent.

        Raises:
            TraceSchemaError: A call record does not read back — naming it,
                or naming an image it refers to that is missing or does not
                match its hash.
        """
        self.require_recording("trajectory", consumer="model trajectory reading")
        entry = self._entries.get(step)
        if entry is None:
            return []
        return [self._read_call(rel, step=step) for rel in entry.llm_calls]

    @property
    def trailing_calls(self) -> list[RecordedLLMCall]:
        """Calls of a step the Loop never surrendered, in commit then start order.

        Their prompts hold their inline images again, as :meth:`llm_messages`'
        do.
        """
        return [call.model_copy(deep=True) for call in self._trailing_calls]

    @cached_property
    def _trailing_calls(self) -> list[RecordedLLMCall]:
        self.require_recording("trajectory", consumer="model trajectory reading")
        return [
            self._read_call(rel) for entry in self._manifest.trailing for rel in entry.llm_calls
        ]

    def call_metrics(self, step: int) -> list[RecordedCallMetrics]:
        """Calls' lightweight facts, without loading prompts, schemas, or images."""
        entry = self._entries.get(step)
        if entry is None:
            return []
        return [self._read_call_metrics(rel, step=step) for rel in entry.call_metrics]

    @property
    def trailing_call_metrics(self) -> list[RecordedCallMetrics]:
        """Lightweight facts of calls whose executing step was never surrendered."""
        return [call.model_copy(deep=True) for call in self._trailing_call_metrics]

    @cached_property
    def _trailing_call_metrics(self) -> list[RecordedCallMetrics]:
        return [
            self._read_call_metrics(rel)
            for entry in self._manifest.trailing
            for rel in entry.call_metrics
        ]

    def extra_files(self, step: int) -> list[ExtraFileRecord]:
        """The extra files *step* committed; ``[]`` for none, or an uncommitted step."""
        entry = self._entries.get(step)
        if entry is None or entry.extra_index is None:
            return []
        return [
            record.model_copy(deep=True) for record in self._read_index(entry.extra_index).files
        ]

    @property
    def trailing_extra_files(self) -> list[ExtraFileRecord]:
        """The extra files every ``commit_trailing`` committed, in commit order."""
        return [
            record.model_copy(deep=True)
            for entry in self._manifest.trailing
            if entry.extra_index is not None
            for record in self._read_index(entry.extra_index).files
        ]

    def recording_timing(self, step: int) -> float | None:
        """Committed recording overhead for *step*, including its persistence.

        The measurement is committed later as indexed extra evidence. It
        never rewrites the step record or becomes the next step's duration.
        """
        for rel, (record, index) in self._extra_records.items():
            if record.meta.get("kind") != "recording_timing" or record.meta.get("step") != step:
                continue
            path = self._listed(rel, listed_in=index)
            timing = _parse(
                path, self.read_extra(rel), RecordingTiming.model_validate, "RecordingTiming"
            )
            if timing.step != step:
                raise TraceSchemaError(
                    f"{path} records timing for step {timing.step}, but its index says step {step}",
                    path=path,
                )
            return timing.recording_ms
        return None

    def read_extra(self, path: str) -> bytes:
        """The bytes of a committed extra file, checked against its index entry.

        Raises:
            KeyError: *path* is in no committed extra-file index. "Never
                committed" and "never written" are one fact to a reader.
            TraceSchemaError: The file is gone, its size or sha256 is not
                what the index recorded, or it lies outside the run
                directory (the error then names the index).
        """
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
        if sha256 not in self._manifest.output_formats:
            raise KeyError(f"{sha256!r} is not a committed output schema of {self._trace_dir}")
        path, data = self._output_schema_bytes(sha256)
        return _parse(path, data, _JSON_OBJECT.validate_python, "JSON object")

    # ── what the run consists of: every committed file ─────────

    @property
    def committed_files(self) -> list[CommittedFile]:
        """Every file this run committed, one entry per listing, in commit order.

        The run's own files first (``manifest.json``, ``config.json``, and
        ``result.json`` when the run wrote one); then, step by step in
        manifest order, the step record, each fragment's file and artifact,
        each LLM call, the extra index and every file it lists; then each
        trailing commit's calls, index and files; then the requested reply
        schemas; then the prompt images, each once (a call record's own
        bytes refer to them). An extra file several commits reference
        (content-addressed) is listed once per commit that lists it — how to
        count it is the caller's rule. Nothing the manifest (or a committed
        index) does not list is here, and every path is checked as it is
        listed (the WAL's shape, inside the run directory), as every accessor
        checks before it opens one. Bytes: :meth:`read_committed`.

        Raises:
            TraceSchemaError: A listed path is malformed or leads outside the
                run directory, an extra index does not read back, or an
                archive file's metadata does not parse — naming the file
                that lists it.
        """
        return deepcopy(self._committed_files)

    @cached_property
    def _committed_files(self) -> list[CommittedFile]:
        self._own_file(CONFIG_FILE)
        files = [
            CommittedFile(path=MANIFEST_FILE, kind="manifest"),
            CommittedFile(path=CONFIG_FILE, kind="config"),
        ]
        if self._result_data is not None:
            files.append(CommittedFile(path=RESULT_FILE, kind="result"))
        for entry in self._manifest.steps:
            files.append(self._listing(entry.step_file, "step", entry.step))
            for key, fragment in entry.fragments.items():
                files.append(self._listing(fragment.file, "fragment", entry.step, key))
                if fragment.artifact is not None:
                    files.append(self._listing(fragment.artifact, "artifact", entry.step, key))
            files += [self._listing(rel, "llm_call", entry.step) for rel in entry.llm_calls]
            files += [self._listing(rel, "call_metrics", entry.step) for rel in entry.call_metrics]
            if entry.extra_index is not None:
                files.append(self._listing(entry.extra_index, "extra_index", entry.step))
                files += self._extra_listings(entry.extra_index, entry.step)
        for trailing in self._manifest.trailing:
            files += [self._listing(rel, "llm_call", None) for rel in trailing.llm_calls]
            files += [self._listing(rel, "call_metrics", None) for rel in trailing.call_metrics]
            if trailing.extra_index is not None:
                files.append(self._listing(trailing.extra_index, "extra_index", None))
                files += self._extra_listings(trailing.extra_index, None)
        files += [
            self._listing(output_format_path(sha256), "output_schema", None)
            for sha256 in self._manifest.output_formats
        ]
        files += [self._listing(rel, "prompt_image", None) for rel in self._manifest.prompt_images]
        return files

    def read_committed(self, path: str) -> bytes:
        """The bytes of a file :attr:`committed_files` lists, through its own accessor's checks.

        An extra file is :meth:`read_extra` (size and sha256 against its
        index); a reply schema and a prompt image must hash to their
        address; an artifact must not be empty; every listed file must be on
        disk, inside the run directory. A call record comes back as it is on
        disk, its images as references.

        Raises:
            KeyError: *path* is not a committed file of this run.
            TraceSchemaError: The file is missing, empty (an artifact), not
                the bytes its index or address promises, or outside the run
                directory.
        """
        found = self._committed_by_path.get(path)
        if found is None:
            raise KeyError(f"{path!r} is not a committed file of {self._trace_dir}")
        if found.kind == "extra":
            return self.read_extra(path)
        if found.kind == "result":
            assert self._result_data is not None, "listed only when the run wrote it"
            return self._result_data
        if found.kind in ("manifest", "config"):
            return _read_bytes(self._own_file(path))
        if found.kind == "artifact":
            return self._read_artifact(path)
        if found.kind == "output_schema":
            return self._output_schema_bytes(self._schema_addresses[path])[1]
        if found.kind == "prompt_image":
            return self._prompt_image_bytes(path, listed_in=self._manifest_path)
        return _read_bytes(self._listed(path))

    # ── environment evidence (env/): through the extra indexes ─

    def archive_files(self, step: int, archiver_id: str) -> list[ArchiveRecord]:
        """The step evidence of *archiver_id* that *step*'s commit took, in index order.

        Step-scoped files only (content-addressed ones included — a blob a
        step references is that step's evidence too); a run record is
        :meth:`archive_run_file`'s. ``[]`` when the step did not commit or
        its commit took nothing of that archiver. Bytes:
        ``read_extra(record.path)``.
        """
        entry = self._entries.get(step)
        if entry is None or entry.extra_index is None:
            return []
        return [
            record.model_copy(deep=True)
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
        """
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
        latest: ArchiveRecord | None = None
        for rel in self._extra_indexes:
            for record in self._archive_records(rel):
                if record.scope != "run" or record.capability_id != capability_id:
                    continue
                if latest is None or _run_version(record) > _run_version(latest):
                    latest = record
        return latest.model_copy(deep=True) if latest is not None else None

    # ── after the run: not in the manifest ────────────────────

    def evals(self) -> dict[str, EvalResult]:
        """Every verdict in this run's ``evals/``: ``verdict_slug`` → :class:`EvalResult`, by slug.

        Verdicts are written after the run, each atomically, by
        ``EvaluationRunner`` (``evals/<verdict_slug>.json``) and are not in the
        manifest, so this is the one accessor that lists a directory — in
        sorted order. Only ``*.json`` files count (an interrupted atomic
        write leaves a ``tmp….tmp``); the slug is the file name without
        ``.json``. No ``evals/`` → ``{}``. Not cached: a verdict written
        after this ``RunTrace`` was opened is seen by the next call.

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
        # Sorted, so which bad file is named first does not depend on the
        # order the file system lists them in.
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json":
                continue
            self._require_inside(path, f"{EVALS_DIR}/{path.name}", listed_in=directory)
            if not path.is_file():
                continue
            verdicts[path.stem] = _read_persisted_eval_result(path)
        return dict(sorted(verdicts.items()))

    # ── internals ─────────────────────────────────────────────

    @cached_property
    def _entries(self) -> dict[int, StepManifestEntry]:
        return {entry.step: entry for entry in reversed(self._manifest.steps)}

    @cached_property
    def _result_data(self) -> bytes | None:
        """``result.json``'s bytes, or ``None`` when the run never wrote it (nothing lists it)."""
        path = self._own_file(RESULT_FILE)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @cached_property
    def _committed_by_path(self) -> dict[str, CommittedFile]:
        """Each committed path's first listing."""
        found: dict[str, CommittedFile] = {}
        for file in self._committed_files:
            found.setdefault(file.path, file)
        return found

    @cached_property
    def _schema_addresses(self) -> dict[str, str]:
        """Each committed reply schema's path → its sha256."""
        return {output_format_path(sha256): sha256 for sha256 in self._manifest.output_formats}

    def _listing(
        self, rel: str, kind: CommittedKind, step: int | None, fragment: str | None = None
    ) -> CommittedFile:
        """One file the manifest lists, its path checked (:meth:`_listed`)."""
        self._listed(rel)
        return CommittedFile(path=rel, kind=kind, step=step, fragment=fragment)

    def _extra_listings(self, rel: str, step: int | None) -> list[CommittedFile]:
        """Every file the extra index *rel* lists, with its archive record when it is evidence."""
        index = self._trace_dir / rel
        archives = {record.path: record for record in self._archive_records(rel)}
        listings: list[CommittedFile] = []
        for record in self._read_index(rel).files:
            self._listed(record.path, listed_in=index)
            listings.append(
                CommittedFile(
                    path=record.path,
                    kind="extra",
                    step=step,
                    extra=record,
                    archive=archives.get(record.path),
                )
            )
        return listings

    def _output_schema_bytes(self, sha256: str) -> tuple[Path, bytes]:
        """A committed reply schema's file and bytes, which must hash to *sha256*."""
        path = self._listed(output_format_path(sha256))
        data = _read_bytes(path)
        if sha256_hex(data) != sha256:
            raise TraceSchemaError(
                f"{path} is content-addressed by {sha256}, but its bytes hash to "
                f"{sha256_hex(data)}",
                path=path,
            )
        return path, data

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
                    yield entry.step, record.model_copy(deep=True)
        if not include_trailing:
            return
        for trailing in self._manifest.trailing:
            if trailing.extra_index is None:
                continue
            for record in self._archive_records(trailing.extra_index):
                if record.scope == "step" and record.archiver_id == archiver_id:
                    yield None, record.model_copy(deep=True)

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
        """A call record, its prompt images put back, validated as a ``RecordedLLMCall``."""
        path = self._listed(rel)
        record = _load_json(path, _read_bytes(path), "a RecordedLLMCall")
        try:
            restore_prompt_images(record, lambda ref: self._prompt_image_text(ref, listed_in=path))
        except ValueError as exc:
            raise TraceSchemaError(
                f"{path}: a stored prompt image cannot be put back: {exc}", path=path
            ) from exc
        call = _validated(path, record, RecordedLLMCall.model_validate, "RecordedLLMCall")
        if step is not None and call.step != step:
            raise TraceSchemaError(
                f"{path} records a call of step {call.step}, but is committed with step {step}",
                path=path,
            )
        return call

    def _read_call_metrics(self, rel: str, *, step: int | None = None) -> RecordedCallMetrics:
        path = self._listed(rel)
        call = _read_model(path, RecordedCallMetrics)
        if step is not None and call.step != step:
            raise TraceSchemaError(
                f"{path} records call metrics of step {call.step}, "
                f"but is committed with step {step}",
                path=path,
            )
        return call

    @cached_property
    def _committed_prompt_images(self) -> frozenset[str]:
        return frozenset(self._manifest.prompt_images)

    def _prompt_image_text(self, ref: PromptImageRef, *, listed_in: Path) -> str:
        """The text the prompt image *ref* (found in the call record *listed_in*) stands for."""
        data = self._prompt_image_bytes(ref.stored_image, listed_in=listed_in)
        try:
            return prompt_image_text(ref, data)
        except ValueError as exc:  # UnicodeDecodeError
            file = self._trace_dir / ref.stored_image
            raise TraceSchemaError(
                f"{file} does not read back as the text {listed_in} refers to: {exc}", path=file
            ) from exc

    def _prompt_image_bytes(self, rel: str, *, listed_in: Path) -> bytes:
        """A committed prompt image's bytes, which must hash to the sha256 its name carries.

        Checked on every read, never cached.

        Raises:
            TraceSchemaError: *rel* is not a committed prompt image (naming
                *listed_in*, which refers to it); the manifest lists it under
                a name no sha256 makes, or outside the run directory (naming
                the manifest); the file is missing or does not hash to its
                name (naming the file).
        """
        if rel not in self._committed_prompt_images:
            raise TraceSchemaError(
                f"{listed_in} refers to the stored prompt image {rel!r}, which the manifest does "
                f"not list: it was never committed",
                path=listed_in,
            )
        file = self._listed(rel)
        digest = prompt_image_digest(rel)
        if digest is None:
            raise TraceSchemaError(
                f"{self._manifest_path} lists {rel!r} as a stored prompt image, but no sha256 "
                f"names it",
                path=self._manifest_path,
            )
        data = _read_bytes(file)
        if sha256_hex(data) != digest:
            raise TraceSchemaError(
                f"{file} is content-addressed by {digest}, but its bytes hash to "
                f"{sha256_hex(data)}",
                path=file,
            )
        return data

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
    return _validated(path, _load_json(path, data, f"a {what}"), validate, what)


def _validated(path: Path, value: Any, validate: Callable[[Any], T], what: str) -> T:
    """*value*, parsed from *path*, validated as *what* — a mismatch names the file."""
    try:
        return validate(value)
    except ValidationError as exc:
        raise TraceSchemaError(f"{path} does not parse as a {what}: {exc}", path=path) from exc


def _require_current_format(path: Path, document: Any) -> None:
    """Refuse a manifest of any format but :data:`TRACE_FORMAT_VERSION`.

    Older formats are not read at all, not even their configuration or
    result, and there is no compatibility shim (AGENTS.md §0.4): format 4
    stores each image a prompt sent inline once per run, where a format-3
    call record holds them inline; format 3 split the manifest's ``status``
    from the agent's outcome, where a format-2 ``error`` meant either "the
    agent failed" or "the trace is incomplete" — no reader can tell which.
    A newer format is one this build does not know.
    """
    if not isinstance(document, dict):
        return  # not a manifest at all: the model's validation names the file
    found = document.get("format_version", 1)
    if found == TRACE_FORMAT_VERSION and not isinstance(found, bool):
        return
    if isinstance(found, int) and not isinstance(found, bool) and found > TRACE_FORMAT_VERSION:
        raise TraceSchemaError(
            f"{path} declares trace format version {found}; this build reads version "
            f"{TRACE_FORMAT_VERSION} only. Read it with the spectragent that wrote it.",
            path=path,
        )
    raise TraceSchemaError(
        f"{path} declares trace format version {found!r}; this build reads version "
        f"{TRACE_FORMAT_VERSION} only. {_why_not_read(found)} An older trace is not read at all: "
        f"re-run the task to record a current trace.",
        path=path,
    )


def _why_not_read(found: Any) -> str:
    """What changed since the format *found*, in the words of a refusal."""
    if found == 3 and type(found) is int:
        return (
            "A format-3 trace holds every image a prompt sent inline in each call record; "
            "the current format stores each one once."
        )
    if type(found) is int and found < 3:
        return (
            "The current manifest says whether the trace is complete and leaves how the agent "
            "ended to result.json; an older trace's status mixed the two."
        )
    return "That is not a format version."


def _read_model(path: Path, model: type[M]) -> M:
    """Read a committed JSON file as *model* — the reader's single entry for JSON."""
    return _parse(path, _read_bytes(path), model.model_validate, model.__name__)


def _read_persisted_eval_result(path: Path) -> EvalResult:
    """Require the provenance that a stored verdict needs to be interpretable.

    ``EvalResult`` supplies construction defaults for a freshly graded
    result. A saved JSON document must have actually recorded these fields;
    otherwise an older score could silently become a current graded verdict.
    """
    value = _read_json_object(path)
    missing = {"schema_version", "evaluator_config", "verdict_source"} - value.keys()
    if missing:
        raise TraceSchemaError(
            f"{path} is missing persisted EvalResult field(s) {sorted(missing)}",
            path=path,
        )
    return _validated(path, value, EvalResult.model_validate, "EvalResult")


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
