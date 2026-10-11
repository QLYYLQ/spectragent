"""A file written before the commit point did not happen — for every accessor.

The manifest is the commit point (``docs/architecture_design.md`` §5.2) and
therefore the reader's only directory (§5.5). Two ways to get files on disk
that it does not list, both pinned here against *every* accessor:

- a crash between the content files and the manifest rename — simulated by
  failing the manifest write of a real ``commit_step`` / ``commit_trailing``
  after everything else of the commit is on disk;
- files dropped into the directory by hand, under ``observations/``,
  ``messages/``, ``steps/`` and an ``env/``-like extra-file tree, including
  files a committed step's index does not name.

Either way the reader's answers must be exactly what they were before — the
environment-evidence accessors included, over the M27 reference trace (where
they answer empty: none of its ``env/`` files is an archive file) and over an
archive trace (``build_archive_trace``), whose crash leaves step evidence, a
shared body and a new run-record version on disk, uncommitted.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import (
    encode_output_format,
    output_format_path,
    sha256_hex,
)
from spectragent.core.trace.reader import RunTrace
from tests._helpers.fragment_samples import SAMPLES
from tests._helpers.trace_builder import (
    ARCHIVE_CAPABILITY,
    BLOBS_ARCHIVER,
    STEPS_ARCHIVER,
    STORAGE_ARCHIVER,
    ArchiveTrace,
    ReferenceTrace,
    archive_file,
    blob,
    build_archive_trace,
    build_reference_trace,
    cancelled_call,
    completed_call,
    extra,
    fragment,
    screenshot,
    step_record,
)

STEPS = range(5)
KEYS = ("browser.dom", "browser.screenshot", "browser.browser_meta", "loop.task", "browser.extra")
#: Archivers asked about by every view — those both traces use, plus the
#: M27 reference trace's ``env/`` producers, whose files carry no archive
#: metadata and so must never show up here.
ARCHIVERS = (STEPS_ARCHIVER, BLOBS_ARCHIVER, STORAGE_ARCHIVER, "browser.network", "x.y")
CAPABILITIES = (ARCHIVE_CAPABILITY, "browser", "x")


class LateReply(BaseModel):
    """A reply model first requested by the step that never commits."""

    verdict: str


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def views(trace: RunTrace) -> dict[str, Any]:
    """Everything every accessor says about the trace, in comparable form."""
    return {
        "step_count": trace.step_count,
        "step_records": [_dump(record) for record in trace.step_records],
        "step": [_dump(trace.step(i)) for i in STEPS],
        "observation_fragment": {
            f"{i}:{key}": _dump(trace.observation_fragment(i, key)) for i in STEPS for key in KEYS
        },
        "screenshot": [trace.screenshot(i) for i in STEPS],
        "screenshots": [path.name for path in trace.screenshots],
        "llm_messages": [[_dump(call) for call in trace.llm_messages(i)] for i in STEPS],
        "trailing_calls": [_dump(call) for call in trace.trailing_calls],
        "extra_files": [[_dump(record) for record in trace.extra_files(i)] for i in STEPS],
        "trailing_extra_files": [_dump(record) for record in trace.trailing_extra_files],
        "read_extra": {
            record.path: trace.read_extra(record.path)
            for i in STEPS
            for record in [*trace.extra_files(i), *trace.trailing_extra_files]
        },
        "output_schema": {sha: trace.output_schema(sha) for sha in trace.manifest.output_formats},
        "archive_files": {
            f"{i}:{archiver}": [_dump(record) for record in trace.archive_files(i, archiver)]
            for i in STEPS
            for archiver in ARCHIVERS
        },
        "iter_archive": {
            f"{archiver}:{trailing}": [
                (step, _dump(record))
                for step, record in trace.iter_archive(archiver, include_trailing=trailing)
            ]
            for archiver in ARCHIVERS
            for trailing in (False, True)
        },
        "blob": {
            record.sha256: trace.blob(record.sha256)
            for archiver in ARCHIVERS
            for _, record in trace.iter_archive(archiver, include_trailing=True)
            if record.content_addressed
        },
        "archive_run_file": {
            capability: _dump(trace.archive_run_file(capability)) for capability in CAPABILITIES
        },
        "committed_files": [_dump(file) for file in trace.committed_files],
        "read_committed": {
            file.path: trace.read_committed(file.path) for file in trace.committed_files
        },
    }


def assert_not_visible(trace: RunTrace, *, paths: list[str], schemas: list[str]) -> None:
    """The lookups by name refuse what was never committed."""
    for path in paths:
        with pytest.raises(KeyError):
            trace.read_extra(path)
        with pytest.raises(KeyError):
            trace.read_committed(path)
    for sha in schemas:
        with pytest.raises(KeyError):
            trace.output_schema(sha)
        with pytest.raises(KeyError):
            trace.read_committed(output_format_path(sha))


@pytest.fixture()
def reference(tmp_path) -> ReferenceTrace:
    return build_reference_trace(tmp_path)


class TestACrashBeforeTheManifestRename:
    @pytest.fixture()
    def crash_on_manifest(self, monkeypatch):
        real = fsio.write_durable

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json":
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)

    def test_an_uncommitted_step_is_invisible_to_every_accessor(self, reference, crash_on_manifest):
        before = views(RunTrace(reference.run_dir))
        assert before["blob"] == {}
        assert not any(before["archive_files"].values())
        assert not any(before["iter_archive"].values())
        assert not any(before["archive_run_file"].values())
        late_blob = blob(b"evidence of a step that never happened")
        late_schema = encode_output_format(LateReply)

        with pytest.raises(OSError, match="power lost"):
            reference.wal.commit_step(
                2,
                step_record(2),
                fragments=[screenshot(2), fragment(2, SAMPLES["browser.dom"]["page"]())],
                # Call 1: call 0 of step 2 is the reference trace's trailing
                # call, committed and immutable — numbered after, as
                # TraceWriter numbers a step's calls after its trailing ones.
                llm_calls=[completed_call(2, 1, "late", output_format=LateReply)],
                extra_files=[extra("env/steps/002/x.y/name", b"late"), late_blob],
            )
        with pytest.raises(OSError, match="power lost"):
            reference.wal.commit_trailing(
                llm_calls=[cancelled_call(3, 0, "later still")],
                extra_files=[extra("env/final/late.txt", b"later")],
            )

        run_dir = reference.run_dir
        # The crash window really is populated: every file of both commits
        # except the manifest made it to disk.
        for rel in (
            "steps/002.json",
            "steps/002.extra.json",
            "steps/trailing_01.extra.json",
            "observations/002_browser.screenshot.png",
            "observations/002_browser.screenshot.json",
            "observations/002_browser.dom.json",
            "messages/002_call_01.json",
            "messages/003_call_00.json",
            f"messages/output_formats/{late_schema.ref.sha256}.json",
            "env/steps/002/x.y/name",
            late_blob.rel_path,
            "env/final/late.txt",
        ):
            assert (run_dir / rel).is_file(), rel

        trace = RunTrace(run_dir)
        assert views(trace) == before
        assert_not_visible(
            trace,
            paths=["env/steps/002/x.y/name", late_blob.rel_path, "env/final/late.txt"],
            schemas=[late_schema.ref.sha256],
        )


class TestHandPlantedOrphans:
    def test_files_nothing_committed_are_invisible_to_every_accessor(self, reference):
        before = views(RunTrace(reference.run_dir))
        run_dir = reference.run_dir
        committed_call = (run_dir / "messages/000_call_01.json").read_bytes()
        planted_schema = b'{"type":"object"}'
        planted_sha = sha256_hex(planted_schema)
        step_two_index = (
            b'{"files": [{"path": "env/steps/002/x.y/name", "size": 4, '
            b'"sha256": "' + sha256_hex(b"late").encode() + b'"}]}'
        )
        planted = {
            # a step the manifest never listed
            "observations/002_browser.screenshot.png": b"\x89PNG\r\n\x1a\nlate",
            "observations/002_browser.screenshot.json": b'{"fragment_id": "browser.screenshot"}',
            "observations/002_loop.task.json": b'{"fragment_id": "loop.task"}',
            "messages/002_call_01.json": committed_call.replace(b'"step": 0', b'"step": 2'),
            "messages/003_call_00.json": committed_call.replace(b'"step": 0', b'"step": 3'),
            "steps/002.json": step_record(2).model_dump_json().encode(),
            "steps/002.extra.json": step_two_index,
            "env/steps/002/x.y/name": b"late",
            # committed steps, files their index does not name
            "observations/000_browser.extra.json": b'{"fragment_id": "browser.extra"}',
            "observations/001_browser.dom.json": b"{}",
            "observations/001_browser.browser_meta.json": b"{}",
            "messages/000_call_02.json": committed_call.replace(b'"call": 1', b'"call": 2'),
            "messages/001_call_01.json": committed_call,
            "env/steps/000/orphan.bin": b"never indexed",
            "steps/trailing_01.extra.json": b'{"files": []}',
            f"messages/output_formats/{planted_sha}.json": planted_schema,
        }
        for rel, data in planted.items():
            path = run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        trace = RunTrace(run_dir)
        assert views(trace) == before
        assert_not_visible(
            trace,
            paths=["env/steps/002/x.y/name", "env/steps/000/orphan.bin"],
            schemas=[planted_sha],
        )
        for rel in planted:
            with pytest.raises(KeyError):
                trace.read_committed(rel)


@pytest.fixture()
def archived(tmp_path) -> ArchiveTrace:
    return build_archive_trace(tmp_path)


class TestArchiveCrashWindow:
    """Environment evidence of a commit that never reached its manifest did not happen."""

    @pytest.fixture()
    def crash_on_manifest(self, monkeypatch):
        real = fsio.write_durable

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json":
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)

    def test_a_crashed_step_leaves_its_evidence_and_its_run_record_invisible(
        self, archived, crash_on_manifest
    ):
        before = views(RunTrace(archived.run_dir))
        assert before["blob"] and before["archive_run_file"][ARCHIVE_CAPABILITY] is not None
        late_step = archive_file(STEPS_ARCHIVER, "cycle_004.json", b'{"cycle": 4}', step=3)
        late_body = archive_file(
            BLOBS_ARCHIVER, "response.bin", b"a body nobody committed", content_addressed=True
        )
        late_run = archive_file(
            ARCHIVE_CAPABILITY, "fingerprint.json", b'{"generation": 2}', scope="run", run_version=2
        )
        late_tail = archive_file(STEPS_ARCHIVER, "late.json", b"{}", trailing=1)

        with pytest.raises(OSError, match="power lost"):
            archived.wal.commit_step(
                3,
                step_record(3),
                # Call 1: call 0 of step 3 is the trailing call already committed.
                llm_calls=[completed_call(3, 1, "late")],
                extra_files=[late_step, late_body, late_run],
            )
        with pytest.raises(OSError, match="power lost"):
            archived.wal.commit_trailing(extra_files=[late_tail])

        run_dir = archived.run_dir
        for file in (late_step, late_body, late_run, late_tail):
            assert (run_dir / file.rel_path).is_file(), file.rel_path

        trace = RunTrace(run_dir)
        assert views(trace) == before
        latest = trace.archive_run_file(ARCHIVE_CAPABILITY)
        assert latest is not None and latest.run_version == 1
        assert_not_visible(
            trace,
            paths=[late_step.rel_path, late_body.rel_path, late_run.rel_path, late_tail.rel_path],
            schemas=[],
        )
        with pytest.raises(KeyError):
            trace.blob(sha256_hex(late_body.data))

    def test_hand_planted_evidence_is_invisible(self, archived):
        before = views(RunTrace(archived.run_dir))
        run_dir = archived.run_dir
        orphan_body = b"a body nobody listed"
        digest = sha256_hex(orphan_body)
        planted = {
            "env/steps/000/builder.steps/orphan.json": b"{}",
            "env/steps/003/builder.steps/cycle_004.json": b'{"cycle": 4}',
            f"env/blobs/sha256/{digest[:2]}/{digest[2:]}": orphan_body,
            "env/run.builder.2.json": b'{"generation": 2}',
            "env/run.x.json": b"{}",
            "env/trailing/01/builder.steps/late.json": b"{}",
            "env/trailing/00/builder.steps/extra.json": b"{}",
        }
        for rel, data in planted.items():
            path = run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        trace = RunTrace(run_dir)
        assert views(trace) == before
        assert_not_visible(trace, paths=list(planted), schemas=[])
        with pytest.raises(KeyError):
            trace.blob(digest)
