"""Tests for RunTrace — the typed, manifest-driven reader.

Every trace here is produced by the real encoder and the real
:class:`WALWriter` from real models (``tests/_helpers/trace_builder.py``),
so the assertions below are a round trip: what the writer was handed is
what the reader gives back, as the same type.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.trace.encoding import TraceFile, sha256_hex
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    CommittedFile,
    TraceConfig,
    TraceStatus,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import TaskObs
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from tests._helpers.fragment_samples import FRAME_BUTTON_HANDLE, JPEG_BYTES, PNG_BYTES
from tests._helpers.trace_builder import (
    ARCHIVE_CAPABILITY,
    BLOBS_ARCHIVER,
    STEPS_ARCHIVER,
    STORAGE_ARCHIVER,
    ArchiveTrace,
    ReferenceTrace,
    Reply,
    agent_result,
    archive_file,
    as_chat,
    build_archive_trace,
    build_reference_trace,
    new_wal,
    required,
    step_record,
)


@pytest.fixture()
def reference(tmp_path) -> ReferenceTrace:
    return build_reference_trace(tmp_path)


@pytest.fixture()
def trace(reference) -> RunTrace:
    return RunTrace(reference.run_dir)


class TestRunTrace:
    def test_manifest_loaded_eagerly(self, trace):
        assert trace.manifest.run_id == "run-test"
        assert trace.manifest.last_step_completed == 1
        # The agent timed out (result.json); the trace recorded all of it.
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.manifest.complete_through == 1
        assert trace.manifest.format_version == TRACE_FORMAT_VERSION == 6

    def test_run_id(self, trace):
        assert trace.run_id == "run-test"

    def test_step_count(self, trace):
        assert trace.step_count == 2

    def test_step_count_uses_committed_entries_after_a_gap(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0))
        wal.commit_step(2, step_record(2))
        trace = RunTrace(wal.run_dir)
        assert trace.step_count == 2
        assert [record.step for record in trace.step_records] == [0, 2]
        assert trace.step(1) is None
        assert trace.step(2) is not None

    def test_config_lazy(self, trace):
        assert trace.config.agent_id == "test_agent"
        assert trace.config.loop_id == "react"
        assert trace.config.task == "find the answer"

    def test_result_lazy(self, trace):
        assert trace.result is not None
        assert trace.result.answer == "42"

    def test_result_none_if_missing(self, tmp_path):
        """Result is None if the run didn't write result.json."""
        wal = new_wal(tmp_path, "run-incomplete")
        wal.commit_step(0, step_record(0))

        assert RunTrace(wal.run_dir).result is None

    def test_step_records_lazy(self, trace):
        assert [record.step for record in trace.step_records] == [0, 1]

    def test_step(self, trace):
        record = trace.step(0)
        assert record is not None
        assert record.step == 0
        assert record.llm_output == "click(btn)"

    def test_step_missing(self, trace):
        assert trace.step(99) is None

    def test_reading_the_result_reads_nothing_else(self, trace, monkeypatch):
        """Lazy: the exact_match judge pays for result.json and nothing more."""
        opened: list[str] = []
        original = Path.read_bytes

        def recording(self: Path) -> bytes:
            opened.append(self.name)
            return original(self)

        monkeypatch.setattr(Path, "read_bytes", recording)
        _ = trace.result
        assert opened == ["result.json"]


class TestReaderSnapshots:
    def test_manifest_edits_cannot_retarget_committed_reads(self, trace, reference):
        (reference.run_dir / "orphan.json").write_text(step_record(0).model_dump_json())
        manifest = trace.manifest
        manifest.steps[0].step_file = "orphan.json"
        manifest.steps[0].fragments.clear()
        manifest.steps.clear()
        manifest.run_id = "other-run"

        assert trace.run_id == "run-test"
        assert trace.step_count == 2
        assert trace.screenshot(0) == PNG_BYTES
        assert trace.step(0).llm_output == "click(btn)"
        with pytest.raises(KeyError):
            trace.read_committed("orphan.json")

    def test_committed_listing_cannot_admit_orphans(self, trace, reference):
        (reference.run_dir / "orphan.json").write_bytes(b"uncommitted")
        files = trace.committed_files
        files.append(CommittedFile(path="orphan.json", kind="step"))
        files[0] = CommittedFile(path="orphan.json", kind="step")

        with pytest.raises(KeyError):
            trace.read_committed("orphan.json")
        assert trace.committed_files[0].path == "manifest.json"

    @pytest.mark.parametrize("source", ["step", "trailing", "listing"])
    def test_public_extra_metadata_cannot_disable_integrity_checks(self, trace, reference, source):
        if source == "step":
            record = trace.extra_files(0)[0]
        elif source == "trailing":
            record = trace.trailing_extra_files[0]
        else:
            record = next(file.extra for file in trace.committed_files if file.extra is not None)
        assert record is not None
        path = record.path
        changed = b"corrupt replacement"
        (reference.run_dir / path).write_bytes(changed)
        record.size = len(changed)
        record.sha256 = sha256_hex(changed)
        record.meta["rewritten"] = {"nested": [True]}

        with pytest.raises(TraceSchemaError, match="does not match its index"):
            trace.read_extra(path)

    def test_config_edits_do_not_change_recording_scope(self, trace):
        config = trace.config
        config.recording = RecordingPlan(trajectory=False)
        config.resolved["injected"] = {"value": [1]}

        assert len(trace.llm_messages(0)) == 2
        assert trace.config.recording.trajectory is True
        assert "injected" not in trace.config.resolved

    def test_cached_evidence_is_independent_for_each_caller(self, trace):
        result = trace.result
        assert result is not None
        result.answer = "changed"
        records = trace.step_records
        records[0].actions.clear()
        records.clear()
        calls = trace.trailing_calls
        calls[0].messages.clear()
        trace.screenshots.clear()

        assert trace.result.answer == "42"
        assert len(trace.step_records) == 2
        assert trace.step_records[0].actions
        assert trace.trailing_calls[0].messages
        assert len(trace.screenshots) == 2

    def test_internal_reads_do_not_copy_the_public_manifest_or_listing(self, trace, monkeypatch):
        def no_snapshot(_):
            raise AssertionError("an internal lookup copied the complete public snapshot")

        monkeypatch.setattr(RunTrace, "manifest", property(no_snapshot))
        monkeypatch.setattr(RunTrace, "committed_files", property(no_snapshot))
        monkeypatch.setattr(RunTrace, "config", property(no_snapshot))
        for _ in range(20):
            assert trace.step_count == 2
            assert trace.screenshot(0) == PNG_BYTES
            assert trace.llm_messages(0)
            assert trace.read_committed("steps/000.json")


