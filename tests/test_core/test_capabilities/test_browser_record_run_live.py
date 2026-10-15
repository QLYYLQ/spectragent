"""The shipped recording config, run end to end: the data flywheel's first deliverable.

``configs/flywheel/record_ui_tars_pinned.yaml`` composed by the real Composer
and run by the real runtime against the local fixture site, with honest
overrides only: the LLM is a loopback scripted endpoint
(``tests/_helpers/scripted_llm.py`` — no model spend), loopback traffic
bypasses the machine's proxy (``NO_PROXY``), the fixture's host-resolver
switches are added to the backend's ``extra_args``, and the trace lands in a
temporary directory. Everything else is the shipped file.

The run's ``env/`` must hold all four kinds of environment evidence — the
network archive (WARC + index), the storage snapshot, the raw DOM of each
committed cycle, and the environment fingerprint — and the trace must name
the agent, cleaner, serializer, backend and model route it ran with.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

RECORD_CONFIG = (
    Path(__file__).resolve().parents[3] / "configs" / "flywheel" / "record_ui_tars_pinned.yaml"
)

#: What the scripted model answers, step by step (UI-TARS text protocol).
SCRIPT = (
    "Thought: The page is loading; wait for it.\nAction: wait()",
    "Thought: The frame shows the inner page. Nothing else to do.\n"
    "Action: finished(content='recorded')",
)


def _overrides(
    raw: dict[str, Any], *, site: FixtureSite, llm: ScriptedLLMServer, trace_dir: Path
) -> None:
    agent = raw["agent"][0]
    agent["llm"].update(
        {"model": llm.model, "base_url": llm.base_url, "api_key": "scripted", "max_retries": 0}
    )
    backend = raw["environment"][0]["capabilities"][0]["backend"]
    backend["extra_args"] = site.chrome_args()
    (writer,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
    writer["trace_dir"] = str(trace_dir)


def test_record_config_produces_full_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    raw = yaml.safe_load(RECORD_CONFIG.read_text("utf-8"))

    with FixtureSite(mapped=True) as site, ScriptedLLMServer(SCRIPT) as llm:
        _overrides(raw, site=site, llm=llm, trace_dir=tmp_path)
        run = ConfigComposer(registrars=collect_registrars()).compose(raw)
        (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]

        async def execute() -> Any:
            return await AgentRuntime.for_agent(run, "main").execute(
                "Record the fixture page.",
                task_id="record--0001",
                benchmark="flywheel",
                subset="fixture",
                timeout=90,
                env_context={"start_url": site.url("oopif")},
            )

        result = asyncio.run(execute())
        served = {r.bare_path for r in site.requests}

    assert result.answer == "recorded", result
    assert len(llm.requests) == 2, "the scripted model was asked once per step"
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert trace.manifest.status.value == "complete"

    env = writer.run_dir / "env"
    files = sorted(str(p.relative_to(env)) for p in env.rglob("*") if p.is_file())
    assert any(f.endswith("browser.network/exchanges.warc.gz") for f in files), files
    assert any(f.endswith("browser.network/index.jsonl") for f in files), files
    assert any(f.endswith("browser.storage/storage.json") for f in files), files
    assert any(f.startswith("blobs/") for f in files), "the raw DOM captures (content-addressed)"
    assert "run.browser.json" in files

    records = [
        record
        for step in range(trace.step_count)
        for record in trace.archive_files(step, "browser.dom_raw")
    ]
    assert records, "a dom_raw capture per committed cycle"
    document = json.loads(gzip.decompress(trace.blob(records[0].sha256)))
    assert (document["source"], document["cleaner"]) == ("archive", "self_capture")

    lines = [
        json.loads(line)
        for step in range(trace.step_count)
        for record in trace.archive_files(step, "browser.network")
        if record.name == "index.jsonl"
        for line in trace.read_extra(record.path).decode().splitlines()
    ]
    archived = {
        line["url"].split("://", 1)[1].split("/", 1)[1] for line in lines if line.get("url")
    }
    assert {"oopif/page.html", "oopif/frame.html", "oopif/frame.js"} <= archived
    assert {"/oopif/page.html", "/oopif/frame.html"} <= served

    fingerprint = trace.archive_run_file("browser")
    assert fingerprint is not None
    facts = json.loads(trace.read_extra(fingerprint.path))
    assert facts["replayability"]["tier"] == "pinned"
    assert facts["replayability"]["params"]["random_seed"] == 7

    # What ran, read from the trace (the report names these).
    resolved = trace.config.resolved
    agent = resolved["agent"][0]
    capability = resolved["environment"][0]["capabilities"][0]
    assert agent["agent_id"] == "ui_tars"
    assert capability["dom_cleaner"]["cleaner_type"] == "browser_use"
    assert capability["dom_serializer"]["serializer_type"] == "text"
    assert capability["backend"]["provider_id"] == "local_chrome"
    assert (agent["llm"]["model_type"], agent["llm"]["provider"]["name"]) == ("openai", "direct")
    # The model name is not trace-safe in the redacted config; the identity
    # fields carry it.
    assert trace.config.llm_model == "scripted-fixture"
    assert (trace.config.agent_id, trace.config.loop_id) == ("ui_tars", "react")
    assert trace.config.env_evidence_privacy is not None  # archivers were bound
    print(
        "\nrecorded run:",
        json.dumps(
            {
                "run_dir": str(writer.run_dir),
                "agent": agent["agent_id"],
                "cleaner": capability["dom_cleaner"]["cleaner_type"],
                "serializer": capability["dom_serializer"]["serializer_type"],
                "backend": capability["backend"]["provider_id"],
                "model_route": f"{agent['llm']['model_type']}/{agent['llm']['provider']['name']}"
                f" model={trace.config.llm_model}",
                "steps": trace.step_count,
                "env_files": len(files),
                "chrome": facts["chrome"]["product"],
            }
        ),
    )
