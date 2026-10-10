"""Tests for RunTrace — the typed, manifest-driven reader.

Every trace here is produced by the real encoder and the real
:class:`WALWriter` from real models (``tests/_helpers/trace_builder.py``),
so the assertions below are a round trip: what the writer was handed is
what the reader gives back, as the same type.
"""

import json
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMState
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.trace.encoding import sha256_hex
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TRACE_FORMAT_VERSION, RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import TaskObs
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
    build_archive_trace,
    build_reference_trace,
    new_wal,
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
        assert trace.manifest.status == RunStatus.ERROR
        assert trace.manifest.format_version == TRACE_FORMAT_VERSION == 2

    def test_run_id(self, trace):
        assert trace.run_id == "run-test"

    def test_step_count(self, trace):
        assert trace.step_count == 2

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
                    end_to_end_ms=1234,
                    steps=2,
                    llm_calls=2,
                    usage=AgentUsage(total_prompt_tokens=20, total_completion_tokens=10),
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
        # Auto-filled by AgentUsage's validator — proves the nested model
        # was re-validated on read, not just re-wrapped.
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
        wal.finalize(RunStatus.COMPLETED)
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
                "metrics": {"end_to_end_ms": 1, "steps": 1},
            },
        )

        with pytest.raises(TraceSchemaError) as exc_info:
            _ = trace.result

        message = str(exc_info.value)
        assert "predates AgentResult schema 2.1" in message
        assert "'2.0'" in message
        assert "result.json" in message
        # The confusing field-level noise stays out of it.
        assert "agent_success" not in message

    def test_a_malformed_current_result_keeps_the_field_errors(self, tmp_path):
        """When the versions agree, pydantic's detail is the only clue."""
        trace = self._trace_with_result(tmp_path, {"schema_version": "2.1", "task_id": "t-1"})

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

        assert call.completion is not None
        assert call.completion.completion == reply.model_dump(mode="json")

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