class TestFragmentsReadBackAsTheirOwnType:
    def test_a_dom_fragment_comes_back_typed(self, trace):
        dom = trace.observation_fragment(0, "browser.dom")

        assert isinstance(dom, DOMObs)
        assert isinstance(dom.element_tree, DOMState)
        # One map, as at capture time.
        assert dom.element_tree.selector_map is dom.selector_map
        entry = dom.selector_map[FRAME_BUTTON_HANDLE]
        assert isinstance(entry, DOMElementNode)
        assert entry.bounds is not None
        assert entry.text_content == "Confirm card"

    def test_a_screenshot_gets_its_bytes_back(self, trace):
        shot = trace.observation_fragment(0, "browser.screenshot")

        assert isinstance(shot, ScreenshotObs)
        assert shot.image_bytes == PNG_BYTES
        assert shot.viewport_width == 1280

    def test_a_failed_capture_is_the_failure_not_an_empty_fragment(self, trace):
        meta = trace.observation_fragment(0, "browser.browser_meta")

        assert isinstance(meta, FragmentFailure)
        assert meta.error == "TimeoutError"
        assert meta.capability == "browser"

    def test_a_loop_fragment_reads_back(self, trace):
        task = trace.observation_fragment(0, "loop.task")

        assert isinstance(task, TaskObs)
        assert task.user_request == "find the answer"
        assert task.fragment_id == "loop.task"

    @pytest.mark.parametrize(
        ("step", "key"),
        [
            (1, "browser.dom"),  # not recorded at this step
            (0, "desktop.screenshot"),  # not active in this run
            (2, "browser.screenshot"),  # step never committed
            (99, "loop.task"),
        ],
    )
    def test_what_was_not_recorded_is_none(self, trace, step, key):
        assert trace.observation_fragment(step, key) is None


class TestScreenshots:
    def test_screenshot_bytes_per_step(self, trace):
        assert trace.screenshot(0) == PNG_BYTES
        assert trace.screenshot(1) == JPEG_BYTES
        assert trace.screenshot(2) is None

    def test_screenshots_are_the_committed_artifacts_in_step_order(self, trace):
        """A JPEG run writes ``.jpg``; the list is the manifest's, not a glob's."""
        assert [path.name for path in trace.screenshots] == [
            "000_browser.screenshot.png",
            "001_browser.screenshot.jpg",
        ]
        assert trace.screenshots[1].read_bytes() == JPEG_BYTES

    def test_a_failed_screenshot_is_none_and_says_why_elsewhere(self, tmp_path):
        from tests._helpers.trace_builder import failure

        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), fragments=[failure(0, "browser.screenshot")])

        trace = RunTrace(wal.run_dir)
        assert trace.screenshot(0) is None
        assert trace.screenshots == []
        assert isinstance(trace.observation_fragment(0, "browser.screenshot"), FragmentFailure)


class TestLLMCalls:
    def test_every_call_of_a_step_in_start_order(self, trace, reference):
        calls = trace.llm_messages(0)

        assert [(call.step, call.call, call.status) for call in calls] == [
            (0, 0, "failed"),
            (0, 1, "completed"),
        ]
        refused, answered = calls
        assert refused.failure is not None
        assert refused.failure.error == "StructuredOutputValidationError"
        assert refused.failure.raw_output == '{"thought": 1}'
        assert refused.failure.usage is not None
        assert refused.failure.usage.total_tokens == 15
        assert refused.completion is None
        assert answered.completion is not None
        assert answered.completion.completion == {"thought": "a button", "action": "click"}
        assert answered.failure is None

    def test_the_prompt_comes_back_as_messages(self, trace):
        call = trace.llm_messages(0)[0]

        assert isinstance(call.messages[0], SystemMessage)
        assert isinstance(call.messages[1], UserMessage)
        assert call.messages[1].text == "what do you see?"

    def test_every_structured_call_references_one_schema(self, trace, reference):
        refs = [call.output_format for step in (0, 1) for call in trace.llm_messages(step)]

        assert all(ref is not None and ref.sha256 == reference.schema_sha for ref in refs)
        assert all(ref is not None and ref.name == "Reply" for ref in refs)
        assert trace.manifest.output_formats == [reference.schema_sha]
        assert trace.output_schema(reference.schema_sha) == Reply.model_json_schema()

    def test_an_uncommitted_step_has_no_calls_its_calls_trail(self, trace):
        assert trace.llm_messages(2) == []

        (trailing,) = trace.trailing_calls
        assert (trailing.step, trailing.call, trailing.status) == (2, 0, "cancelled")
        assert trailing.failure is not None
        assert trailing.failure.error == "CancelledError"
        assert trailing.completion is None
        assert trailing.messages[-1].text == "step two, never answered"

    def test_an_unknown_schema_is_a_key_error(self, trace):
        with pytest.raises(KeyError):
            trace.output_schema("0" * 64)


