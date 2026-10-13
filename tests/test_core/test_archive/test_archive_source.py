"""The replay read side: ``ArchiveReader`` over ``RunTrace`` (§4.14 "回放的数据通路").

A replay sees a recorded run only through :func:`open_archive` and the
:class:`ArchiveReader` Protocol. These tests pin the three things the
capability relies on without knowing the WAL: ``RunTrace`` *is* an
``ArchiveReader`` (structurally — it does not import the Protocol); only
committed evidence is visible, so an orphan ``env/`` file is never served;
and opening and reading write nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from spectragent.core.archive.source import (
    ArchiveEntry,
    ArchiveOpenError,
    ArchiveReader,
    open_archive,
)
from spectragent.core.trace.digest import tree_digest
from spectragent.core.trace.encoding import archive_step_path, sha256_hex
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from tests._helpers.trace_builder import (
    ARCHIVE_CAPABILITY,
    BLOBS_ARCHIVER,
    STEPS_ARCHIVER,
    ArchiveTrace,
    build_archive_trace,
)


@pytest.fixture()
def archived(tmp_path: Path) -> ArchiveTrace:
    return build_archive_trace(tmp_path)


class _Line(BaseModel):
    cycle: int


class TestRunTraceIsAnArchiveReader:
    def test_isinstance(self, archived: ArchiveTrace) -> None:
        assert isinstance(RunTrace(archived.run_dir), ArchiveReader)

    def test_statically_too(self, archived: ArchiveTrace) -> None:
        """Pyright checks these assignments: a drift between the two breaks the type check."""
        reader: ArchiveReader = RunTrace(archived.run_dir)
        entry: ArchiveEntry | None = reader.archive_run_file(ARCHIVE_CAPABILITY)
        assert entry is not None and entry.scope == "run"

    def test_open_archive_hands_back_the_committed_view(self, archived: ArchiveTrace) -> None:
        reader = open_archive(archived.run_dir)
        assert reader.run_id == "run-archive"
        steps = [(step, entry.name) for step, entry in reader.iter_archive(STEPS_ARCHIVER)]
        assert steps == [(0, "cycle_001.json"), (1, "cycle_002.json"), (2, "cycle_003.json")]
        files = reader.archive_files(1, STEPS_ARCHIVER)
        assert [entry.name for entry in files] == ["cycle_002.json"]
        assert reader.read_extra(files[0].path) == b'{"cycle": 2}'
        assert reader.archive_lines(files[0].path, _Line) == [_Line(cycle=2)]
        assert reader.blob(sha256_hex(archived.shared_blob.data)) == archived.shared_blob.data

    def test_a_string_path_opens_too(self, archived: ArchiveTrace) -> None:
        assert open_archive(str(archived.run_dir)).run_id == "run-archive"


class TestOnlyCommittedEvidenceIsVisible:
    def test_an_orphan_env_file_is_invisible(self, archived: ArchiveTrace) -> None:
        """A file the manifest never listed — a crash-window leftover — is never served."""
        orphan_rel = archive_step_path(1, BLOBS_ARCHIVER, "orphan.bin")
        orphan = archived.run_dir / orphan_rel
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"never committed")
        # And a whole uncommitted step directory of a known archiver.
        stray = archived.run_dir / archive_step_path(7, STEPS_ARCHIVER, "cycle_008.json")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b'{"cycle": 8}')

        reader = open_archive(archived.run_dir)
        everything = [
            entry.path
            for archiver in (STEPS_ARCHIVER, BLOBS_ARCHIVER)
            for _, entry in reader.iter_archive(archiver, include_trailing=True)
        ]
        assert orphan_rel not in everything
        assert all("cycle_008" not in path for path in everything)
        assert reader.archive_files(7, STEPS_ARCHIVER) == []
        with pytest.raises(KeyError):
            reader.read_extra(orphan_rel)
        with pytest.raises(KeyError):
            reader.archive_lines(orphan_rel, _Line)

    def test_a_listed_file_whose_bytes_changed_is_refused(self, archived: ArchiveTrace) -> None:
        reader = open_archive(archived.run_dir)
        entry = reader.archive_files(0, STEPS_ARCHIVER)[0]
        (archived.run_dir / entry.path).write_bytes(b'{"cycle": 99}')
        with pytest.raises(TraceSchemaError, match="does not match its index entry"):
            reader.read_extra(entry.path)


class TestReplayReadsWithoutWriting:
    def test_opening_and_reading_writes_nothing(self, archived: ArchiveTrace) -> None:
        before = tree_digest(archived.run_dir, metadata=True)
        reader = open_archive(archived.run_dir)
        for archiver in (STEPS_ARCHIVER, BLOBS_ARCHIVER):
            for _, entry in reader.iter_archive(archiver, include_trailing=True):
                if entry.content_addressed:
                    reader.blob(entry.sha256)
                else:
                    reader.read_extra(entry.path)
        run_record = reader.archive_run_file(ARCHIVE_CAPABILITY)
        assert run_record is not None
        json.loads(reader.read_extra(run_record.path))
        reader.archive_lines(reader.archive_files(2, STEPS_ARCHIVER)[0].path, _Line)
        assert tree_digest(archived.run_dir, metadata=True) == before


class TestOpeningSaysWhatIsWrong:
    def test_a_missing_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-run"
        with pytest.raises(ArchiveOpenError, match="not a directory") as caught:
            open_archive(missing)
        assert caught.value.path == missing
        assert str(missing) in str(caught.value)

    def test_a_file_is_not_a_run_directory(self, tmp_path: Path) -> None:
        file = tmp_path / "run.txt"
        file.write_text("x")
        with pytest.raises(ArchiveOpenError, match="not a directory"):
            open_archive(file)

    def test_a_directory_without_a_manifest(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty-run"
        empty.mkdir()
        with pytest.raises(ArchiveOpenError, match="no manifest.json") as caught:
            open_archive(empty)
        assert caught.value.path == empty

    def test_a_corrupt_manifest_is_the_trace_layer_s_own_error(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken-run"
        broken.mkdir()
        (broken / "manifest.json").write_text("{not json")
        with pytest.raises(TraceSchemaError, match="manifest.json"):
            open_archive(broken)
