"""Tests for RunTrace lazy-loading reader — M10.

Every trace here is produced by the real :class:`WALWriter` from real
``StepRecord`` / ``AgentResult`` objects, so the assertions below are a
round-trip: what the writer was handed is what the reader gives back,
as the same type.
"""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.step_record import StepRecord, TokenUsage


def _step(idx: int, llm_output: str) -> StepRecord:
    return StepRecord(
        step=idx,
        observation_summary=f"page {idx}",
        llm_output=llm_output,
        actions=[Action(name="browser.click", params={"index": idx})],
        action_results=[ActionResult(success=True, extracted_content="ok")],
        timestamp=100.0 + idx,
        token_usage=TokenUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    )


def _result(**kw) -> AgentResult:
    defaults = {
        "task_id": "task-001",
        "task": "find the answer",
        "timestamp": datetime.now(UTC),
        "env_status": EnvironmentStatus.SUCCESS,
        "agent_done": AgentDoneStatus.DONE,
        "agent_reported_success": True,
        "answer": "42",
        "model_id": "gpt-4o-mini",
        "metrics": AgentMetrics(
            end_to_end_ms=1234,
            steps=2,
            llm_calls=2,
            usage=AgentUsage(total_prompt_tokens=20, total_completion_tokens=10),
        ),
    }
    defaults.update(kw)
    return AgentResult(**defaults)


@pytest.fixture()
def populated_trace(tmp_path):
    """Create a trace directory with 2 completed steps + result."""
    wal = WALWriter(tmp_path, "run-test")
    config = TraceConfig(
        agent_id="test_agent", loop_id="react", task="find the answer"
    )
    wal.init_run(config)

    # Step 0
    obs_rel = wal.write_observation(
        0, "browser.dom", {"element_tree": "<div>step0</div>"}
    )
    in_rel, out_rel = wal.write_messages(
        0,
        [{"role": "user", "content": "observe"}],
        {"completion": "click(btn)", "usage": {"tokens": 5}},
    )
    step_rel = wal.write_step_record(0, _step(0, "click(btn)"))
    wal.commit_step(
        step=0,
        step_file=step_rel,
        observation_files=[obs_rel],
        message_input_file=in_rel,
        message_output_file=out_rel,
    )

    # Step 1 with screenshot
    wal.write_observation(1, "browser.dom", {"element_tree": "<div>step1</div>"})
    wal.write_observation(1, "browser.screenshot", b"\x89PNGfakedata")
    in_rel, out_rel = wal.write_messages(
        1,
        [{"role": "user", "content": "observe again"}],
        {"completion": "done()", "usage": {"tokens": 3}},
    )
    step_rel = wal.write_step_record(1, _step(1, "done()"))
    wal.commit_step(step=1, step_file=step_rel)

    # Result
    wal.write_result(_result())
    wal.finalize(RunStatus.COMPLETED)

    return tmp_path / "run-test"