class TestExtraFiles:
    def test_each_step_lists_what_it_committed(self, trace, reference):
        assert [record.path for record in trace.extra_files(0)] == [
            item.rel_path for item in reference.extras[0]
        ]
        assert [record.path for record in trace.extra_files(1)] == [
            item.rel_path for item in reference.extras[1]
        ]
        assert trace.extra_files(2) == []

    def test_the_index_records_size_digest_and_meta(self, trace, reference):
        response = trace.extra_files(0)[0]
        assert response.meta == {"kind": "net"}
        assert response.size == len(reference.extras[0][0].data)
        assert response.content_addressed is False

        shared = trace.extra_files(1)[0]
        assert shared.content_addressed is True
        assert shared.path == reference.shared_blob.rel_path

    def test_trailing_extra_files(self, trace):
        assert [record.path for record in trace.trailing_extra_files] == ["env/final/summary.txt"]
        assert trace.read_extra("env/final/summary.txt") == b"run ended mid-step"

    def test_read_extra_returns_the_committed_bytes(self, trace, reference):
        assert trace.read_extra(reference.shared_blob.rel_path) == reference.shared_blob.data
        assert trace.read_extra("env/steps/001/x.y/name") == b"step one evidence"

    def test_an_uncommitted_path_is_a_key_error(self, trace):
        with pytest.raises(KeyError):
            trace.read_extra("env/steps/009/never")


@pytest.fixture()
def archived(tmp_path) -> ArchiveTrace:
    return build_archive_trace(tmp_path)


class TestArchiveAccessors:
    """Environment evidence, resolved only through the extra indexes (§5.5)."""

    @pytest.mark.parametrize("source", ["files", "iterator", "run"])
    def test_archive_metadata_is_an_independent_snapshot(self, archived, source):
        trace = RunTrace(archived.run_dir)
        if source == "files":
            record = trace.archive_files(0, STEPS_ARCHIVER)[0]
        elif source == "iterator":
            record = next(trace.iter_archive(STEPS_ARCHIVER))[1]
        else:
            record = trace.archive_run_file(ARCHIVE_CAPABILITY)
        assert record is not None
        original = record.model_copy(deep=True)
        record.archiver_id = "other.archive"
        record.ids["injected"] = "other-session"
        record.run_version = 999

        if source == "run":
            assert trace.archive_run_file(ARCHIVE_CAPABILITY) == original
        else:
            assert trace.archive_files(0, STEPS_ARCHIVER)[0] == original
            assert next(trace.iter_archive(STEPS_ARCHIVER))[1] == original

    def test_a_steps_files_of_one_archiver_in_index_order(self, archived):
        trace = RunTrace(archived.run_dir)
        assert [r.path for r in trace.archive_files(0, STEPS_ARCHIVER)] == [
            archived.evidence[0][0].rel_path
        ]
        assert [r.name for r in trace.archive_files(1, STORAGE_ARCHIVER)] == ["storage.json"]
        assert trace.archive_files(1, BLOBS_ARCHIVER)[0].path == archived.shared_blob.rel_path
        # Another archiver's files, a run record, an uncommitted step: none.
        assert trace.archive_files(0, STORAGE_ARCHIVER) == []
        assert trace.archive_files(0, ARCHIVE_CAPABILITY) == []
        assert trace.archive_files(3, STEPS_ARCHIVER) == []
        assert trace.archive_files(9, STEPS_ARCHIVER) == []

    def test_a_record_is_its_index_entry_and_its_metadata(self, archived):
        trace = RunTrace(archived.run_dir)
        (record,) = trace.archive_files(0, STEPS_ARCHIVER)
        written = archived.evidence[0][0]
        assert (record.path, record.size, record.sha256, record.content_addressed) == (
            written.rel_path,
            len(written.data),
            sha256_hex(written.data),
            False,
        )
        assert (
            record.model_dump(exclude={"path", "size", "sha256", "content_addressed"})
            == (written.meta["archive"])
        )
        assert (record.capability_id, record.cycle, record.ids) == (
            ARCHIVE_CAPABILITY,
            1,
            {"frame": "main"},
        )
        assert trace.read_extra(record.path) == written.data

    def test_iter_archive_walks_the_steps_then_the_trailing_commits_when_asked(self, archived):
        trace = RunTrace(archived.run_dir)
        assert [(step, r.name) for step, r in trace.iter_archive(STEPS_ARCHIVER)] == [
            (0, "cycle_001.json"),
            (1, "cycle_002.json"),
            (2, "cycle_003.json"),
        ]
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_ARCHIVER, include_trailing=True)
        ][-1] == (None, "tail.json")
        bodies = list(trace.iter_archive(BLOBS_ARCHIVER, include_trailing=True))
        assert [step for step, _ in bodies] == [0, 1, None]
        assert bodies[0][1].path == bodies[1][1].path == archived.shared_blob.rel_path
        # Run records are not step evidence.
        assert list(trace.iter_archive(ARCHIVE_CAPABILITY, include_trailing=True)) == []

    def test_a_blob_is_read_by_its_digest(self, archived):
        trace = RunTrace(archived.run_dir)
        shared = archived.shared_blob
        assert trace.blob(sha256_hex(shared.data)) == shared.data
        tail_body = archived.evidence[-1][1]
        assert trace.blob(sha256_hex(tail_body.data)) == tail_body.data
        with pytest.raises(KeyError):
            trace.blob(sha256_hex(b"never committed"))
        with pytest.raises(KeyError):  # a path, not a digest, opens nothing
            trace.blob("../../manifest.json")

    def test_the_run_record_is_the_highest_committed_version(self, archived):
        trace = RunTrace(archived.run_dir)
        latest = trace.archive_run_file(ARCHIVE_CAPABILITY)
        assert latest is not None
        assert (latest.path, latest.run_version, latest.scope) == (
            archived.run_records[1].rel_path,
            1,
            "run",
        )
        assert trace.read_extra(latest.path) == b'{"generation": 1}'
        assert trace.archive_run_file("nobody") is None

    def test_the_version_not_the_commit_order_decides(self, tmp_path):
        """A version committed later in a trailing entry, but numbered lower, is not the latest."""
        wal = new_wal(tmp_path)
        v1 = archive_file("cap", "fp.json", b'{"v": 1}', scope="run", run_version=1)
        v0 = archive_file("cap", "fp.json", b'{"v": 0}', scope="run", run_version=0)
        wal.commit_step(0, step_record(0), extra_files=[v1])
        wal.commit_trailing(extra_files=[v0])
        latest = RunTrace(wal.run_dir).archive_run_file("cap")
        assert latest is not None and latest.run_version == 1

    def test_an_extra_file_without_archive_metadata_is_no_archive_file(self, archived):
        trace = RunTrace(archived.run_dir)
        plain = archived.plain_extra
        assert plain is not None
        assert plain.rel_path in [r.path for r in trace.extra_files(0)]
        assert trace.read_extra(plain.rel_path) == b"not evidence"
        everyone = [STEPS_ARCHIVER, BLOBS_ARCHIVER, STORAGE_ARCHIVER, ARCHIVE_CAPABILITY]
        assert all(
            plain.rel_path not in [r.path for r in trace.archive_files(0, a)] for a in everyone
        )

    def test_a_blob_another_producer_put_at_an_archive_address_is_no_archive_file(self, reference):
        """``build_reference_trace`` puts a plain extra file at an ``env/blobs/`` address."""
        trace = RunTrace(reference.run_dir)
        shared = reference.shared_blob
        assert trace.read_extra(shared.rel_path) == shared.data
        with pytest.raises(KeyError):
            trace.blob(sha256_hex(shared.data))

    def test_a_trace_without_evidence_answers_empty(self, trace):
        """The M27 reference trace has ``env/`` extra files, and none of them is an archive file."""
        assert trace.archive_files(0, "browser.network") == []
        assert list(trace.iter_archive("browser.network", include_trailing=True)) == []
        assert trace.archive_run_file("browser") is None


