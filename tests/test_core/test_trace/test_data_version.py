"""Every run records the version of the data its task was read from (``docs/architecture_design.md`` §4.10.1 "数据", §5.3).

The task loader reads the version beside its data (``data_version.json``,
written by ``spectragent benchmark fetch``); the runtime hands it to the trace
with the benchmark and subset, and it moves ``config_hash``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spectragent.core.data_loader._common import BenchmarkDataError, write_data_version
from spectragent.core.data_loader.task.browsecomp import BrowseCompTaskLoader
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.data_version import DataVersion
from tests._helpers.assembly import AGENT_ID, make_env, make_run
from tests.test_core.test_trace.test_run_configuration import _Agent, _Cap, _Env, _Memory, _SlowLLM

_VERSION = DataVersion(source="huggingface", repository="org/set", revision="a" * 40)


def _data(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "data_info.json").write_text(json.dumps({"split": {"All": "tasks.jsonl"}}))
    (directory / "tasks.jsonl").write_text(json.dumps({"task_id": "t1", "task_text": "x"}) + "\n")
    return directory


def test_the_loader_reads_the_version_beside_its_data(tmp_path: Path) -> None:
    directory = _data(tmp_path / "data")
    assert BrowseCompTaskLoader(data_dir=str(directory)).data_version is None
    write_data_version(directory, _VERSION)
    assert BrowseCompTaskLoader(data_dir=str(directory)).data_version == _VERSION


def test_a_record_that_is_not_one_is_refused(tmp_path: Path) -> None:
    directory = _data(tmp_path / "data")
    (directory / "data_version.json").write_text('{"source": "ftp"}')
    with pytest.raises(BenchmarkDataError, match="data_version.json is not a data version record"):
        BrowseCompTaskLoader(data_dir=str(directory))


def test_the_version_moves_the_hash_and_is_written_only_when_there_is_one() -> None:
    plain = TraceConfig(benchmark="b", subset="All")
    versioned = TraceConfig(benchmark="b", subset="All", data_version=_VERSION)
    other = TraceConfig(
        benchmark="b", subset="All", data_version=_VERSION.model_copy(update={"revision": "b" * 40})
    )
    digests = {config.configuration_digest(resolved={}) for config in (plain, versioned, other)}
    assert len(digests) == 3
    assert "data_version" not in plain.model_dump(mode="json")
    assert versioned.model_dump(mode="json")["data_version"] == _VERSION.model_dump(mode="json")


async def test_a_run_records_its_benchmarks_data_version(tmp_path: Path) -> None:
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
    benchmark = SimpleNamespace(
        setup=None,
        live_verifiers=lambda: [],
        task_loader=SimpleNamespace(data_version=_VERSION),
    )
    run = make_run(
        agent=_Agent(),
        loop=ReactLoop(max_steps=1),
        env=make_env(_Env, {"runcfg_cap": _Cap()}),
        llm=_SlowLLM(),
        memory=_Memory(),
        hooks=[writer],
        benchmark=benchmark,
    )
    await AgentRuntime.for_agent(run, AGENT_ID).execute(
        "t", task_id="t1", benchmark="b", subset="All", timeout=10
    )
    assert writer.run_dir is not None
    assert RunTrace(writer.run_dir).config.data_version == _VERSION
