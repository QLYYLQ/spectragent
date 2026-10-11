"""The three kinds of "not readable", kept apart (``docs/architecture_design.md`` §5.5).

- **Not there** — the manifest does not list it (or ``result.json`` is
  absent): ``None`` / ``[]``, and ``KeyError`` for the lookups by name.
- **There but unreadable** — corrupt JSON, bytes that are not UTF-8, an empty
  file, a schema mismatch, a committed file gone from disk, bytes that
  disagree with their index, a key no class claims, a format this build
  cannot serve: :class:`TraceSchemaError`, naming the file.
- **The evaluator failed** — not the trace's business, not tested here.

Every reader input is crossed with every corruption that applies to it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.core.trace.encoding import EncodedFragment, EncodedLLMCall, TraceFile, sha256_hex
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import (
    ARCHIVE_CAPABILITY,
    STEPS_ARCHIVER,
    ArchiveTrace,
    ReferenceTrace,
    build_archive_trace,
    build_reference_trace,
    completed_call,
    new_wal,
    screenshot,
    step_record,
)


@pytest.fixture()
def reference(tmp_path) -> ReferenceTrace:
    return build_reference_trace(tmp_path)


Read = Callable[[Path], Any]


def _trace(run_dir: Path) -> RunTrace:
    return RunTrace(run_dir)


#: input → (file relative to the run dir, the read that must fail, a
#: payload that is valid JSON but the wrong schema)
JSON_INPUTS: dict[str, tuple[str, Read, bytes]] = {
    "manifest": ("manifest.json", _trace, b'{"run_id": 5}'),
    "config": ("config.json", lambda d: _trace(d).config, b'{"agent_id": [1, 2]}'),
    "result": (
        "result.json",
        lambda d: _trace(d).result,
        b'{"schema_version": "2.1", "task_id": "t"}',
    ),
    "step via step()": ("steps/000.json", lambda d: _trace(d).step(0), b'{"step": "zero"}'),
    "step via step_records": (
        "steps/001.json",
        lambda d: _trace(d).step_records,
        b'{"step": "one"}',
    ),
    "fragment": (
        "observations/000_browser.dom.json",
        lambda d: _trace(d).observation_fragment(0, "browser.dom"),
        b'{"fragment_id": "browser.dom", "selector_map": [1, 2]}',
    ),
    "failed fragment": (
        "observations/000_browser.browser_meta.json",
        lambda d: _trace(d).observation_fragment(0, "browser.browser_meta"),
        b'{"fragment_id": "browser.browser_meta"}',
    ),
    "call": (
        "messages/000_call_00.json",
        lambda d: _trace(d).llm_messages(0),
        b'{"step": 0, "call": 0}',
    ),
    "trailing call": (
        "messages/002_call_00.json",
        lambda d: _trace(d).trailing_calls,
        b'{"step": 2, "call": 0, "status": "lost"}',
    ),
    "extra index": (
        "steps/000.extra.json",
        lambda d: _trace(d).extra_files(0),
        b'{"files": "none"}',
    ),
    "extra index via read_extra": (
        "steps/001.extra.json",
        lambda d: _trace(d).read_extra("env/steps/001/x.y/name"),
        b'{"files": [{"path": "env/steps/001/x.y/name"}]}',
    ),
    "trailing extra index": (
        "steps/trailing_00.extra.json",
        lambda d: _trace(d).trailing_extra_files,
        b'{"files": [{"size": "big"}]}',
    ),
    "extra index via archive_files": (
        "steps/000.extra.json",
        lambda d: _trace(d).archive_files(0, "browser.network"),
        b'{"files": "none"}',
    ),
    "extra index via iter_archive": (
        "steps/001.extra.json",
        lambda d: list(_trace(d).iter_archive("x.y")),
        b'{"files": [{"path": "env/steps/001/x.y/name"}]}',
    ),
    "trailing extra index via iter_archive": (
        "steps/trailing_00.extra.json",
        lambda d: list(_trace(d).iter_archive("x.y", include_trailing=True)),
        b'{"files": [{"size": "big"}]}',
    ),
    "extra index via archive_run_file": (
        "steps/trailing_00.extra.json",
        lambda d: _trace(d).archive_run_file("browser"),
        b'{"files": "none"}',
    ),
    "extra index via blob": (
        "steps/000.extra.json",
        lambda d: _trace(d).blob("0" * 64),
        b'{"files": [{"path": 3}]}',
    ),
}

CORRUPTIONS: dict[str, Callable[[Path, bytes], object]] = {
    "invalid JSON": lambda path, _: path.write_bytes(b'{"step": 0, "llm_output": '),
    "invalid UTF-8": lambda path, _: path.write_bytes(b'{"step": "\xff\xfe"}'),
    "empty": lambda path, _: path.write_bytes(b""),
    "schema mismatch": lambda path, payload: path.write_bytes(payload),
    "listed but missing": lambda path, _: path.unlink(),
}

MATRIX = [(name, corruption) for name in JSON_INPUTS for corruption in CORRUPTIONS]


@pytest.mark.parametrize(("name", "corruption"), MATRIX, ids=[f"{n}|{c}" for n, c in MATRIX])
def test_every_json_input_and_every_corruption(reference, name, corruption):
    rel, read, mismatched = JSON_INPUTS[name]
    path = reference.run_dir / rel
    CORRUPTIONS[corruption](path, mismatched)

    if name == "result" and corruption == "listed but missing":
        # result.json is not listed by anything: absent means the run died
        # before writing it, the one missing file that is not damage.
        assert _trace(reference.run_dir).result is None
        return

    with pytest.raises(TraceSchemaError) as exc_info:
        read(reference.run_dir)
    assert exc_info.value.path == path
    assert str(path) in str(exc_info.value)


ARTIFACT_READS: dict[str, Read] = {
    "observation_fragment": lambda d: _trace(d).observation_fragment(0, "browser.screenshot"),
    "screenshot": lambda d: _trace(d).screenshot(0),
    "screenshots": lambda d: _trace(d).screenshots,
}


@pytest.mark.parametrize("corruption", ["empty", "listed but missing"])
@pytest.mark.parametrize("accessor", list(ARTIFACT_READS))
def test_a_damaged_artifact(reference, accessor, corruption):
    path = reference.run_dir / "observations/000_browser.screenshot.png"
    CORRUPTIONS[corruption](path, b"")

    with pytest.raises(TraceSchemaError) as exc_info:
        ARTIFACT_READS[accessor](reference.run_dir)
    assert exc_info.value.path == path


class TestBytesThatDisagreeWithTheirIndex:
    @pytest.mark.parametrize(
        "damage",
        [
            lambda data: bytes(reversed(data)),  # same size, other bytes
            lambda data: data[:-1],  # torn
            lambda data: b"",
        ],
        ids=["tampered", "torn", "empty"],
    )
    def test_an_extra_file(self, reference, damage):
        rel = "env/steps/000/browser.network/response.json"
        path = reference.run_dir / rel
        path.write_bytes(damage(path.read_bytes()))

        with pytest.raises(TraceSchemaError, match="does not match its index entry") as exc_info:
            _trace(reference.run_dir).read_extra(rel)
        assert exc_info.value.path == path

    def test_a_missing_extra_file(self, reference):
        path = reference.run_dir / reference.shared_blob.rel_path
        path.unlink()
        with pytest.raises(TraceSchemaError, match="missing on disk"):
            _trace(reference.run_dir).read_extra(reference.shared_blob.rel_path)

    def test_two_indexes_that_disagree_about_one_blob(self, reference):
        """A deduplicated blob is one file: every index listing it must agree on its bytes."""
        shared = reference.shared_blob.rel_path
        index = reference.run_dir / "steps/001.extra.json"
        payload = json.loads(index.read_text())
        (entry,) = [record for record in payload["files"] if record["path"] == shared]
        entry["size"] += 1
        index.write_text(json.dumps(payload))

        with pytest.raises(TraceSchemaError, match="an earlier index recorded") as exc_info:
            _trace(reference.run_dir).read_extra(shared)
        assert exc_info.value.path == index

    @pytest.mark.parametrize(
        "damage",
        [
            lambda path: path.write_bytes(b'{"type": "object"}'),
            lambda path: path.write_bytes(b"{"),
            lambda path: path.write_bytes(b""),
            lambda path: path.unlink(),
        ],
        ids=["replaced", "invalid JSON", "empty", "missing"],
    )
    def test_an_output_schema(self, reference, damage):
        path = reference.run_dir / f"messages/output_formats/{reference.schema_sha}.json"
        damage(path)
        with pytest.raises(TraceSchemaError) as exc_info:
            _trace(reference.run_dir).output_schema(reference.schema_sha)
        assert exc_info.value.path == path

    @pytest.mark.parametrize(
        "data",
        [b"[1, 2]", b'{"type": ', b'{"title": "\xff"}', b""],
        ids=["not an object", "invalid JSON", "invalid UTF-8", "empty"],
    )
    def test_an_output_schema_at_its_own_address_that_is_not_a_schema(self, tmp_path, data):
        """The digest matches, so only the parse can refuse it — a producer's bug, not damage."""
        wal = new_wal(tmp_path)
        digest = sha256_hex(data)
        rel = f"messages/output_formats/{digest}.json"
        call = completed_call(0, 0, "go")
        wal.commit_step(
            0,
            step_record(0),
            llm_calls=[EncodedLLMCall(call.file, TraceFile(rel, data, content_addressed=True))],
        )

        with pytest.raises(TraceSchemaError, match="JSON object") as exc_info:
            RunTrace(wal.run_dir).output_schema(digest)
        assert exc_info.value.path == wal.run_dir / rel