class _IndexLine(BaseModel):
    """A line type a capability might write — the trace layer knows none of them."""

    kind: str
    n: int


def _lines_trace(tmp_path, data: bytes) -> tuple[RunTrace, str]:
    wal = new_wal(tmp_path)
    file = archive_file(
        "builder.steps", "index.jsonl", data, step=0, media_type="application/x-ndjson"
    )
    wal.commit_step(0, step_record(0), extra_files=[file])
    return RunTrace(wal.run_dir), file.rel_path


class TestArchiveLines:
    """``archive_lines``: one committed JSON-lines file, read back as the caller's model."""

    def test_each_line_comes_back_as_the_model_in_file_order(self, tmp_path):
        trace, path = _lines_trace(
            tmp_path, b'{"kind": "a", "n": 1}\n{"kind": "b", "n": 2}\n{"kind": "c", "n": 3}\n'
        )
        lines = trace.archive_lines(path, _IndexLine)
        assert lines == [
            _IndexLine(kind="a", n=1),
            _IndexLine(kind="b", n=2),
            _IndexLine(kind="c", n=3),
        ]
        assert all(type(line) is _IndexLine for line in lines)

    def test_blank_lines_are_skipped_and_crlf_is_fine(self, tmp_path):
        trace, path = _lines_trace(
            tmp_path, b'\n{"kind": "a", "n": 1}\r\n\n   \n{"kind": "b", "n": 2}'
        )
        assert [line.n for line in trace.archive_lines(path, _IndexLine)] == [1, 2]

    def test_an_empty_file_is_no_lines(self, tmp_path):
        trace, path = _lines_trace(tmp_path, b"\n")
        assert trace.archive_lines(path, _IndexLine) == []

    def test_a_line_that_is_not_json_names_the_file_and_the_line(self, tmp_path):
        trace, path = _lines_trace(tmp_path, b'{"kind": "a", "n": 1}\n\n{"kind": \n')
        with pytest.raises(
            TraceSchemaError, match=r"index\.jsonl line 3 does not parse as JSON"
        ) as caught:
            trace.archive_lines(path, _IndexLine)
        assert caught.value.path == trace.trace_dir / path

    def test_a_line_that_is_not_utf8_is_unreadable(self, tmp_path):
        trace, path = _lines_trace(tmp_path, b'{"kind": "\xff", "n": 1}\n')
        with pytest.raises(TraceSchemaError, match="line 1 does not parse as JSON"):
            trace.archive_lines(path, _IndexLine)

    def test_a_line_of_the_wrong_shape_names_the_model_and_the_line(self, tmp_path):
        trace, path = _lines_trace(tmp_path, b'{"kind": "a", "n": 1}\n{"kind": "b", "n": "two"}\n')
        with pytest.raises(TraceSchemaError, match=r"line 2 does not parse as a _IndexLine"):
            trace.archive_lines(path, _IndexLine)

    def test_an_uncommitted_path_is_a_key_error(self, tmp_path):
        trace, _ = _lines_trace(tmp_path, b'{"kind": "a", "n": 1}\n')
        with pytest.raises(KeyError):
            trace.archive_lines("env/steps/000/builder.steps/other.jsonl", _IndexLine)

    def test_bytes_that_changed_since_the_commit_are_refused(self, tmp_path):
        trace, path = _lines_trace(tmp_path, b'{"kind": "a", "n": 1}\n')
        (trace.trace_dir / path).write_bytes(b'{"kind": "a", "n": 9}\n')
        with pytest.raises(TraceSchemaError, match="does not match its index entry"):
            trace.archive_lines(path, _IndexLine)


