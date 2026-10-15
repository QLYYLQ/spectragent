"""The commit protocol of ``WALWriter.init_run`` / ``commit_step`` / ``commit_trailing``.

``docs/architecture_design.md`` §5.2: validate everything, write every content
file with a data ``fsync`` and no directory ``fsync``, write the step record
and the extra-file index, ``fsync`` each distinct directory once, and only
then rewrite the manifest — the commit point. ``init_run`` is the first
commit: ``config.json`` and the run directory's own name before the first
manifest. These tests observe the real primitives (``fsio.write_durable`` /
``fsio.fsync_dir``, and ``os.fsync`` itself for the budget test) rather than
trusting the WAL's own account.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import EncodedFragment, EncodedLLMCall, TraceFile, sha256_hex
from spectragent.core.trace.models import TraceConfig, TraceManifest, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from tests._helpers.trace_builder import (
    Reply,
    agent_result,
    blob,
    completed_call,
    extra,
    failure,
    new_wal,
    screenshot,
    step_record,
)

#: Default wall-clock budget for committing 1000 small files in one step.
#: ~0.2 s measured on APFS; Linux ext4/xfs, where ``fsync`` really flushes,
#: is where the headroom goes. Override for a slow disk with
#: ``SPECTRAGENT_WAL_COMMIT_BUDGET_S``.
#: TODO(linux): calibrated on APFS only; ext4 and xfs have not been measured.
_DEFAULT_BUDGET_S = 3.0


@dataclass
class IOLog:
    """Every durable write and directory ``fsync`` the WAL issues, in order."""

    run_dir: Path
    events: list[tuple[str, str]] = field(default_factory=list)
    #: Raise ``EIO`` for the first event this returns True for.
    fail_on: Callable[[str, str], bool] | None = None

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.run_dir).as_posix()
        except ValueError:
            return str(path)

    def record(self, kind: str, path: Path) -> None:
        rel = self.rel(path)
        if self.fail_on is not None and self.fail_on(kind, rel):
            self.fail_on = None
            raise OSError(errno.EIO, f"injected {kind} failure", str(path))
        self.events.append((kind, rel))

    def writes(self) -> list[str]:
        return [rel for kind, rel in self.events if kind == "write"]

    def dir_fsyncs(self) -> list[str]:
        return [rel for kind, rel in self.events if kind == "fsync_dir"]


@pytest.fixture()
def io_log(monkeypatch, tmp_path) -> IOLog:
    log = IOLog(tmp_path / "run-test")
    real_write, real_fsync_dir = fsio.write_durable, fsio.fsync_dir

    def write_durable(path: Path, data: bytes) -> set[Path]:
        log.record("write", path)
        return real_write(path, data)

    def fsync_dir(path: Path) -> None:
        log.record("fsync_dir", path)
        real_fsync_dir(path)

    # ``atomic_write`` resolves both through the module at call time, so the
    # manifest commit is observed through the same two seams.
    monkeypatch.setattr(fsio, "write_durable", write_durable)
    monkeypatch.setattr(fsio, "fsync_dir", fsync_dir)
    return log


def _manifest_bytes(wal: WALWriter) -> bytes:
    return (wal.run_dir / "manifest.json").read_bytes()


def _a_full_step(step: int = 0) -> dict:
    return {
        "fragments": [screenshot(step), failure(step, "browser.browser_meta")],
        "llm_calls": [completed_call(step, 0, "go", output_format=Reply)],
        "extra_files": [
            extra(f"env/steps/{step:03d}/browser.network/a.json", b"a"),
            blob(f"shared body {step}".encode()),
        ],
    }


def _dirs_a_first_full_step_fsyncs(step: int = 0) -> set[str]:
    """Every directory whose entries ``_a_full_step`` adds to a fresh run.

    Each written file's directory, plus the parent of each directory the
    WAL had not made durable yet — ``env/`` and everything under it,
    ``messages/output_formats/`` — the run directory included.
    """
    blob_dir = PurePosixPath(_a_full_step(step)["extra_files"][1].rel_path).parent
    return {
        ".",
        "steps",
        "observations",
        "messages",
        "messages/output_formats",
        "metrics",
        "env",
        "env/steps",
        f"env/steps/{step:03d}",
        f"env/steps/{step:03d}/browser.network",
        "env/blobs",
        "env/blobs/sha256",
        blob_dir.as_posix(),
    }


def _batch_dir_fsyncs(io_log: IOLog) -> set[str]:
    """The directory ``fsync``-s of the last commit, before its manifest write."""
    manifest_write = len(io_log.events) - 1 - io_log.events[::-1].index(("write", "manifest.json"))
    return {rel for kind, rel in io_log.events[:manifest_write] if kind == "fsync_dir"}


class TestInitRun:
    """The first commit: ``config.json``, then every directory name, then the manifest.

    A run exists once its manifest does — ``discover_traces`` looks for
    nothing else — so what the manifest implies must be durable before it
    is written: ``config.json`` (a listed run whose ``RunTrace.config``
    raises is a trace a sweep accepts and cannot read) and the run
    directory's own name, an entry in ``traces/``. No later commit
    ``fsync``-s a directory outside the run, so if ``init_run`` skipped
    that, a power loss could take back a run whose every step reported
    durable.
    """

    @staticmethod
    def _wal(io_log: IOLog, base: Path) -> WALWriter:
        wal = WALWriter(base, "run-test")
        io_log.run_dir = wal.run_dir
        return wal

    @pytest.mark.parametrize(
        "first_run", [False, True], ids=["traces-exists", "first-run-creates-traces"]
    )
    def test_config_and_directory_names_are_durable_before_the_first_manifest(
        self, tmp_path, io_log, first_run
    ):
        base = tmp_path / "traces"
        if not first_run:
            base.mkdir()
        wal = self._wal(io_log, base)

        wal.init_run(TraceConfig(task="find the answer"))

        assert io_log.events == [
            ("write", "config.json"),
            # Deepest first: the run directory (config.json, steps/, …), the
            # entry naming it in traces/, and — when this run created
            # traces/ — the entry naming that.
            ("fsync_dir", "."),
            ("fsync_dir", io_log.rel(base)),
            *([("fsync_dir", io_log.rel(tmp_path))] if first_run else []),
            ("write", "manifest.json"),
            ("fsync_dir", "."),
        ]
        for name in ("steps", "observations", "messages", "metrics", "evals"):
            assert (wal.run_dir / name).is_dir(), name

    STAGES: dict[str, Callable[[str, str], bool]] = {
        "config.json": lambda kind, rel: kind == "write" and rel == "config.json",
        "run directory fsync": lambda kind, rel: kind == "fsync_dir" and rel == ".",
        "traces/ fsync": lambda kind, rel: kind == "fsync_dir" and rel != ".",
        "manifest write": lambda kind, rel: kind == "write" and rel == "manifest.json",
    }

    @pytest.mark.parametrize("stage", list(STAGES))
    def test_a_failure_before_the_manifest_leaves_no_run(self, tmp_path, io_log, stage):
        wal = self._wal(io_log, tmp_path / "traces")
        io_log.fail_on = self.STAGES[stage]

        with pytest.raises(OSError, match="injected"):
            wal.init_run(TraceConfig(task="find the answer"))

        # No manifest, no run: a sweep skips the directory instead of
        # listing a run whose config or name a power loss can take back.
        assert not (wal.run_dir / "manifest.json").exists()
        assert wal.manifest is None
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.commit_step(0, step_record(0))


class TestOrdering:
    def test_content_then_record_then_index_then_directories_then_manifest(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        io_log.events.clear()

        wal.commit_step(0, step_record(0), **_a_full_step())

        kinds = [kind for kind, _ in io_log.events]
        writes = io_log.writes()
        record_at = writes.index("steps/000.json")
        assert writes[record_at + 1] == "steps/000.extra.json"
        assert writes[-1] == "manifest.json"
        content = writes[:record_at]
        assert set(content) == {
            "observations/000_browser.screenshot.png",
            "observations/000_browser.screenshot.json",
            "observations/000_browser.browser_meta.json",
            "messages/000_call_00.json",
            "metrics/000_call_00.json",
            f"messages/output_formats/{_schema_sha()}.json",
            "env/steps/000/browser.network/a.json",
            _a_full_step()["extra_files"][1].rel_path,
        }
        # Every write (content, record, index) precedes every directory
        # fsync of the batch, which precede the manifest write; the
        # manifest's own directory fsync comes last.
        first_dir_fsync = kinds.index("fsync_dir")
        manifest_write = io_log.events.index(("write", "manifest.json"))
        assert all(kind == "write" for kind in kinds[:first_dir_fsync])
        assert all(kind == "fsync_dir" for kind in kinds[first_dir_fsync:manifest_write])
        assert io_log.events[manifest_write + 1 :] == [("fsync_dir", ".")]

    def test_each_directory_is_fsynced_once_per_batch(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        io_log.events.clear()

        wal.commit_step(0, step_record(0), **_a_full_step())

        manifest_write = io_log.events.index(("write", "manifest.json"))
        batch = [rel for kind, rel in io_log.events[:manifest_write] if kind == "fsync_dir"]
        assert len(batch) == len(set(batch)), batch
        written_into = {str(Path(rel).parent.as_posix()) for rel in io_log.writes()[:-1]}
        # Every directory written into, plus the parent of each directory
        # this commit created (env/, env/steps, …, env/blobs/sha256/xx).
        assert written_into <= set(batch)
        assert set(batch) == _dirs_a_first_full_step_fsyncs()

    def test_deepest_directory_first(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        io_log.events.clear()

        wal.commit_step(0, step_record(0), **_a_full_step())

        manifest_write = io_log.events.index(("write", "manifest.json"))
        batch = [rel for kind, rel in io_log.events[:manifest_write] if kind == "fsync_dir"]
        depths = [0 if rel == "." else rel.count("/") + 1 for rel in batch]
        assert depths == sorted(depths, reverse=True)


def _schema_sha() -> str:
    from spectragent.core.trace.encoding import encode_output_format

    return encode_output_format(Reply).ref.sha256


class TestAFailedCommitLeavesTheManifestAlone:
    """Whatever stage fails, the commit point is never reached.

    The manifest on disk *and* the writer's in-memory copy stay as they were
    — a later ``finalize`` must not write out a step that never committed —
    and the same commit can be retried: the half-written files are orphans,
    not committed paths.
    """

    STAGES: dict[str, Callable[[str, str], bool]] = {
        "content file": lambda kind, rel: kind == "write" and rel.startswith("observations/"),
        "output schema": lambda kind, rel: kind == "write" and "output_formats" in rel,
        "extra file": lambda kind, rel: kind == "write" and rel.startswith("env/"),
        "step record": lambda kind, rel: kind == "write" and rel == "steps/000.json",
        "extra index": lambda kind, rel: kind == "write" and rel == "steps/000.extra.json",
        "directory fsync": lambda kind, rel: kind == "fsync_dir",
        "manifest write": lambda kind, rel: kind == "write" and rel == "manifest.json",
    }

    @pytest.mark.parametrize("stage", list(STAGES))
    def test_injected_failure(self, tmp_path, io_log, stage):
        wal = new_wal(tmp_path)
        before_disk, before_memory = _manifest_bytes(wal), wal.manifest
        io_log.fail_on = self.STAGES[stage]

        with pytest.raises(OSError, match="injected"):
            wal.commit_step(0, step_record(0), **_a_full_step())

        assert _manifest_bytes(wal) == before_disk
        assert wal.manifest is before_memory
        assert RunTrace(wal.run_dir).step(0) is None

        # Retry: nothing of the failed attempt was claimed.
        io_log.events.clear()
        wal.commit_step(0, step_record(0), **_a_full_step())
        trace = RunTrace(wal.run_dir)
        assert trace.step(0) is not None
        assert trace.manifest.output_formats == [_schema_sha()]
        assert len(trace.extra_files(0)) == 2
        # Nor was anything made durable: the directories the failed attempt
        # created exist now, but their names are still only in the page
        # cache, so the retry fsyncs every parent a first commit would.
        assert _batch_dir_fsyncs(io_log) == _dirs_a_first_full_step_fsyncs()

    def test_a_later_commit_makes_a_failed_commits_directories_durable(self, tmp_path, io_log):
        """Not only a retry of the same step: whatever commits next under them.

        The shape M28's run end takes — a step commit fails, ``commit_trailing``
        then flushes evidence into the directories that commit created.
        """
        wal = new_wal(tmp_path)
        shared = blob(b"response body")
        io_log.fail_on = lambda kind, rel: kind == "write" and rel == "steps/000.json"
        with pytest.raises(OSError, match="injected"):
            wal.commit_step(0, step_record(0), extra_files=[shared])
        io_log.events.clear()

        wal.commit_trailing(extra_files=[shared])

        shard = PurePosixPath(shared.rel_path).parent.as_posix()
        assert {".", "env", "env/blobs", "env/blobs/sha256", shard} <= _batch_dir_fsyncs(io_log)
        assert RunTrace(wal.run_dir).read_extra(shared.rel_path) == b"response body"

    def test_a_directory_a_commit_made_durable_is_not_fsynced_again(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), extra_files=[extra("env/steps/000/net/a.json", b"a")])
        io_log.events.clear()

        wal.commit_step(1, step_record(1), extra_files=[extra("env/steps/000/net/b.json", b"b")])

        # Only the directories that gained an entry: the file's, the record's.
        assert _batch_dir_fsyncs(io_log) == {"env/steps/000/net", "steps"}

    def test_a_failed_trailing_commit_leaves_it_alone_too(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.fail_on = lambda kind, rel: kind == "fsync_dir"

        with pytest.raises(OSError, match="injected"):
            wal.commit_trailing(llm_calls=[completed_call(0, 0, "x")])

        assert _manifest_bytes(wal) == before
        assert wal.manifest is not None
        assert wal.manifest.trailing == []


class TestContentAddressedFiles:
    def test_a_blob_is_written_once_and_listed_by_every_commit(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        shared = blob(b"one response body")
        wal.commit_step(0, step_record(0), extra_files=[shared])
        io_log.events.clear()

        wal.commit_step(1, step_record(1), extra_files=[shared])

        assert shared.rel_path not in io_log.writes()
        trace = RunTrace(wal.run_dir)
        assert [r.path for r in trace.extra_files(0)] == [shared.rel_path]
        assert [r.path for r in trace.extra_files(1)] == [shared.rel_path]
        assert trace.read_extra(shared.rel_path) == b"one response body"

    def test_the_same_blob_twice_in_one_commit_is_written_once(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        io_log.events.clear()
        shared = blob(b"twice")
        wal.commit_step(
            0,
            step_record(0),
            extra_files=[shared, TraceFile(shared.rel_path, shared.data, True, {"n": 2})],
        )

        assert io_log.writes().count(shared.rel_path) == 1
        records = RunTrace(wal.run_dir).extra_files(0)
        assert [r.meta for r in records] == [{}, {"n": 2}]

    def test_an_output_schema_is_committed_once_per_run(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        wal.commit_step(
            0, step_record(0), llm_calls=[completed_call(0, 0, "a", output_format=Reply)]
        )
        io_log.events.clear()
        wal.commit_step(
            1, step_record(1), llm_calls=[completed_call(1, 0, "b", output_format=Reply)]
        )
        wal.commit_trailing(llm_calls=[completed_call(2, 0, "c", output_format=Reply)])

        assert not [rel for rel in io_log.writes() if "output_formats" in rel]
        assert RunTrace(wal.run_dir).manifest.output_formats == [_schema_sha()]

    def test_a_path_that_does_not_carry_the_digest_is_refused(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()
        other = sha256_hex(b"other data")

        for rel in (
            "env/blobs/sha256/not-a-digest",
            f"env/blobs/sha256/{other[:2]}/{other[2:]}",  # someone else's digest
            f"env/{sha256_hex(b'payload')}/inner.bin",  # digest as a directory only
        ):
            with pytest.raises(ValueError, match="does not name its data's sha256"):
                wal.commit_step(0, step_record(0), extra_files=[TraceFile(rel, b"payload", True)])
        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before

    def test_whole_and_sharded_digests_are_both_addresses(self, tmp_path):
        wal = new_wal(tmp_path)
        digest = sha256_hex(b"payload")
        wal.commit_step(
            0,
            step_record(0),
            extra_files=[
                TraceFile(f"env/blobs/{digest}.json", b"payload", True),
                TraceFile(f"env/shards/{digest[:2]}/{digest[2:]}", b"payload", True),
            ],
        )
        assert len(RunTrace(wal.run_dir).extra_files(0)) == 2

    def test_an_output_schema_must_be_content_addressed(self, tmp_path):
        wal = new_wal(tmp_path)
        call = completed_call(0, 0, "go", output_format=Reply)
        assert call.output_format is not None
        bad = EncodedLLMCall(
            call.file, TraceFile(call.output_format.rel_path, call.output_format.data)
        )
        with pytest.raises(ValueError, match="content-addressed"):
            wal.commit_step(0, step_record(0), llm_calls=[bad])

    def test_a_committed_blob_path_with_other_bytes_is_refused(self, tmp_path, io_log):
        """Deduplication skips the write, never the check: the path must name *these* bytes.

        Accepting it would index a size and sha256 the file on disk does not
        have, and every ``read_extra`` of the run would then refuse the
        disagreeing indexes.
        """
        wal = new_wal(tmp_path)
        shared = blob(b"original body")
        wal.commit_step(0, step_record(0), extra_files=[shared])
        before = _manifest_bytes(wal)
        io_log.events.clear()

        forged = TraceFile(shared.rel_path, b"different body", content_addressed=True)
        with pytest.raises(ValueError, match="does not name its data's sha256"):
            wal.commit_step(1, step_record(1), extra_files=[forged])

        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before
        assert RunTrace(wal.run_dir).read_extra(shared.rel_path) == b"original body"

    def test_a_case_variant_of_a_committed_blob_collides(self, tmp_path):
        wal = new_wal(tmp_path)
        shared = blob(b"body")
        wal.commit_step(0, step_record(0), extra_files=[shared])
        variant = TraceFile(shared.rel_path.replace("env/blobs", "env/Blobs"), b"body", True)
        with pytest.raises(ValueError, match="already committed"):
            wal.commit_step(1, step_record(1), extra_files=[variant])


#: Each input channel whose names ``encoding.py`` decides: the directory its own
#: check wants a path under, and a well-formed name there (``{sha}`` is the
#: requested schema's digest).
_CHANNELS = {
    "fragment": ("observations", "000_x.json"),
    "artifact": ("observations", "000_k.png"),
    "call record": ("messages", "000_call_00.json"),
    "output schema": ("messages/output_formats", "{sha}.json"),
}


def _bad_path(directory: str, name: str, shape: str) -> str:
    """*name* under *directory*, spelled with one thing ``check_relative_path`` refuses."""
    stem = name.split(".", 1)[0]
    if shape == "climbs out":  # one level past the run directory
        up = "/".join([".."] * (directory.count("/") + 3))
        return f"{directory}/{stem}/{up}/escaped.json"
    if shape == "dot segment":
        return f"{directory}/./{name}"
    if shape == "empty segment":
        return f"{directory}//{name}"
    if shape == "backslash":
        return f"{directory}/{stem}\\..\\..\\{name}"
    raise AssertionError(shape)


def _on_channel(channel: str, rel: str) -> dict[str, Any]:
    """``commit_step`` arguments that carry *rel* on one input channel, all else well-formed."""
    if channel == "fragment":
        return {"fragments": [EncodedFragment("x", TraceFile(rel, b"{}"), None, True)]}
    if channel == "artifact":
        shot = screenshot(0)
        assert shot.artifact is not None
        return {
            "fragments": [
                EncodedFragment(shot.key, shot.file, TraceFile(rel, shot.artifact.data), False)
            ]
        }
    if channel == "call record":
        return {"llm_calls": [EncodedLLMCall(TraceFile(rel, completed_call(0, 0, "go").file.data))]}
    if channel == "output schema":
        call = completed_call(0, 0, "go", output_format=Reply)
        assert call.output_format is not None
        schema = TraceFile(rel.format(sha=_schema_sha()), call.output_format.data, True)
        return {"llm_calls": [EncodedLLMCall(call.file, schema)]}
    raise AssertionError(channel)


def _nothing_beside_the_run(tmp_path: Path) -> bool:
    return [child.name for child in tmp_path.iterdir()] == ["run-test"]


class TestPathRules:
    @pytest.mark.parametrize(
        "rel",
        [
            "",
            "/etc/passwd",
            "env\\steps\\x",
            "env//x",
            "env/x/",
            "./env/x",
            "env/./x",
            "env/../steps/000.json",
            "..",
            "env/\x00",
        ],
    )
    def test_a_path_must_be_relative_posix_without_dot_segments(self, tmp_path, io_log, rel):
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()
        with pytest.raises(ValueError):
            wal.commit_step(0, step_record(0), extra_files=[extra(rel, b"x")])
        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before

    @pytest.mark.parametrize("shape", ["climbs out", "dot segment", "empty segment", "backslash"])
    @pytest.mark.parametrize("channel", list(_CHANNELS))
    def test_every_channel_obeys_the_same_path_rule(self, tmp_path, io_log, channel, shape):
        """Not only extra files: the names ``encoding.py`` decides are checked the same way.

        Most of these paths pass their channel's own directory test — a
        prefix test for ``observations/`` and ``messages/``, and for a schema
        ``PurePosixPath``'s parent, which drops a ``.`` or empty segment — so
        ``check_relative_path`` is what stands between them and the disk. A
        fragment key is free text (``FragmentFailure.fragment_id``), and
        ``..`` walks straight through a prefix test, out of the run. The
        message pins *which* rule refused it.
        """
        wal = new_wal(tmp_path)
        before_disk, before_memory = _manifest_bytes(wal), wal.manifest
        io_log.events.clear()
        rel = _bad_path(*_CHANNELS[channel], shape)

        with pytest.raises(ValueError, match="segment|relative POSIX path"):
            wal.commit_step(0, step_record(0), **_on_channel(channel, rel))

        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before_disk
        assert wal.manifest is before_memory
        assert _nothing_beside_the_run(tmp_path)
        wal.commit_step(0, step_record(0), fragments=[screenshot(0)])  # the run goes on

    def test_a_failure_filed_under_a_key_that_climbs_out_is_refused(self, tmp_path, io_log):
        """Through the real encoder: a ``FragmentFailure`` is filed under its key as given."""
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()
        escaping = failure(0, "x/../../../escaped")
        assert escaping.file.rel_path == "observations/000_x/../../../escaped.json"

        with pytest.raises(ValueError, match="'..' segment"):
            wal.commit_step(0, step_record(0), fragments=[escaping])

        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before
        assert _nothing_beside_the_run(tmp_path)

    @pytest.mark.parametrize(
        "commit",
        [
            {
                "llm_calls": [
                    EncodedLLMCall(TraceFile("messages/000_call_00/../../../x.json", b"{}"))
                ]
            },
            {"extra_files": [TraceFile("env/../../x.txt", b"x")]},
        ],
        ids=["call record", "extra file"],
    )
    def test_commit_trailing_obeys_it_too(self, tmp_path, io_log, commit):
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()

        with pytest.raises(ValueError, match="'..' segment"):
            wal.commit_trailing(**commit)

        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before
        assert _nothing_beside_the_run(tmp_path)

    @pytest.mark.parametrize(
        "rel",
        [
            "manifest.json",
            "config.json",
            "result.json",
            "Result.JSON",
            "steps/007.json",
            "Steps/007.json",
            "observations/000_browser.dom.json",
            "messages/000_call_00.json",
            "evals/judge.json",
            "steps",
        ],
    )
    def test_an_extra_file_cannot_take_a_name_the_wal_owns(self, tmp_path, io_log, rel):
        wal = new_wal(tmp_path)
        io_log.events.clear()
        with pytest.raises(ValueError, match="the WAL owns"):
            wal.commit_step(0, step_record(0), extra_files=[extra(rel, b"forged")])
        assert io_log.writes() == []

    @pytest.mark.parametrize(
        "rel",
        [
            "result.json/evidence.txt",
            "RESULT.JSON/evidence.txt",
            "manifest.json/x",
            "config.json/x",
        ],
    )
    def test_a_wal_file_name_cannot_become_a_directory(self, tmp_path, io_log, rel):
        """``result.json/x`` once made ``result.json`` a directory, and the close-out died on it."""
        wal = new_wal(tmp_path)
        io_log.events.clear()
        with pytest.raises(ValueError, match="the WAL owns"):
            wal.commit_step(0, step_record(0), extra_files=[extra(rel, b"evidence")])
        assert io_log.writes() == []

        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        trace = RunTrace(wal.run_dir)
        assert trace.result is not None
        assert trace.manifest.status == TraceStatus.COMPLETE

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("env/a", "env/a/b"),
            ("env/a/b", "env/a"),
            ("env/A", "env/a/b"),
            ("env/steps/000/x.arch/p", "env/steps/000/x.arch/p/q"),
        ],
    )
    def test_a_path_is_a_file_or_a_directory_never_both(self, tmp_path, io_log, first, second):
        """Refused in validation — not as ``NotADirectoryError`` half-way through the writes."""
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()
        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(
                0, step_record(0), extra_files=[extra(first, b"1"), extra(second, b"2")]
            )
        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before

    @pytest.mark.parametrize(
        ("first", "second"), [("env/a", "env/a/b"), ("env/a/b", "env/a"), ("env/a/b", "env/A")]
    )
    def test_never_both_across_commits_either(self, tmp_path, io_log, first, second):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), extra_files=[extra(first, b"1")])
        before = _manifest_bytes(wal)
        io_log.events.clear()

        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(1, step_record(1), extra_files=[extra(second, b"2")])

        assert io_log.writes() == []
        assert _manifest_bytes(wal) == before
        wal.commit_step(1, step_record(1), extra_files=[extra("env/c", b"3")])  # the run goes on

    def test_a_blob_shard_directory_cannot_become_a_file(self, tmp_path):
        wal = new_wal(tmp_path)
        shared = blob(b"body")
        wal.commit_step(0, step_record(0), extra_files=[shared])
        shard = PurePosixPath(shared.rel_path).parent.as_posix()
        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(1, step_record(1), extra_files=[extra(shard, b"a file")])

    def test_a_call_record_cannot_take_the_schema_directorys_name(self, tmp_path):
        wal = new_wal(tmp_path)
        call = completed_call(0, 0, "go")
        stray = EncodedLLMCall(TraceFile("messages/output_formats", call.file.data))
        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(0, step_record(0), llm_calls=[stray])

    def test_a_committed_file_is_immutable(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), extra_files=[extra("env/notes.txt", b"first")])
        io_log.events.clear()
        for rel in ("env/notes.txt", "env/NOTES.txt"):
            with pytest.raises(ValueError, match="already committed"):
                wal.commit_step(1, step_record(1), extra_files=[extra(rel, b"second")])
        assert io_log.writes() == []
        assert RunTrace(wal.run_dir).read_extra("env/notes.txt") == b"first"

    def test_one_commit_cannot_carry_a_path_twice(self, tmp_path):
        wal = new_wal(tmp_path)
        with pytest.raises(ValueError, match="twice in one commit"):
            wal.commit_step(
                0, step_record(0), extra_files=[extra("env/a", b"1"), extra("env/a", b"2")]
            )

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            # Precomposed and decomposed: one file on a normalization-insensitive APFS.
            (
                unicodedata.normalize("NFC", "env/café.json"),
                unicodedata.normalize("NFD", "env/café.json"),
            ),
            # Canonical caseless matching, not casefold(NFC(...)): the case fold of
            # ``Ś`` decomposes, the long s with a combining acute never composes.
            ("env/Ś.json", "env/\u017f\u0301.json"),
        ],
        ids=["nfc-nfd", "caseless"],
    )
    def test_two_spellings_of_one_name_are_one_path(self, tmp_path, io_log, first, second):
        """A trace copied to macOS must not hold two files the filesystem stores as one."""
        assert first != second
        wal = new_wal(tmp_path)
        with pytest.raises(ValueError, match="twice in one commit"):
            wal.commit_step(
                0, step_record(0), extra_files=[extra(first, b"1"), extra(second, b"2")]
            )
        wal.commit_step(0, step_record(0), extra_files=[extra(first, b"1")])
        io_log.events.clear()
        with pytest.raises(ValueError, match="already committed"):
            wal.commit_step(1, step_record(1), extra_files=[extra(second, b"2")])
        with pytest.raises(ValueError, match="never both"):
            wal.commit_step(1, step_record(1), extra_files=[extra(f"{second}/x", b"3")])
        assert io_log.writes() == []
        assert RunTrace(wal.run_dir).read_extra(first) == b"1"

    def test_a_fragment_lives_under_observations(self, tmp_path):
        wal = new_wal(tmp_path)
        good = screenshot(0)
        stray = EncodedFragment(good.key, TraceFile("env/shot.json", good.file.data), None, False)
        with pytest.raises(ValueError, match="under observations/"):
            wal.commit_step(0, step_record(0), fragments=[stray])

    def test_a_fragment_key_appears_once_per_step(self, tmp_path):
        wal = new_wal(tmp_path)
        with pytest.raises(ValueError, match="appears twice"):
            wal.commit_step(0, step_record(0), fragments=[screenshot(0), screenshot(0)])

    def test_a_call_record_is_not_a_schema(self, tmp_path):
        wal = new_wal(tmp_path)
        call = completed_call(0, 0, "go")
        stray = EncodedLLMCall(
            TraceFile(f"messages/output_formats/{'a' * 64}.json", call.file.data)
        )
        with pytest.raises(ValueError, match="among schemas"):
            wal.commit_step(0, step_record(0), llm_calls=[stray])


class TestCommitTrailing:
    def test_each_trailing_commit_gets_its_own_entry_and_index(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.commit_trailing(extra_files=[extra("env/final/a.txt", b"a")])
        wal.commit_trailing(
            llm_calls=[completed_call(0, 0, "x")], extra_files=[extra("env/final/b.txt", b"b")]
        )

        manifest = TraceManifest.model_validate_json(_manifest_bytes(wal))
        assert [entry.extra_index for entry in manifest.trailing] == [
            "steps/trailing_00.extra.json",
            "steps/trailing_01.extra.json",
        ]
        assert manifest.trailing[1].llm_calls == ["messages/000_call_00.json"]
        assert manifest.last_step_completed == -1
        trace = RunTrace(wal.run_dir)
        assert [r.path for r in trace.trailing_extra_files] == [
            "env/final/a.txt",
            "env/final/b.txt",
        ]

    def test_nothing_to_commit_is_a_no_op(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        before = _manifest_bytes(wal)
        io_log.events.clear()
        wal.commit_trailing()
        assert io_log.events == []
        assert _manifest_bytes(wal) == before

    def test_a_blob_committed_by_a_step_is_not_rewritten_at_run_end(self, tmp_path, io_log):
        wal = new_wal(tmp_path)
        shared = blob(b"body")
        wal.commit_step(0, step_record(0), extra_files=[shared])
        io_log.events.clear()
        wal.commit_trailing(extra_files=[shared])
        assert shared.rel_path not in io_log.writes()
        assert [r.path for r in RunTrace(wal.run_dir).trailing_extra_files] == [shared.rel_path]


@dataclass
class FsyncCount:
    files: int = 0
    directories: int = 0
    #: ``st_size`` of each file at the moment of its ``fsync``, in call order:
    #: what the kernel holds, which is all that ``fsync`` makes durable.
    file_sizes: list[int] = field(default_factory=list)

    def reset(self) -> None:
        self.files = self.directories = 0
        self.file_sizes.clear()


@pytest.fixture()
def fsync_count(monkeypatch) -> FsyncCount:
    """Count real ``os.fsync`` calls, split by what the descriptor is."""
    count = FsyncCount()
    real_fsync = os.fsync

    def counting(fd: int) -> None:
        status = os.fstat(fd)
        if stat.S_ISDIR(status.st_mode):
            count.directories += 1
        else:
            count.files += 1
            count.file_sizes.append(status.st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting)
    return count


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is skipped on Windows")
def test_a_thousand_small_files_commit_within_budget(tmp_path, fsync_count):
    """500 per-step evidence files + 500 sharded blobs, 1 KiB each, in one commit.

    Structure first, because the wall clock depends on the disk: one data
    ``fsync`` per file plus the WAL's own three (``steps/000.json``,
    ``steps/000.extra.json``, ``manifest.json``), and one directory ``fsync``
    per distinct directory and per parent of a created directory, plus the
    run directory once more for the manifest rename. Each data ``fsync``
    must cover its file's bytes: a 1 KiB file is under Python's write
    buffer, so an ``fsync`` before the flush would count the same and
    cover an empty file. Then the budget, ``SPECTRAGENT_WAL_COMMIT_BUDGET_S``
    (default 3 s; ~0.2 s on APFS).
    """
    wal = new_wal(tmp_path)
    run_dir = wal.run_dir
    files = [
        extra(f"env/steps/000/source{i % 5}/{i:04d}.bin", os.urandom(1024)) for i in range(500)
    ]
    files += [blob(os.urandom(1024)) for _ in range(500)]

    written = [item.rel_path for item in files] + ["steps/000.json", "steps/000.extra.json"]
    distinct = {(run_dir / rel).parent for rel in written}
    created = {
        directory
        for rel in written
        for directory in (run_dir / rel).parents
        if directory.is_relative_to(run_dir) and not directory.exists()
    }
    created_parents = {directory.parent for directory in created}
    fsync_count.reset()

    started = time.perf_counter()
    wal.commit_step(0, step_record(0), extra_files=files)
    elapsed = time.perf_counter() - started

    assert fsync_count.files == len(files) + 3
    # The content files, then the step record, its extra index and the
    # manifest — each fsynced holding its final bytes, none empty.
    wal_files = ("steps/000.json", "steps/000.extra.json", "manifest.json")
    assert fsync_count.file_sizes == [len(item.data) for item in files] + [
        (run_dir / rel).stat().st_size for rel in wal_files
    ]
    # Exactly one per directory: the union, plus the manifest's run directory.
    assert fsync_count.directories == len(distinct | created_parents) + 1
    assert fsync_count.directories <= len(distinct) + len(created_parents) + 1

    budget = float(os.environ.get("SPECTRAGENT_WAL_COMMIT_BUDGET_S", _DEFAULT_BUDGET_S))
    assert elapsed < budget, (
        f"committing {len(files)} files took {elapsed:.2f}s, over the {budget}s budget "
        f"(set SPECTRAGENT_WAL_COMMIT_BUDGET_S for a slow disk)"
    )

    trace = RunTrace(run_dir)
    assert len(trace.extra_files(0)) == 1000
    assert trace.read_extra(files[0].rel_path) == files[0].data
    assert trace.read_extra(files[-1].rel_path) == files[-1].data

    # The 1000 files are in the step's index, not the manifest: it is
    # rewritten whole on every commit and read eagerly by every reader, so
    # inlining them would make it grow quadratically over a run.
    manifest = (run_dir / "manifest.json").read_bytes()
    assert len(manifest) < 4096
    assert b"env/" not in manifest


class TestTheManifestReferencesExtraFilesItDoesNotList:
    """Pinned by key set: ``StepManifestEntry`` allows extra fields, so pydantic would not object."""

    def test_a_step_entry_holds_only_the_index_path(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), **_a_full_step())

        (entry,) = json.loads(_manifest_bytes(wal))["steps"]

        assert set(entry) == {
            "step",
            "step_file",
            "fragments",
            "llm_calls",
            "call_metrics",
            "extra_index",
        }
        assert entry["extra_index"] == "steps/000.extra.json"

    def test_a_trailing_entry_too(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.commit_trailing(extra_files=[extra("env/final/a.txt", b"a")])

        (entry,) = json.loads(_manifest_bytes(wal))["trailing"]

        assert set(entry) == {"segment_id", "llm_calls", "call_metrics", "extra_index"}