ARCHIVE_READS: dict[str, Read] = {
    "archive_files": lambda d: _trace(d).archive_files(0, STEPS_ARCHIVER),
    "iter_archive": lambda d: list(_trace(d).iter_archive(STEPS_ARCHIVER)),
    "blob": lambda d: _trace(d).blob("0" * 64),
    "archive_run_file": lambda d: _trace(d).archive_run_file(ARCHIVE_CAPABILITY),
    "committed_files": lambda d: _trace(d).committed_files,
}


@pytest.fixture()
def archived(tmp_path) -> ArchiveTrace:
    return build_archive_trace(tmp_path)


class TestArchiveMetadataThatDoesNotParse:
    """``meta["archive"]`` is what makes an extra file an archive file — it must be one."""

    @pytest.mark.parametrize(
        "damage",
        [
            lambda meta: meta.update(archiver_id=5),
            lambda meta: meta.pop("scope"),
            lambda meta: meta.update(scope="session"),
            lambda meta: meta.update(run_version=0),  # a step file claiming a version
            lambda meta: meta.clear(),
        ],
        ids=["wrong type", "missing field", "unknown scope", "version on a step file", "empty"],
    )
    @pytest.mark.parametrize("accessor", list(ARCHIVE_READS))
    def test_names_the_index(self, archived, accessor, damage):
        index = archived.run_dir / "steps/000.extra.json"
        payload = json.loads(index.read_text())
        damage(payload["files"][0]["meta"]["archive"])
        index.write_text(json.dumps(payload))

        with pytest.raises(TraceSchemaError, match="ArchiveMeta") as exc_info:
            ARCHIVE_READS[accessor](archived.run_dir)
        assert exc_info.value.path == index

    def test_a_run_record_without_its_version(self, archived):
        index = archived.run_dir / "steps/002.extra.json"
        payload = json.loads(index.read_text())
        (entry,) = [f for f in payload["files"] if f["meta"]["archive"]["scope"] == "run"]
        entry["meta"]["archive"]["run_version"] = None
        index.write_text(json.dumps(payload))

        with pytest.raises(TraceSchemaError, match="carries its version") as exc_info:
            _trace(archived.run_dir).archive_run_file(ARCHIVE_CAPABILITY)
        assert exc_info.value.path == index

    def test_other_producers_metadata_is_not_read(self, archived):
        """A record without the ``archive`` key is skipped, whatever else its meta holds."""
        index = archived.run_dir / "steps/000.extra.json"
        payload = json.loads(index.read_text())
        (plain,) = [f for f in payload["files"] if "archive" not in f["meta"]]
        plain["meta"] = {"kind": ["anything", {"at": "all"}]}
        index.write_text(json.dumps(payload))

        assert [r.name for r in _trace(archived.run_dir).archive_files(0, STEPS_ARCHIVER)] == [
            "cycle_001.json"
        ]