class TestCommittedFiles:
    """What a run consists of: every listing in commit order, read through each file's own checks."""

    def test_every_listing_in_commit_order(self, trace, reference):
        listed = [(file.kind, file.step, file.fragment) for file in trace.committed_files]
        step0 = [
            ("step", 0, None),
            ("fragment", 0, "browser.dom"),
            ("fragment", 0, "browser.screenshot"),
            ("artifact", 0, "browser.screenshot"),
            ("fragment", 0, "browser.browser_meta"),  # the failure recorded in its place
            ("fragment", 0, "loop.task"),
            ("llm_call", 0, None),
            ("llm_call", 0, None),
            ("call_metrics", 0, None),
            ("call_metrics", 0, None),
            ("extra_index", 0, None),
            ("extra", 0, None),
            ("extra", 0, None),
        ]
        step1 = [
            ("step", 1, None),
            ("fragment", 1, "browser.screenshot"),
            ("artifact", 1, "browser.screenshot"),
            ("fragment", 1, "loop.task"),
            ("llm_call", 1, None),
            ("call_metrics", 1, None),
            ("extra_index", 1, None),
            ("extra", 1, None),
            ("extra", 1, None),
        ]
        trailing = [
            ("llm_call", None, None),
            ("call_metrics", None, None),
            ("extra_index", None, None),
            ("extra", None, None),
        ]
        own = [("manifest", None, None), ("config", None, None), ("result", None, None)]
        last = [("output_schema", None, None)] + [("prompt_image", None, None)] * 3
        assert listed == own + step0 + step1 + trailing + last
        images = [file.path for file in trace.committed_files if file.kind == "prompt_image"]
        assert images == [image.rel_path for image in reference.images], (
            "each image once, in the order the commits first carried it"
        )
        extras = [(file.step, file.path) for file in trace.committed_files if file.kind == "extra"]
        assert extras == [
            *((0, item.rel_path) for item in reference.extras[0]),
            *((1, item.rel_path) for item in reference.extras[1]),
            *((None, item.rel_path) for item in reference.extras[-1]),
        ], "the shared blob once per commit that lists it"

    def test_each_file_reads_back_as_the_bytes_on_disk(self, trace, reference):
        for file in trace.committed_files:
            assert trace.read_committed(file.path) == (reference.run_dir / file.path).read_bytes()
        assert trace.read_committed(reference.shared_blob.rel_path) == reference.shared_blob.data

    def test_a_run_that_wrote_no_result_lists_none(self, tmp_path):
        wal = new_wal(tmp_path, "run-incomplete")
        wal.commit_step(0, step_record(0))
        trace = RunTrace(wal.run_dir)
        assert [file.kind for file in trace.committed_files] == ["manifest", "config", "step"]
        with pytest.raises(KeyError):
            trace.read_committed("result.json")

    def test_an_unlisted_path_is_a_key_error(self, trace, reference):
        (reference.run_dir / "observations" / "orphan.json").write_bytes(b"{}")
        for path in ("observations/orphan.json", "steps/009.json", "env/steps/009/never"):
            with pytest.raises(KeyError):
                trace.read_committed(path)

    def test_evidence_carries_its_archive_record(self, archived):
        trace = RunTrace(archived.run_dir)
        extras = [file for file in trace.committed_files if file.kind == "extra"]
        plain = [file for file in extras if file.archive is None]
        assert archived.plain_extra is not None
        assert [file.path for file in plain] == [archived.plain_extra.rel_path]
        shared = [file for file in extras if file.path == archived.shared_blob.rel_path]
        assert [file.step for file in shared] == [0, 1]
        assert all(file.archive and file.archive.content_addressed for file in shared)
        records = [file.archive for file in extras if file.archive is not None]
        assert [record.run_version for record in records if record.scope == "run"] == [0, 1]
        assert all(file.extra is not None and file.extra.path == file.path for file in extras)

    @pytest.mark.parametrize(
        ("damage", "path_of", "message"),
        [
            ("rewrite", lambda r: r.extras[0][0].rel_path, "does not match its index entry"),
            ("empty", lambda r: "observations/000_browser.screenshot.png", "empty artifact"),
            (
                "rewrite",
                lambda r: f"messages/output_formats/{r.schema_sha}.json",
                "content-addressed",
            ),
            ("delete", lambda r: "steps/001.json", "missing on disk"),
        ],
        ids=["extra", "artifact", "output_schema", "step"],
    )
    def test_damage_is_refused_by_the_file_s_own_check(
        self, trace, reference, damage, path_of, message
    ):
        path = path_of(reference)
        target = reference.run_dir / path
        if damage == "delete":
            target.unlink()
        else:
            target.write_bytes(b"" if damage == "empty" else b'{"changed": true}')
        with pytest.raises(TraceSchemaError, match=message):
            trace.read_committed(path)