class TestRunTrace:
    def test_manifest_loaded_eagerly(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.manifest.run_id == "run-test"
        assert trace.manifest.last_step_completed == 1
        assert trace.manifest.status == RunStatus.COMPLETED

    def test_run_id(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.run_id == "run-test"

    def test_step_count(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.step_count == 2

    def test_config_lazy(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.config.agent_id == "test_agent"
        assert trace.config.loop_id == "react"
        assert trace.config.task == "find the answer"

    def test_result_lazy(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.result is not None
        assert trace.result.answer == "42"

    def test_result_none_if_missing(self, tmp_path):
        """Result is None if run didn't write result.json."""
        wal = WALWriter(tmp_path, "run-incomplete")
        wal.init_run(TraceConfig())
        wal.write_step_record(0, _step(0, "click(btn)"))
        wal.commit_step(step=0, step_file="steps/000.json")

        trace = RunTrace(tmp_path / "run-incomplete")
        assert trace.result is None

    def test_step_records_lazy(self, populated_trace):
        trace = RunTrace(populated_trace)
        records = trace.step_records
        assert len(records) == 2
        assert records[0].step == 0
        assert records[1].step == 1

    def test_step(self, populated_trace):
        trace = RunTrace(populated_trace)
        record = trace.step(0)
        assert record is not None
        assert record.step == 0
        assert record.llm_output == "click(btn)"

    def test_step_missing(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.step(99) is None

    def test_get_observation_json(self, populated_trace):
        trace = RunTrace(populated_trace)
        obs = trace.get_observation(0, "browser.dom")
        assert obs is not None
        assert obs["element_tree"] == "<div>step0</div>"

    def test_get_observation_binary(self, populated_trace):
        trace = RunTrace(populated_trace)
        data = trace.get_observation(1, "browser.screenshot")
        assert isinstance(data, bytes)
        assert data.startswith(b"\x89PNG")

    def test_get_observation_missing(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.get_observation(0, "nonexistent") is None

    def test_get_messages(self, populated_trace):
        trace = RunTrace(populated_trace)
        input_data, output_data = trace.get_messages(0)
        assert input_data is not None
        assert input_data[0]["role"] == "user"
        assert output_data is not None
        assert output_data["completion"] == "click(btn)"

    def test_screenshots(self, populated_trace):
        trace = RunTrace(populated_trace)
        shots = trace.screenshots
        assert len(shots) == 1
        assert shots[0].name == "001_browser.screenshot.png"

    def test_lazy_loading_does_not_read_steps_eagerly(self, populated_trace):
        """Accessing only result should NOT trigger step file reads."""
        trace = RunTrace(populated_trace)

        with patch("spectragent.core.trace.reader.json.loads", wraps=json.loads) as mock_loads:
            # Access result only
            _ = trace.result
            # json.loads should be called for result.json only (not step files)
            call_count = mock_loads.call_count
            assert call_count == 1  # Only result.json


class TestTheReaderGivesBackWhatTheWriterWasHanded:
    """The round-trip: models in, the same models out.

    This is the property that was missing. The reader used to return
    bare dicts, so the only way to learn a field name was to open a JSON
    file — and guessing wrong (``rec["step_index"]`` for what is really
    ``rec.step``) type-checked cleanly and blew up at runtime.
    """

    def test_step_record_survives_the_round_trip(self, populated_trace):
        record = RunTrace(populated_trace).step(0)

        assert isinstance(record, StepRecord)
        # Attribute access, not rec["step"].
        assert record.step == 0
        assert record.observation_summary == "page 0"
        assert record.llm_output == "click(btn)"
        assert record.timestamp == 100.0
        assert record.actions[0].name == "browser.click"
        assert record.actions[0].params == {"index": 0}
        assert record.action_results[0].success is True
        assert record.action_results[0].extracted_content == "ok"
        assert record.token_usage is not None
        assert record.token_usage.total_tokens == 15

    def test_step_records_are_models_not_dicts(self, populated_trace):
        records = RunTrace(populated_trace).step_records

        assert [r.step for r in records] == [0, 1]
        assert all(isinstance(r, StepRecord) for r in records)

    def test_result_survives_the_round_trip(self, populated_trace):
        result = RunTrace(populated_trace).result

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

    def test_a_wrong_field_name_is_an_attribute_error_not_a_key_error(
        self, populated_trace
    ):
        """The failure mode that motivated this: ``step_index``.

        On a dict it was a ``KeyError`` naming nothing useful; on the
        model it is an ``AttributeError`` naming ``StepRecord``, which is
        also what a type checker reports before the code ever runs.
        """
        record = RunTrace(populated_trace).step(0)

        with pytest.raises(AttributeError, match="StepRecord"):
            _ = record.step_index  # type: ignore[attr-defined]


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
        trace = self._trace_with_result(
            tmp_path, {"schema_version": "2.1", "task_id": "t-1"}
        )

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
        wal = WALWriter(tmp_path, "run-badstep")
        wal.init_run(TraceConfig())
        wal.write_step_record(0, _step(0, "click(btn)"))
        (wal.run_dir / "steps/000.json").write_text('{"step": "not-an-int"}', "utf-8")
        wal.commit_step(step=0, step_file="steps/000.json")

        trace = RunTrace(wal.run_dir)
        with pytest.raises(TraceSchemaError, match="does not parse as a StepRecord"):
            _ = trace.step_records