class TestRecordsFiledInTheWrongPlace:
    def test_a_step_file_recording_another_step(self, reference):
        path = reference.run_dir / "steps/000.json"
        path.write_bytes(step_record(1).model_dump_json().encode())
        with pytest.raises(TraceSchemaError, match="records step 1"):
            _trace(reference.run_dir).step(0)

    def test_a_call_recording_another_step(self, reference):
        path = reference.run_dir / "messages/001_call_00.json"
        record = json.loads(path.read_bytes())
        record["step"] = 7
        path.write_text(json.dumps(record))
        with pytest.raises(TraceSchemaError, match="call of step 7"):
            _trace(reference.run_dir).llm_messages(1)

    def test_a_fragment_payload_under_another_key(self, tmp_path):
        wal = new_wal(tmp_path)
        meta = BrowserMetaObs(url="https://a").model_dump_json().encode()
        stray = EncodedFragment(
            "browser.dom", TraceFile("observations/000_browser.dom.json", meta), None, False
        )
        wal.commit_step(0, step_record(0), fragments=[stray])

        with pytest.raises(TraceSchemaError, match="filed under 'browser.dom'"):
            RunTrace(wal.run_dir).observation_fragment(0, "browser.dom")

    def test_a_failure_filed_under_another_key(self, reference):
        path = reference.run_dir / "observations/000_browser.browser_meta.json"
        record = json.loads(path.read_bytes())
        record["fragment_id"] = "browser.dom"
        path.write_text(json.dumps(record))
        with pytest.raises(TraceSchemaError, match="failure of 'browser.dom'"):
            _trace(reference.run_dir).observation_fragment(0, "browser.browser_meta")

    def test_two_screenshot_fragments_in_one_step(self, tmp_path):
        wal = new_wal(tmp_path)
        shot = screenshot(0)
        other = EncodedFragment(
            "desktop.screenshot",
            TraceFile("observations/000_desktop.screenshot.json", shot.file.data),
            None,
            False,
        )
        wal.commit_step(0, step_record(0), fragments=[shot, other])
        with pytest.raises(TraceSchemaError, match="2 screenshot fragments"):
            RunTrace(wal.run_dir).screenshot(0)