class TestTheReaderGivesBackWhatTheWriterWasHanded:
    """The round-trip: models in, the same models out.

    The reader used to return bare dicts, so the only way to learn a field
    name was to open a JSON file — and guessing wrong (``rec["step_index"]``
    for what is really ``rec.step``) type-checked cleanly and blew up at
    runtime.
    """

    def test_step_record_survives_the_round_trip(self, trace):
        record = trace.step(0)

        assert isinstance(record, StepRecord)
        assert record == step_record(0)

    def test_step_records_are_models_not_dicts(self, trace):
        records = trace.step_records

        assert [r.step for r in records] == [0, 1]
        assert all(isinstance(r, StepRecord) for r in records)

    def test_result_survives_the_round_trip(self, tmp_path):
        wal = new_wal(tmp_path)
        wal.write_result(
            agent_result(
                metrics=AgentMetrics(
                    execution_ms=1234,
                    steps=2,
                    llm_calls=2,
                    usage=AgentUsage(
                        total_prompt_tokens=20, total_completion_tokens=10, total_tokens=30
                    ),
                )
            )
        )
        result = RunTrace(wal.run_dir).result

        assert isinstance(result, AgentResult)
        assert result.task_id == "task-001"
        assert result.answer == "42"
        assert result.agent_done is AgentDoneStatus.DONE
        assert result.agent_reported_success is True
        assert result.model_id == "gpt-4o-mini"
        assert result.metrics.steps == 2
        assert result.metrics.usage is not None
        # The provider's recorded total survives independently of the subtotals.
        assert result.metrics.usage.total_tokens == 30

    def test_a_wrong_field_name_is_an_attribute_error_not_a_key_error(self, trace):
        """The failure mode that motivated this: ``step_index``."""
        record = trace.step(0)

        with pytest.raises(AttributeError, match="StepRecord"):
            _ = record.step_index  # type: ignore[attr-defined]


class TestTheManifestIsTheCommitPoint:
    """A step file the manifest never claimed did not happen.

    ``commit_step`` writes ``steps/NNN.json`` and renames the manifest
    afterwards — that gap is the whole reason the WAL is a WAL. A crash
    inside it leaves a complete, parseable step file for a step that never
    committed. (Every other accessor is pinned the same way in
    ``test_crash_window.py``.)
    """

    def _half_committed(self, tmp_path) -> RunTrace:
        wal = new_wal(tmp_path, "run-crash")
        wal.commit_step(0, step_record(0))
        # Step 1: written, never committed — the crash window.
        (wal.run_dir / "steps/001.json").write_text(step_record(1).model_dump_json(), "utf-8")
        return RunTrace(wal.run_dir)

    def test_an_uncommitted_step_is_not_a_step(self, tmp_path):
        trace = self._half_committed(tmp_path)
        assert (trace.trace_dir / "steps/001.json").is_file()
        assert trace.step(1) is None

    def test_the_three_step_accessors_agree(self, tmp_path):
        trace = self._half_committed(tmp_path)
        assert trace.step_count == 1
        assert [rec.step for rec in trace.step_records] == [0]
        assert trace.step(0) is not None

    def test_a_committed_step_whose_file_vanished_is_damage_not_absence(self, tmp_path):
        """Listed but missing raises: a run that lost a committed file is corrupt."""
        trace = self._half_committed(tmp_path)
        (trace.trace_dir / "steps/000.json").unlink()

        with pytest.raises(TraceSchemaError, match="000.json") as exc_info:
            trace.step(0)
        assert exc_info.value.path == trace.trace_dir / "steps/000.json"
        with pytest.raises(TraceSchemaError):
            _ = trace.step_records


class TestUnreadableTracesSayWhy:
    def _trace_with_result(self, tmp_path, payload: dict) -> RunTrace:
        wal = WALWriter(tmp_path, "run-legacy")
        wal.init_run(TraceConfig())
        wal.finalize(TraceStatus.COMPLETE)
        (wal.run_dir / "result.json").write_text(json.dumps(payload), "utf-8")
        return RunTrace(wal.run_dir)

    def test_a_legacy_result_names_the_schema_not_the_renamed_field(self, tmp_path):
        """A 2.0 trace must not fail with ``extra_forbidden: agent_success``."""
        trace = self._trace_with_result(
            tmp_path,
            {
                "schema_version": "2.0",
                "task_id": "t-1",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "env_status": "success",
                "agent_done": "done",
                "agent_success": True,
                "answer": "42",
                "metrics": {"execution_ms": 1, "steps": 1},
            },
        )

        with pytest.raises(TraceSchemaError) as exc_info:
            _ = trace.result

        message = str(exc_info.value)
        assert "predates AgentResult schema 3.0" in message
        assert "'2.0'" in message
        assert "result.json" in message
        # The confusing field-level noise stays out of it.
        assert "agent_success" not in message

    def test_a_malformed_current_result_keeps_the_field_errors(self, tmp_path):
        """When the versions agree, pydantic's detail is the only clue."""
        trace = self._trace_with_result(tmp_path, {"schema_version": "3.0", "task_id": "t-1"})

        with pytest.raises(TraceSchemaError) as exc_info:
            _ = trace.result

        message = str(exc_info.value)
        assert "does not parse as an AgentResult" in message
        assert "metrics" in message  # the missing required field

    def test_an_unreadable_result_is_not_a_missing_one(self, tmp_path):
        """The distinction this error exists to preserve."""
        trace = self._trace_with_result(tmp_path, {"schema_version": "2.0"})

        with pytest.raises(TraceSchemaError):
            _ = trace.result

    def test_a_corrupt_step_file_names_the_file(self, tmp_path):
        wal = new_wal(tmp_path, "run-badstep")
        wal.commit_step(0, step_record(0))
        (wal.run_dir / "steps/000.json").write_text('{"step": "not-an-int"}', "utf-8")

        trace = RunTrace(wal.run_dir)
        with pytest.raises(TraceSchemaError, match="does not parse as a StepRecord"):
            _ = trace.step_records


