"""Tests for RunTrace lazy-loading reader — M10."""

import json
from unittest.mock import patch

import pytest

from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter


@pytest.fixture()
def populated_trace(tmp_path):
    """Create a trace directory with 2 completed steps + result."""
    wal = WALWriter(tmp_path, "run-test")
    config = TraceConfig(
        agent_id="test_agent", loop_id="react", task="find the answer"
    )
    wal.init_run(config)

    # Step 0
    obs_rel = wal.write_observation(0, "dom", {"element_tree": "<div>step0</div>"})
    in_rel, out_rel = wal.write_messages(
        0,
        [{"role": "user", "content": "observe"}],
        {"completion": "click(btn)", "usage": {"tokens": 5}},
    )
    step_rel = wal.write_step_record(0, {"step": 0, "llm_output": "click(btn)"})
    wal.commit_step(
        step=0,
        step_file=step_rel,
        observation_files=[obs_rel],
        message_input_file=in_rel,
        message_output_file=out_rel,
    )

    # Step 1 with screenshot
    wal.write_observation(1, "dom", {"element_tree": "<div>step1</div>"})
    wal.write_observation(1, "screenshot", b"\x89PNGfakedata")
    in_rel, out_rel = wal.write_messages(
        1,
        [{"role": "user", "content": "observe again"}],
        {"completion": "done()", "usage": {"tokens": 3}},
    )
    step_rel = wal.write_step_record(1, {"step": 1, "llm_output": "done()"})
    wal.commit_step(step=1, step_file=step_rel)

    # Result
    wal.write_result({"task_id": "task-001", "answer": "42"})
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
        assert trace.result["answer"] == "42"

    def test_result_none_if_missing(self, tmp_path):
        """Result is None if run didn't write result.json."""
        wal = WALWriter(tmp_path, "run-incomplete")
        wal.init_run(TraceConfig())
        wal.write_step_record(0, {"step": 0})
        wal.commit_step(step=0, step_file="steps/000.json")

        trace = RunTrace(tmp_path / "run-incomplete")
        assert trace.result is None

    def test_step_records_lazy(self, populated_trace):
        trace = RunTrace(populated_trace)
        records = trace.step_records
        assert len(records) == 2
        assert records[0]["step"] == 0
        assert records[1]["step"] == 1

    def test_get_step_record(self, populated_trace):
        trace = RunTrace(populated_trace)
        record = trace.get_step_record(0)
        assert record is not None
        assert record["step"] == 0
        assert record["llm_output"] == "click(btn)"

    def test_get_step_record_missing(self, populated_trace):
        trace = RunTrace(populated_trace)
        assert trace.get_step_record(99) is None

    def test_get_observation_json(self, populated_trace):
        trace = RunTrace(populated_trace)
        obs = trace.get_observation(0, "dom")
        assert obs is not None
        assert obs["element_tree"] == "<div>step0</div>"

    def test_get_observation_binary(self, populated_trace):
        trace = RunTrace(populated_trace)
        data = trace.get_observation(1, "screenshot")
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
        assert shots[0].name == "001_screenshot.png"

    def test_lazy_loading_does_not_read_steps_eagerly(self, populated_trace):
        """Accessing only result should NOT trigger step file reads."""
        trace = RunTrace(populated_trace)

        with patch("spectragent.core.trace.reader.json.loads", wraps=json.loads) as mock_loads:
            # Access result only
            _ = trace.result
            # json.loads should be called for result.json only (not step files)
            call_count = mock_loads.call_count
            assert call_count == 1  # Only result.json