class TestUnknownFragmentKeys:
    def test_a_key_no_class_claims_names_the_key(self, tmp_path):
        wal = new_wal(tmp_path)
        payload = b'{"fragment_id": "nowhere.thing"}'
        wal.commit_step(
            0,
            step_record(0),
            fragments=[
                EncodedFragment(
                    "nowhere.thing",
                    TraceFile("observations/000_nowhere.thing.json", payload),
                    None,
                    False,
                )
            ],
        )
        with pytest.raises(TraceSchemaError, match="'nowhere.thing'") as exc_info:
            RunTrace(wal.run_dir).observation_fragment(0, "nowhere.thing")
        assert "must be importable" in str(exc_info.value)

    def test_a_key_the_trace_does_not_record_is_simply_absent(self, reference):
        assert _trace(reference.run_dir).observation_fragment(0, "nowhere.thing") is None


def _format_1_trace(base: Path) -> Path:
    """A trace as written before the manifest carried ``format_version``."""
    wal = new_wal(base, "run-v1")
    wal.commit_step(0, step_record(0))
    from tests._helpers.trace_builder import agent_result

    wal.write_result(agent_result())
    manifest = json.loads((wal.run_dir / "manifest.json").read_text())
    del manifest["format_version"]
    for key in ("trailing", "output_formats"):
        del manifest[key]
    manifest["unfinished_message_files"] = []
    entry = manifest["steps"][0]
    for key in ("fragments", "llm_calls", "extra_index"):
        del entry[key]
    entry.update(
        observation_files=["observations/000_browser.screenshot.png"],
        message_input_file="messages/000_input.json",
        message_output_file="messages/000_output.json",
        message_files=["messages/000_input.json", "messages/000_output.json"],
    )
    (wal.run_dir / "manifest.json").write_text(json.dumps(manifest))
    (wal.run_dir / "observations/000_browser.screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return wal.run_dir


V2_ACCESSORS: dict[str, Read] = {
    "observation_fragment": lambda d: _trace(d).observation_fragment(0, "browser.screenshot"),
    "screenshot": lambda d: _trace(d).screenshot(0),
    "screenshots": lambda d: _trace(d).screenshots,
    "llm_messages": lambda d: _trace(d).llm_messages(0),
    "trailing_calls": lambda d: _trace(d).trailing_calls,
    "extra_files": lambda d: _trace(d).extra_files(0),
    "trailing_extra_files": lambda d: _trace(d).trailing_extra_files,
    "read_extra": lambda d: _trace(d).read_extra("env/x"),
    "output_schema": lambda d: _trace(d).output_schema("0" * 64),
    "archive_files": lambda d: _trace(d).archive_files(0, "x.y"),
    # Not consumed: the format check happens at the call, not at the first ``next()``.
    "iter_archive": lambda d: _trace(d).iter_archive("x.y"),
    "blob": lambda d: _trace(d).blob("0" * 64),
    "archive_run_file": lambda d: _trace(d).archive_run_file("x"),
    "archive_lines": lambda d: _trace(d).archive_lines("env/x", StepRecord),
    "committed_files": lambda d: _trace(d).committed_files,
    "read_committed": lambda d: _trace(d).read_committed("manifest.json"),
}


class TestFormatVersions:
    def test_a_format_1_trace_keeps_its_unchanged_accessors(self, tmp_path):
        trace = _trace(_format_1_trace(tmp_path))

        assert trace.manifest.format_version == 1
        assert trace.step_count == 1
        assert trace.config.agent_id == "test_agent"
        assert trace.result is not None
        assert trace.step(0) is not None
        assert [record.step for record in trace.step_records] == [0]

    @pytest.mark.parametrize("accessor", list(V2_ACCESSORS))
    def test_a_format_1_trace_refuses_the_changed_ones(self, tmp_path, accessor):
        run_dir = _format_1_trace(tmp_path)
        with pytest.raises(TraceSchemaError, match="format version 1") as exc_info:
            V2_ACCESSORS[accessor](run_dir)
        assert accessor in str(exc_info.value)
        assert exc_info.value.path == run_dir / "manifest.json"

    def test_a_newer_format_is_refused_at_construction(self, reference):
        path = reference.run_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["format_version"] = 3
        path.write_text(json.dumps(manifest))

        with pytest.raises(TraceSchemaError, match="format version 3"):
            _trace(reference.run_dir)


class TestNotThereIsNotAnError:
    def test_absences(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), llm_calls=[completed_call(0, 0, "go")])
        trace = RunTrace(wal.run_dir)

        assert trace.result is None
        assert trace.step(5) is None
        assert trace.observation_fragment(0, "browser.dom") is None
        assert trace.screenshot(0) is None
        assert trace.screenshots == []
        assert trace.llm_messages(5) == []
        assert trace.trailing_calls == []
        assert trace.extra_files(0) == []
        assert trace.trailing_extra_files == []
        with pytest.raises(KeyError):
            trace.read_extra("env/x")
        with pytest.raises(KeyError):
            trace.output_schema("0" * 64)
        assert trace.archive_files(0, "x.y") == []
        assert list(trace.iter_archive("x.y", include_trailing=True)) == []
        assert trace.archive_run_file("x") is None
        with pytest.raises(KeyError):
            trace.blob("0" * 64)