def _nested(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = {"k": value}
    return value


class TestWhateverTheWriterCommitsTheReaderReads:
    """The reader parses with ``json.loads``, never pydantic's ``validate_json``.

    pydantic-core's serializer, which writes every trace file, emits JSON up
    to ~254 levels deep; its parser refuses anything past 200, reporting it
    as "Invalid JSON: recursion limit exceeded". A trace file in between —
    a deep structured reply, a deep tool result, a deep DOM page — was
    committed and could never be read back.
    """

    def test_a_deep_structured_reply(self, tmp_path):
        from pydantic import BaseModel

        from tests._helpers.trace_builder import completed_call

        class Tree(BaseModel):
            tree: object

        wal = new_wal(tmp_path)
        reply = Tree(tree=_nested(230))
        wal.commit_step(0, step_record(0), llm_calls=[completed_call(0, 0, "go", reply=reply)])

        (call,) = RunTrace(wal.run_dir).llm_messages(0)

        assert as_chat(call).completion is not None
        assert required(as_chat(call).completion).completion == reply.model_dump(mode="json")

    def test_a_deep_fragment(self, tmp_path):
        from spectragent.schemas.observation.fragments import ToolResultObs
        from tests._helpers.trace_builder import fragment

        wal = new_wal(tmp_path)
        result = _nested(230)
        wal.commit_step(
            0, step_record(0), fragments=[fragment(0, ToolResultObs(tool_name="t", result=result))]
        )

        read = RunTrace(wal.run_dir).observation_fragment(0, "tool_sandbox.tool_result")

        assert isinstance(read, ToolResultObs)
        assert read.result == result

    def test_json_deeper_than_the_interpreter_can_parse_is_unreadable_not_a_crash(self, tmp_path):
        """Far past anything the writer emits — so damage, named like any other."""
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0))
        path = wal.run_dir / "steps/000.json"
        path.write_bytes(b"[" * 100_000 + b"]" * 100_000)

        with pytest.raises(TraceSchemaError) as exc_info:
            RunTrace(wal.run_dir).step(0)
        assert exc_info.value.path == path


class TestAnOfflineReaderFindsFragmentClasses:
    """An evaluation process may never have imported the module defining a fragment.

    Browser fragments register when their watchdog's module is imported. On
    a registry miss the reader discovers the fragment packages — once — and
    retries, rather than failing a trace it could have read.
    """

    def test_a_miss_discovers_then_retries(self, tmp_path, monkeypatch):
        import spectragent.core.trace.reader as reader_module
        from spectragent.core.capabilities.browser.observations import BrowserMetaObs
        from spectragent.schemas.observation._registrar import ObservationRegistrar
        from tests._helpers.trace_builder import fragment

        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0), fragments=[fragment(0, BrowserMetaObs(url="https://a"))])

        unimported = {"browser.browser_meta"}
        real_get = ObservationRegistrar.get
        discovered: list[tuple[str, ...]] = []

        def get(key: str) -> type:
            if key in unimported:
                raise KeyError(key)
            return real_get(key)

        def discover(*packages: str) -> list[str]:
            discovered.append(packages)
            unimported.clear()  # the import registered it
            return list(packages)

        monkeypatch.setattr(ObservationRegistrar, "get", get)
        monkeypatch.setattr(reader_module, "discover", discover)

        trace = RunTrace(wal.run_dir)
        meta = trace.observation_fragment(0, "browser.browser_meta")
        assert isinstance(meta, BrowserMetaObs)
        assert meta.url == "https://a"
        assert discovered == [("spectragent.schemas", "spectragent.core.capabilities")]

        trace.observation_fragment(0, "browser.browser_meta")
        assert len(discovered) == 1


