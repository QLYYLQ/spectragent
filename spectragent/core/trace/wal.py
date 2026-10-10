"""WALWriter — the write side of a run directory.

A pure I/O engine: it knows nothing of hooks or AOP. ``TraceWriter`` hands
it one step at a time, already encoded (:mod:`spectragent.core.trace.encoding`),
and it commits each in a fixed order (``docs/architecture_design.md`` §5.2):

0. validate everything — paths, content addresses, record bytes — before
   writing a single byte;
1. write every content file durably (temp file → ``fsync(file)`` →
   ``os.replace``), recording the directories touched or created but not
   ``fsync``-ing them yet;
2. write ``steps/NNN.json`` and, when the step carries extra files, its
   ``steps/NNN.extra.json`` index — data ``fsync`` only;
3. ``fsync`` each distinct directory once (deepest first), including the
   parent of every directory not yet known durable;
4. atomically rewrite ``manifest.json`` — **the commit point**.

A failure anywhere in 0–3 raises with the manifest untouched, on disk and in
memory, so whatever was written is an orphan no reader will see — and the
same commit can be retried. "Not yet known durable" is what makes the retry
honest: a directory the failed attempt created already exists when the retry
runs, so "created by this call" would skip the ``fsync`` of its parent, and
the manifest would then commit files under a name a power loss can take
back. The writer therefore remembers which directories a *successful* commit
made durable, and ``fsync``-s the parent of every other one it writes under.

Each file's data ``fsync`` is kept; only the directory ``fsync`` is batched.
Renaming a file whose bytes never reached the disk lets a power loss leave
the manifest listing an empty file, which would make the commit point lie.
One directory ``fsync`` covers every entry written under that directory, so
batching costs no guarantee — and saves a ``fsync`` per file on a step that
carries hundreds of evidence files.

No ``F_FULLFSYNC`` at the commit point on macOS. Measured at ~5.2–5.4 ms
per call against ~0.06–0.14 ms for ``os.fsync``, it would add ~8.5 s to the
test suite's ~1,600 manifest commits (~10 % of an ~85 s run), for a
guarantee that applies only on the platform the WAL is least often run on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import (
    CONFIG_FILE,
    EVALS_DIR,
    MANIFEST_FILE,
    MESSAGES_DIR,
    OBSERVATIONS_DIR,
    OUTPUT_FORMATS_DIR,
    RESULT_FILE,
    STEPS_DIR,
    WAL_OWNED_DIRS,
    WAL_OWNED_FILES,
    EncodedFragment,
    EncodedLLMCall,
    TraceFile,
    check_relative_path,
    fold_path,
    sha256_hex,
    step_extra_index_path,
    step_record_path,
    trailing_extra_index_path,
)
from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    ExtraFileIndex,
    ExtraFileRecord,
    FragmentFiles,
    RunStatus,
    StepManifestEntry,
    TraceConfig,
    TraceManifest,
    TrailingEntry,
)
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.step_record import StepRecord


class WALWriter:
    """Stateful WAL writer for a single agent run.

    Creates and manages the trace directory (§5.3)::

        traces/{run_id}/
            manifest.json   config.json   result.json
            steps/  observations/  messages/  evals/  [env/]

    ``env/`` holds environment evidence when a run binds archivers. It is
    made of ordinary extra files — this writer neither knows nor names
    them; ``encoding.encode_archive_file`` does.

    Beyond the manifest it keeps four things in memory, all updated only
    once a manifest rewrite has succeeded: every committed path (so a
    committed file can never be overwritten), every directory those paths
    run through (so no later path can turn one into a file, or a file into
    a directory), the content-addressed paths already on disk (so a blob
    many steps reference is written once), and the directories whose own
    names are known durable (so a retried commit still ``fsync``-s the
    parent of a directory its failed attempt created).
    """

    def __init__(self, base_dir: Path, run_id: str) -> None:
        self._run_dir = base_dir / run_id
        self._run_id = run_id
        self._manifest: TraceManifest | None = None
        # Folded (``encoding.fold_path``): on a case- and normalization-
        # insensitive filesystem (macOS by default) ``env/A.txt`` and
        # ``env/a.txt`` are one file, as are ``café`` precomposed and
        # decomposed, and a trace must survive being copied to one.
        self._committed: set[str] = set()
        self._committed_dirs: set[str] = set()
        self._committed_blobs: set[str] = set()
        self._durable_dirs: set[Path] = set()

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def manifest(self) -> TraceManifest | None:
        return self._manifest

    def init_run(self, config: TraceConfig, *, config_hash: str | None = None) -> None:
        """Create the directory tree, write ``config.json``, then the first manifest.

        In that order, and durable before the manifest: ``config.json``
        (creating the run directory, and ``traces/`` if needed), the WAL's
        own subdirectories, then one directory ``fsync`` each for the run
        directory and the parent of every directory created on the way. The
        manifest is what makes the run exist — ``discover_traces`` looks for
        nothing else — so a failure before it leaves no run behind, and a
        run that exists has its config.

        Args:
            config: The snapshot written to ``config.json``.
            config_hash: The run's configuration digest, computed by the
                caller because only the caller still has the *unredacted*
                configuration to compute it from — see
                :meth:`~spectragent.core.trace.models.TraceConfig.configuration_digest`.
                Omitting it falls back to a digest over the recorded
                (redacted) snapshot, which is all this writer can see and
                which cannot tell two runs apart that differ only in an
                unmarked field.
        """
        # The hash covers the *config* portion of the snapshot
        # (``TraceConfig.configuration_digest``), not these bytes: the file
        # also carries ``task`` and ``task_id``, so hashing it gave every
        # task in a sweep a different config_hash.
        config_bytes = config.model_dump_json(indent=2).encode()
        if config_hash is None:
            config_hash = config.configuration_digest()
        now = datetime.now(UTC)
        manifest = TraceManifest(
            format_version=TRACE_FORMAT_VERSION,
            run_id=self._run_id,
            last_step_completed=-1,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
            config_hash=config_hash,
        )

        # config.json first and durable, the manifest that makes the run
        # exist after it — never a manifest whose config is not there.
        directories = fsio.write_durable(self._run_dir / CONFIG_FILE, config_bytes)
        own_dirs = (STEPS_DIR, OBSERVATIONS_DIR, MESSAGES_DIR, EVALS_DIR)
        for name in own_dirs:
            (self._run_dir / name).mkdir(exist_ok=True)
        # The run directory holds config.json and the subdirectories;
        # ``directories`` adds the parent of each directory created for it —
        # ``traces/``, which holds the run's own name, and on a sweep's first
        # run ``traces/``'s parent. No later commit fsyncs a directory
        # outside the run, so this is the only place the run's name is made
        # durable.
        _fsync_all(directories | {self._run_dir})
        fsio.atomic_write(self._run_dir / MANIFEST_FILE, _manifest_bytes(manifest))

        self._manifest = manifest
        self._committed = set()
        # The WAL's own directories, and the one it creates on demand: no
        # file may ever take one of these names.
        self._committed_dirs = {fold_path(name) for name in (*own_dirs, OUTPUT_FORMATS_DIR)}
        self._committed_blobs = set()
        self._durable_dirs = {self._run_dir / name for name in own_dirs}

    def commit_step(
        self,
        step: int,
        record: StepRecord,
        *,
        fragments: Sequence[EncodedFragment] = (),
        llm_calls: Sequence[EncodedLLMCall] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """Commit one step; when this returns, it is durable.

        Args:
            step: The step index. Must match ``record.step`` and must not
                have been committed already.
            record: The step's ``StepRecord``, written to ``steps/NNN.json``.
            fragments: The step's observation, from ``encode_observation``.
            llm_calls: Every LLM call of the step, in start order.
            extra_files: Files the step carries beside its own records,
                listed in ``steps/NNN.extra.json``.

        Raises:
            ValueError: A path breaks a rule of §5.2, a content-addressed
                file is not at its digest, or the step is already
                committed — before any byte is written.
            OSError: Writing or ``fsync``-ing failed; the manifest is
                unchanged.
        """
        manifest = self._require_manifest()
        if record.step != step:
            raise ValueError(f"commit_step({step}) was handed the record of step {record.step}")
        if any(entry.step == step for entry in manifest.steps):
            raise ValueError(f"step {step} is already committed to {self._run_dir}")

        plan = _Plan(self)
        plan.add_fragments(fragments)
        plan.add_calls(llm_calls)
        plan.add_extra(extra_files)
        plan.add_record(
            TraceFile(step_record_path(step), record.model_dump_json(indent=2).encode())
        )
        extra_index = plan.add_extra_index(step_extra_index_path(step))

        entry = StepManifestEntry(
            step=step,
            step_file=step_record_path(step),
            fragments={
                item.key: FragmentFiles(
                    file=item.file.rel_path,
                    artifact=item.artifact.rel_path if item.artifact is not None else None,
                    failed=item.failed,
                )
                for item in fragments
            },
            llm_calls=[call.file.rel_path for call in llm_calls],
            extra_index=extra_index,
        )
        self._commit(
            plan,
            manifest.model_copy(
                update={
                    "steps": [*manifest.steps, entry],
                    "last_step_completed": max(manifest.last_step_completed, step),
                    "output_formats": [*manifest.output_formats, *plan.output_formats],
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    def commit_trailing(
        self,
        *,
        llm_calls: Sequence[EncodedLLMCall] = (),
        extra_files: Sequence[TraceFile] = (),
    ) -> None:
        """Commit files that belong to no committed step, by the same protocol.

        The LLM calls of a step the Loop never surrendered (timeout,
        external cancellation, crash), and whatever a producer flushes at
        the end of a run. No ``StepRecord`` is written and
        ``last_step_completed`` does not move: claiming a step would put an
        action-less phantom in the manifest. ``manifest.trailing`` gains one
        entry; its extra files are indexed in ``steps/trailing_KK.extra.json``.
        Committing nothing is a no-op.
        """
        manifest = self._require_manifest()
        if not llm_calls and not extra_files:
            return

        plan = _Plan(self)
        plan.add_calls(llm_calls)
        plan.add_extra(extra_files)
        extra_index = plan.add_extra_index(trailing_extra_index_path(len(manifest.trailing)))

        entry = TrailingEntry(
            llm_calls=[call.file.rel_path for call in llm_calls], extra_index=extra_index
        )
        self._commit(
            plan,
            manifest.model_copy(
                update={
                    "trailing": [*manifest.trailing, entry],
                    "output_formats": [*manifest.output_formats, *plan.output_formats],
                    "updated_at": datetime.now(UTC),
                }
            ),
        )

    def write_result(self, result: AgentResult) -> None:
        """Write ``result.json`` atomically; ``RunTrace.result`` reads it back as an ``AgentResult``."""
        self._require_manifest()
        fsio.atomic_write(self._run_dir / RESULT_FILE, result.model_dump_json(indent=2).encode())

    def finalize(self, status: RunStatus) -> None:
        """Record the terminal status — a trace stuck at ``running`` is one no evaluator touches."""
        manifest = self._require_manifest()
        self._write_manifest(
            manifest.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
        )

    # ── internals ─────────────────────────────────────────────

    def _require_manifest(self) -> TraceManifest:
        if self._manifest is None:
            msg = "WALWriter not initialized — call init_run() first"
            raise RuntimeError(msg)
        return self._manifest

    def _commit(self, plan: _Plan, manifest: TraceManifest) -> None:
        """Steps 1–4. Nothing in memory changes unless the manifest rewrite succeeded."""
        directories: set[Path] = set()
        for item in plan.content:  # 1. content files
            directories |= fsio.write_durable(self._run_dir / item.rel_path, item.data)
        for item in plan.records:  # 2. step record, extra index
            directories |= fsio.write_durable(self._run_dir / item.rel_path, item.data)
        # A directory's name is an entry in its parent. ``write_durable``
        # reports the directories *it* created; one a failed attempt created
        # already exists by now, and its name is not durable either.
        undurable = {
            self._run_dir / ancestor
            for item in (*plan.content, *plan.records)
            for ancestor in _ancestors(item.rel_path)
        } - self._durable_dirs
        directories |= {directory.parent for directory in undurable}
        _fsync_all(directories)  # 3. each directory once
        self._write_manifest(manifest)  # 4. commit point
        self._committed |= plan.claimed
        self._committed_dirs |= plan.dirs
        self._committed_blobs |= plan.blobs
        self._durable_dirs |= undurable

    def _write_manifest(self, manifest: TraceManifest) -> None:
        fsio.atomic_write(self._run_dir / MANIFEST_FILE, _manifest_bytes(manifest))
        self._manifest = manifest


@dataclass
class _Plan:
    """One commit, validated and encoded before any byte is written."""

    wal: WALWriter
    content: list[TraceFile] = field(default_factory=list)
    records: list[TraceFile] = field(default_factory=list)
    #: Extra-file index entries, in the order the producer handed the files over.
    extra: list[ExtraFileRecord] = field(default_factory=list)
    #: Folded paths (``fold_path``) this commit adds to the run.
    claimed: set[str] = field(default_factory=set)
    #: Folded directories those paths run through.
    dirs: set[str] = field(default_factory=set)
    #: Content-addressed paths this commit writes.
    blobs: set[str] = field(default_factory=set)
    #: Output schemas this commit is the first to carry.
    output_formats: list[str] = field(default_factory=list)

    def add_fragments(self, fragments: Iterable[EncodedFragment]) -> None:
        keys: set[str] = set()
        for item in fragments:
            if item.key in keys:
                raise ValueError(f"fragment {item.key!r} appears twice in one step")
            keys.add(item.key)
            self._own(item.file, OBSERVATIONS_DIR)
            if item.artifact is not None:
                self._own(item.artifact, OBSERVATIONS_DIR)

    def add_calls(self, calls: Iterable[EncodedLLMCall]) -> None:
        for call in calls:
            if _within(call.file.rel_path, OUTPUT_FORMATS_DIR):
                raise ValueError(f"{call.file.rel_path!r}: a call record cannot live among schemas")
            self._own(call.file, MESSAGES_DIR)
            schema = call.output_format
            if schema is None:
                continue
            # Before the parent test below: ``PurePosixPath`` drops ``.`` and
            # empty segments, so ``messages/output_formats/./<sha>.json``
            # would pass it.
            check_relative_path(schema.rel_path)
            if not schema.content_addressed or PurePosixPath(
                schema.rel_path
            ).parent != PurePosixPath(OUTPUT_FORMATS_DIR):
                raise ValueError(
                    f"{schema.rel_path!r}: an output schema is content-addressed, "
                    f"directly under {OUTPUT_FORMATS_DIR}/"
                )
            digest = _check_address(schema)
            if self._blob(schema):
                self.output_formats.append(digest)

    def add_extra(self, files: Iterable[TraceFile]) -> None:
        for item in files:
            check_relative_path(item.rel_path)
            _check_not_wal_owned(item.rel_path)
            digest = sha256_hex(item.data)
            if item.content_addressed:
                _check_address(item, digest)
                self._blob(item)
            else:
                self._claim(item.rel_path)
                self.content.append(item)
            self.extra.append(
                ExtraFileRecord(
                    path=item.rel_path,
                    size=len(item.data),
                    sha256=digest,
                    content_addressed=item.content_addressed,
                    meta=dict(item.meta),
                )
            )

    def add_record(self, item: TraceFile) -> None:
        self._claim(item.rel_path)
        self.records.append(item)

    def add_extra_index(self, rel_path: str) -> str | None:
        """Encode the extra-file index, if this commit carries any extra file."""
        if not self.extra:
            return None
        # Encoded here, in validation: a ``meta`` value that cannot be
        # serialized must fail before the first byte, not after.
        data = ExtraFileIndex(files=self.extra).model_dump_json(indent=2).encode()
        self.add_record(TraceFile(rel_path, data))
        return rel_path

    def _own(self, item: TraceFile, directory: str) -> None:
        """A file whose name the encoding module decides: under *directory*, written once."""
        # Not redundant with ``_within``, a prefix test that ``..`` walks
        # straight through. A fragment's file is named by its key, which is
        # free text on a ``FragmentFailure``: the key ``x/../../../escaped``
        # files it at ``observations/000_x/../../../escaped.json``, outside
        # the run directory.
        check_relative_path(item.rel_path)
        if item.content_addressed or not _within(item.rel_path, directory):
            raise ValueError(f"{item.rel_path!r}: expected a per-step file under {directory}/")
        self._claim(item.rel_path)
        self.content.append(item)

    def _claim(self, rel_path: str) -> None:
        """Add *rel_path* to the run, or refuse it before a byte is written.

        Beyond immutability, a path is a file or a directory of the run,
        never both: ``env/a`` and ``env/a/b``, in one commit or across two,
        would otherwise fail with ``NotADirectoryError`` half-way through
        the writes — or, for a name the WAL writes later (``result.json``),
        turn the run's close-out into an ``IsADirectoryError``.
        """
        folded = fold_path(rel_path)
        if folded in self.wal._committed:
            raise ValueError(
                f"{rel_path!r} is already committed to {self.wal.run_dir}: committed files are "
                f"immutable (only a content-addressed file may be committed again)"
            )
        if folded in self.claimed:
            raise ValueError(f"{rel_path!r} appears twice in one commit")
        if folded in self.wal._committed_dirs or folded in self.dirs:
            raise ValueError(
                f"{rel_path!r} is a directory of {self.wal.run_dir}: a path is a file or a "
                f"directory, never both"
            )
        ancestors = _ancestors(folded)
        for ancestor in ancestors:
            if ancestor in self.wal._committed or ancestor in self.claimed:
                raise ValueError(
                    f"{rel_path!r} puts a directory where {ancestor!r} is a file of "
                    f"{self.wal.run_dir}: a path is a file or a directory, never both"
                )
        self.claimed.add(folded)
        self.dirs.update(ancestors)

    def _blob(self, item: TraceFile) -> bool:
        """Schedule a content-addressed file unless it is already on disk; True if new."""
        if item.rel_path in self.wal._committed_blobs or item.rel_path in self.blobs:
            return False
        self._claim(item.rel_path)
        self.blobs.add(item.rel_path)
        self.content.append(item)
        return True


def _check_not_wal_owned(rel_path: str) -> None:
    """Not a name the WAL owns — nor one that would make such a name a directory.

    ``result.json/evidence.txt`` would create a *directory* ``result.json``,
    and the run's close-out would then fail to write its result.
    """
    head = fold_path(rel_path.split("/", 1)[0])
    if head in WAL_OWNED_FILES or head in WAL_OWNED_DIRS:
        raise ValueError(
            f"{rel_path!r} is a name the WAL owns ({sorted(WAL_OWNED_FILES)}, or anything "
            f"under {sorted(f'{d}/' for d in WAL_OWNED_DIRS)}); extra files go elsewhere"
        )


def _ancestors(rel_path: str) -> list[str]:
    """The directories *rel_path* runs through: ``a/b/c`` → ``["a", "a/b"]``."""
    parts = rel_path.split("/")[:-1]
    return ["/".join(parts[: depth + 1]) for depth in range(len(parts))]


def _check_address(item: TraceFile, digest: str | None = None) -> str:
    """A content-addressed path must carry its data's full sha256 — whole or as ``ab/cdef…``."""
    digest = digest or sha256_hex(item.data)
    path = PurePosixPath(item.rel_path)

    def names(stem: str) -> bool:
        return path.name == stem or path.name.startswith(f"{stem}.")

    if names(digest) or (path.parent.name == digest[:2] and names(digest[2:])):
        return digest
    raise ValueError(
        f"{item.rel_path!r} is marked content-addressed but does not name its data's sha256 "
        f"({digest})"
    )


def _within(rel_path: str, directory: str) -> bool:
    """*rel_path* starts with ``directory/`` — a prefix test, sound only after ``check_relative_path``."""
    return fold_path(rel_path).startswith(f"{fold_path(directory)}/")


def _fsync_all(directories: Iterable[Path]) -> None:
    # Deepest first, so a new directory's own entries are durable before
    # the entry naming it in its parent is.
    for directory in sorted(set(directories), key=lambda d: len(d.parts), reverse=True):
        fsio.fsync_dir(directory)


def _manifest_bytes(manifest: TraceManifest) -> bytes:
    return manifest.model_dump_json(indent=2).encode()