class TestAPathTheTraceListsStaysInsideIt:
    """The manifest and the extra indexes are data, and traces are shared.

    A crafted or damaged one could list ``/home/me/.ssh/id_rsa`` as step 0's
    screenshot: ``RunTrace.screenshots`` handed the path to an image judge,
    which base64-encoded the file into its request, and a JSON file that
    failed to parse had its first bytes echoed into the error. Every listed
    path is now checked — the WAL's own path rule, then containment once
    symlinks are resolved — before anything opens it, and the refusal
    names the file that listed it.
    """

    SECRET = b"-----BEGIN OPENSSH PRIVATE KEY----- outside the trace"

    #: A path the manifest lists → (where it sits in the manifest, a read that opens it).
    MANIFEST_LISTED: dict[str, tuple[tuple[Any, ...], Read]] = {
        "step file via step()": (("steps", 0, "step_file"), lambda d: _trace(d).step(0)),
        "step file via step_records": (
            ("steps", 0, "step_file"),
            lambda d: _trace(d).step_records,
        ),
        "fragment": (
            ("steps", 0, "fragments", "browser.dom", "file"),
            lambda d: _trace(d).observation_fragment(0, "browser.dom"),
        ),
        "artifact via observation_fragment": (
            ("steps", 0, "fragments", "browser.screenshot", "artifact"),
            lambda d: _trace(d).observation_fragment(0, "browser.screenshot"),
        ),
        "artifact via screenshot": (
            ("steps", 0, "fragments", "browser.screenshot", "artifact"),
            lambda d: _trace(d).screenshot(0),
        ),
        "artifact via screenshots": (
            ("steps", 0, "fragments", "browser.screenshot", "artifact"),
            lambda d: _trace(d).screenshots,
        ),
        "call": (("steps", 0, "llm_calls", 0), lambda d: _trace(d).llm_messages(0)),
        "trailing call": (("trailing", 0, "llm_calls", 0), lambda d: _trace(d).trailing_calls),
        "extra index": (("steps", 0, "extra_index"), lambda d: _trace(d).extra_files(0)),
        "extra index via read_extra": (
            ("steps", 0, "extra_index"),
            lambda d: _trace(d).read_extra("env/steps/000/browser.network/response.json"),
        ),
        "trailing extra index": (
            ("trailing", 0, "extra_index"),
            lambda d: _trace(d).trailing_extra_files,
        ),
        "step file via committed_files": (
            ("steps", 0, "step_file"),
            lambda d: _trace(d).committed_files,
        ),
        "fragment via committed_files": (
            ("steps", 0, "fragments", "browser.dom", "file"),
            lambda d: _trace(d).committed_files,
        ),
        "artifact via committed_files": (
            ("steps", 0, "fragments", "browser.screenshot", "artifact"),
            lambda d: _trace(d).committed_files,
        ),
        "call via committed_files": (
            ("steps", 0, "llm_calls", 0),
            lambda d: _trace(d).committed_files,
        ),
        "trailing call via committed_files": (
            ("trailing", 0, "llm_calls", 0),
            lambda d: _trace(d).committed_files,
        ),
        "extra index via committed_files": (
            ("steps", 0, "extra_index"),
            lambda d: _trace(d).committed_files,
        ),
        "trailing extra index via committed_files": (
            ("trailing", 0, "extra_index"),
            lambda d: _trace(d).committed_files,
        ),
        "artifact via read_committed": (
            ("steps", 0, "fragments", "browser.screenshot", "artifact"),
            lambda d: _trace(d).read_committed("steps/001.json"),
        ),
    }

    @pytest.fixture()
    def secret(self, tmp_path) -> Path:
        path = tmp_path / "outside" / "id_rsa"
        path.parent.mkdir()
        path.write_bytes(self.SECRET)
        return path

    def _refused(self, read: Read, run_dir: Path, *, listed_in: Path) -> None:
        with pytest.raises(TraceSchemaError) as exc_info:
            read(run_dir)
        assert exc_info.value.path == listed_in
        assert self.SECRET.decode() not in str(exc_info.value)

    @staticmethod
    def _edit_manifest(run_dir: Path, where: tuple[Any, ...], value: str | None = None) -> str:
        """The manifest entry at *where*; replaced by *value* when one is given."""
        path = run_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        holder = manifest
        for key in where[:-1]:
            holder = holder[key]
        listed = holder[where[-1]]
        if value is not None:
            holder[where[-1]] = value
            path.write_text(json.dumps(manifest))
        return listed

    @pytest.mark.parametrize("escape", ["absolute", "dot-dot", "symlink"])
    @pytest.mark.parametrize("accessor", list(MANIFEST_LISTED))
    def test_a_manifest_path(self, reference, secret, accessor, escape):
        where, read = self.MANIFEST_LISTED[accessor]
        run_dir = reference.run_dir
        if escape == "symlink":
            listed = run_dir / self._edit_manifest(run_dir, where)
            listed.unlink()
            listed.symlink_to(secret)
        else:
            outside = str(secret) if escape == "absolute" else "../outside/id_rsa"
            self._edit_manifest(run_dir, where, outside)

        self._refused(read, run_dir, listed_in=run_dir / "manifest.json")

    @pytest.mark.parametrize("escape", ["absolute", "dot-dot", "symlink"])
    @pytest.mark.parametrize("accessor", ["output_schema", "committed_files"])
    def test_an_output_schema(self, reference, secret, escape, accessor):
        run_dir = reference.run_dir
        sha = reference.schema_sha
        if escape == "symlink":
            schema = run_dir / f"messages/output_formats/{sha}.json"
            schema.unlink()
            schema.symlink_to(secret)
        else:
            sha = "/abs" if escape == "absolute" else "../../../outside/id_rsa"
            self._edit_manifest(run_dir, ("output_formats", 0), sha)

        read: Read = (
            (lambda d: _trace(d).output_schema(sha))
            if accessor == "output_schema"
            else (lambda d: _trace(d).committed_files)
        )
        self._refused(read, run_dir, listed_in=run_dir / "manifest.json")

    @pytest.mark.parametrize("escape", ["absolute", "dot-dot"])
    @pytest.mark.parametrize("accessor", ["extra_files", "read_extra", "committed_files"])
    def test_a_path_an_extra_index_lists(self, reference, secret, accessor, escape):
        index = reference.run_dir / "steps/000.extra.json"
        payload = json.loads(index.read_text())
        outside = str(secret) if escape == "absolute" else "../outside/id_rsa"
        payload["files"][0]["path"] = outside
        index.write_text(json.dumps(payload))

        reads: dict[str, Read] = {
            "extra_files": lambda d: _trace(d).extra_files(0),
            "read_extra": lambda d: _trace(d).read_extra(outside),
            "committed_files": lambda d: _trace(d).committed_files,
        }
        self._refused(reads[accessor], reference.run_dir, listed_in=index)

    @pytest.mark.parametrize("escape", ["absolute", "dot-dot"])
    @pytest.mark.parametrize("accessor", list(ARCHIVE_READS))
    def test_a_path_an_archive_index_lists(self, archived, secret, accessor, escape):
        index = archived.run_dir / "steps/000.extra.json"
        payload = json.loads(index.read_text())
        payload["files"][0]["path"] = str(secret) if escape == "absolute" else "../outside/id_rsa"
        index.write_text(json.dumps(payload))

        self._refused(ARCHIVE_READS[accessor], archived.run_dir, listed_in=index)

    def test_an_archive_blob_that_is_a_symlink_out(self, archived, secret):
        shared = archived.shared_blob
        (archived.run_dir / shared.rel_path).unlink()
        (archived.run_dir / shared.rel_path).symlink_to(secret)

        self._refused(
            lambda d: _trace(d).blob(sha256_hex(shared.data)),
            archived.run_dir,
            listed_in=archived.run_dir / "steps/000.extra.json",
        )

    def test_a_run_record_that_is_a_symlink_out(self, archived, secret):
        """The record is handed back; its bytes go through ``read_extra``'s containment."""
        latest = _trace(archived.run_dir).archive_run_file(ARCHIVE_CAPABILITY)
        assert latest is not None
        (archived.run_dir / latest.path).unlink()
        (archived.run_dir / latest.path).symlink_to(secret)

        self._refused(
            lambda d: _trace(d).read_extra(latest.path),
            archived.run_dir,
            listed_in=archived.run_dir / "steps/002.extra.json",
        )

    @pytest.mark.parametrize("accessor", ["read_extra", "committed_files", "read_committed"])
    def test_an_extra_file_that_is_a_symlink_out(self, reference, secret, accessor):
        rel = "env/steps/000/browser.network/response.json"
        (reference.run_dir / rel).unlink()
        (reference.run_dir / rel).symlink_to(secret)

        reads: dict[str, Read] = {
            "read_extra": lambda d: _trace(d).read_extra(rel),
            "committed_files": lambda d: _trace(d).committed_files,
            "read_committed": lambda d: _trace(d).read_committed(rel),
        }
        self._refused(
            reads[accessor],
            reference.run_dir,
            listed_in=reference.run_dir / "steps/000.extra.json",
        )

    @pytest.mark.parametrize(
        ("name", "read"),
        [
            ("manifest.json", _trace),
            ("config.json", lambda d: _trace(d).config),
            ("result.json", lambda d: _trace(d).result),
            ("config.json", lambda d: _trace(d).committed_files),
            ("result.json", lambda d: _trace(d).committed_files),
        ],
        ids=["manifest", "config", "result", "config_listed", "result_listed"],
    )
    def test_a_file_the_run_names_itself(self, reference, tmp_path, name, read):
        """Refused though the file it leads to would read: where it is, not what it holds."""
        outside = tmp_path / "outside-copy" / name
        outside.parent.mkdir()
        (reference.run_dir / name).rename(outside)
        (reference.run_dir / name).symlink_to(outside)

        self._refused(read, reference.run_dir, listed_in=reference.run_dir / name)

    def test_a_directory_that_is_a_symlink_out(self, reference, tmp_path):
        """Every file under it leaves the run, though no listed path says so."""
        observations = reference.run_dir / "observations"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        observations.rename(elsewhere / "observations")
        observations.symlink_to(elsewhere / "observations")

        for read in (
            lambda d: _trace(d).observation_fragment(0, "browser.dom"),
            lambda d: _trace(d).screenshots,
        ):
            with pytest.raises(TraceSchemaError) as exc_info:
                read(reference.run_dir)
            assert exc_info.value.path == reference.run_dir / "manifest.json"

    def test_a_symlink_that_stays_inside_is_fine(self, reference):
        """Containment, not a ban on links: a link within the run reads normally."""
        rel = "env/steps/000/browser.network/response.json"
        target = reference.run_dir / rel
        moved = reference.run_dir / "env" / "moved.json"
        target.rename(moved)
        target.symlink_to(moved)

        assert _trace(reference.run_dir).read_extra(rel) == b'{"url": "https://a"}'


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_os_error_that_is_not_about_the_trace_propagates(reference):
    """Permissions and I/O failures say nothing about the trace: not a schema error."""
    path = reference.run_dir / "steps/000.json"
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _trace(reference.run_dir).step(0)
    finally:
        path.chmod(0o644)