class TestEvals:
    """``evals()`` — the post-run verdicts, the one accessor that lists a directory (§5.5)."""

    def test_no_evals_directory_is_empty(self, trace, reference):
        (reference.run_dir / "evals").rmdir()
        assert trace.evals() == {}

    def test_an_empty_evals_directory_is_empty(self, trace, reference):
        assert (reference.run_dir / "evals").is_dir(), "the WAL creates it at init"
        assert trace.evals() == {}

    def test_verdicts_come_back_typed_and_sorted_by_slug(self, trace, reference):
        from spectragent.schemas.eval_result import EvalResult
        from tests._helpers.trace_builder import eval_result, write_verdict

        write_verdict(reference.run_dir, "online_mind2web-ab12", eval_result(predicted_label=0))
        write_verdict(reference.run_dir, "exact_match", eval_result(evaluator_id="exact_match"))
        write_verdict(reference.run_dir, "a-b", eval_result())
        write_verdict(reference.run_dir, "a", eval_result())
        verdicts = trace.evals()
        assert list(verdicts) == ["a", "a-b", "exact_match", "online_mind2web-ab12"]
        assert all(isinstance(v, EvalResult) for v in verdicts.values())
        assert verdicts["online_mind2web-ab12"].predicted_label == 0

    def test_not_cached(self, trace, reference):
        from tests._helpers.trace_builder import eval_result, write_verdict

        assert trace.evals() == {}
        write_verdict(reference.run_dir, "exact_match", eval_result())
        assert list(trace.evals()) == ["exact_match"], "a verdict written later is seen"

    def test_only_json_files_count(self, trace, reference):
        from tests._helpers.trace_builder import eval_result, write_verdict

        write_verdict(reference.run_dir, "exact_match", eval_result())
        evals_dir = reference.run_dir / "evals"
        (evals_dir / "tmp0123456789abcdef.tmp").write_bytes(b"{torn")  # an interrupted write
        (evals_dir / "notes.txt").write_text("hand-written")
        (evals_dir / "nested.json").mkdir()
        assert list(trace.evals()) == ["exact_match"]

    @pytest.mark.parametrize(
        "content",
        [b"{not json", b'{"task_id": "t"}', "\udc80".encode("utf-8", "surrogatepass")],
        ids=["not_json", "not_an_eval_result", "not_utf8"],
    )
    def test_a_corrupt_verdict_names_its_file(self, trace, reference, content):
        evals_dir = reference.run_dir / "evals"
        evals_dir.mkdir(exist_ok=True)  # the WAL creates it at init
        (evals_dir / "exact_match.json").write_bytes(content)
        with pytest.raises(TraceSchemaError, match="exact_match.json") as info:
            trace.evals()
        assert info.value.path == evals_dir / "exact_match.json"

    @pytest.mark.parametrize("version", ["1.0", "1.1", "2.0"])
    def test_old_verdict_versions_are_refused(self, trace, reference, version):
        from tests._helpers.trace_builder import eval_result

        payload = eval_result().model_dump(mode="json")
        payload["schema_version"] = version
        path = reference.run_dir / "evals" / "old.json"
        path.write_text(json.dumps(payload), "utf-8")

        with pytest.raises(TraceSchemaError, match="old.json"):
            trace.evals()

    @pytest.mark.parametrize("missing", ["schema_version", "evaluator_config", "verdict_source"])
    def test_persisted_verdict_requires_identity_and_source(self, trace, reference, missing):
        from tests._helpers.trace_builder import eval_result

        # Fresh construction may default to graded; persisted evidence must
        # prove that the writer actually recorded the source and identity.
        verdict = eval_result()
        assert verdict.verdict_source == "graded"
        payload = verdict.model_dump(mode="json")
        del payload[missing]
        path = reference.run_dir / "evals" / "missing.json"
        path.write_text(json.dumps(payload), "utf-8")

        with pytest.raises(TraceSchemaError, match=missing):
            trace.evals()

    def test_the_first_bad_verdict_named_does_not_depend_on_listing_order(
        self, trace, reference, monkeypatch
    ):
        """The directory is walked sorted, whatever order the file system lists it in."""
        evals_dir = reference.run_dir / "evals"
        for name in ("b_judge.json", "a_judge.json"):
            (evals_dir / name).write_bytes(b"{not json")
        listed = Path.iterdir

        def backwards(self: Path):
            entries = list(listed(self))
            return iter(sorted(entries, reverse=True)) if self == evals_dir else iter(entries)

        monkeypatch.setattr(Path, "iterdir", backwards)
        with pytest.raises(TraceSchemaError) as info:
            trace.evals()
        assert info.value.path == evals_dir / "a_judge.json"

    def test_a_verdict_symlinked_out_of_the_run_is_refused(self, trace, reference, tmp_path):
        from tests._helpers.trace_builder import eval_result, write_verdict

        outside = write_verdict(tmp_path / "elsewhere", "exact_match", eval_result())
        evals_dir = reference.run_dir / "evals"
        evals_dir.mkdir(exist_ok=True)  # the WAL creates it at init
        (evals_dir / "exact_match.json").symlink_to(outside)
        with pytest.raises(TraceSchemaError, match="outside the run directory"):
            trace.evals()

    def test_an_evals_directory_symlinked_out_of_the_run_is_refused(
        self, trace, reference, tmp_path
    ):
        from tests._helpers.trace_builder import eval_result, write_verdict

        write_verdict(tmp_path / "elsewhere", "exact_match", eval_result())
        (reference.run_dir / "evals").rmdir()  # the WAL's own, still empty
        (reference.run_dir / "evals").symlink_to(tmp_path / "elsewhere" / "evals")
        with pytest.raises(TraceSchemaError, match="outside the run directory"):
            trace.evals()

    def test_the_manifest_does_not_list_them(self, trace, reference):
        """Written after the run, verdicts are not in the manifest — and need not be."""
        from tests._helpers.trace_builder import eval_result, write_verdict

        write_verdict(reference.run_dir, "exact_match", eval_result())
        manifest = (reference.run_dir / "manifest.json").read_text()
        assert "evals" not in manifest
        assert list(trace.evals()) == ["exact_match"]


def test_recording_timing_reads_only_later_committed_extra_evidence(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0))
    assert RunTrace(wal.run_dir).recording_timing(0) is None
    timing = TraceFile(
        "recording/step_000.json",
        b'{"step":0,"recording_ms":12.5}',
        meta={"kind": "recording_timing", "step": 0},
    )
    wal.commit_trailing(extra_files=[timing])
    trace = RunTrace(wal.run_dir)
    assert trace.recording_timing(0) == 12.5
    record = trace.step(0)
    assert record is not None
    assert record.recording_ms is None
    assert trace.recording_timing(1) is None
