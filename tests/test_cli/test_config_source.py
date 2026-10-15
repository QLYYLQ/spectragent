"""CLI provenance comes from consumed bytes, with no dependency on later file edits."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from spectragent.core.composer import ComposedRun
from spectragent.schemas.config_source import ConfigSource
from tests._helpers.trace_builder import agent_result


async def test_single_run_passes_the_source_version_it_parsed(tmp_path, monkeypatch):
    from spectragent.core import run_cli

    path = tmp_path / "run.yaml"
    original = b"task_label: consumed\n"
    path.write_bytes(original)
    captured = {}

    class Runtime:
        agent = SimpleNamespace(
            agent_id="fixture",
            agent_loop=SimpleNamespace(loop_id="react"),
            llm=SimpleNamespace(model="fixture"),
        )
        env = None

        async def execute(self, **kwargs):
            captured.update(kwargs)
            return agent_result()

    def compose(raw):
        assert raw["task_label"] == "consumed"
        path.write_bytes(b"task_label: changed\n")
        return SimpleNamespace(ids=lambda layer: ["fixture"], instances=lambda layer: [], hooks=[])

    monkeypatch.setattr(
        run_cli, "ConfigComposer", lambda **kwargs: SimpleNamespace(compose=compose)
    )
    monkeypatch.setattr(run_cli.AgentRuntime, "for_agent", lambda *args: Runtime())
    assert (
        await run_cli._run(config_path=path, task="read", start_url=None, timeout=1, dry_run=False)
        == 0
    )
    assert captured["config_source"] == ConfigSource.from_path(path, data=original)


def _stub_composer(monkeypatch, path, *, change_source=False):
    from spectragent.core import benchmark_runner

    def compose(raw):
        if change_source:
            assert raw == {"version": "consumed"}
            path.write_bytes(b"version: changed\n")
        return ComposedRun(by_layer={})

    loader = SimpleNamespace(benchmark_name="fixture", subset="all", task_ids=lambda: [])
    monkeypatch.setattr(
        benchmark_runner, "build_composer", lambda: SimpleNamespace(compose=compose)
    )
    monkeypatch.setattr(benchmark_runner, "_resolve_loader", lambda *args: loader)


def test_batch_session_uses_the_same_source_version_as_the_parsed_configuration(
    tmp_path, monkeypatch
):
    from spectragent.core.benchmark_runner import main

    path = tmp_path / "batch.yaml"
    consumed = b"version: consumed\n"
    path.write_bytes(consumed)
    _stub_composer(monkeypatch, path, change_source=True)
    output = {}
    assert (
        main(
            ["--config", str(path), "--session-dir", str(tmp_path / ".sessions")],
            report=output.update,
        )
        == 0
    )
    session = json.loads(next((tmp_path / ".sessions").glob("*.json")).read_text())
    assert session["config_source"] == {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(consumed).hexdigest(),
    }


def test_generated_config_uses_captured_original_source_without_rereading_it(tmp_path, monkeypatch):
    from spectragent.core.benchmark_runner import main

    generated = tmp_path / "generated.yaml"
    generated.write_bytes(b"{}\n")
    missing_original = tmp_path / "original.yaml"
    source = ConfigSource.from_path(missing_original, data=b"version: original\n")
    _stub_composer(monkeypatch, generated)
    assert (
        main(
            [
                "--config",
                str(generated),
                "--session-dir",
                str(tmp_path / ".sessions"),
                "--config-source",
                source.path,
                "--config-source-sha256",
                source.sha256,
            ]
        )
        == 0
    )
    session = json.loads(next((tmp_path / ".sessions").glob("*.json")).read_text())
    assert session["config_source"] == source.model_dump()
    assert not missing_original.exists()


@pytest.mark.parametrize(
    "args", [["--config-source", "/original.yaml"], ["--config-source-sha256", "0" * 64]]
)
def test_original_source_flags_are_a_required_pair(args):
    from spectragent.core.benchmark_runner import main

    with pytest.raises(SystemExit) as caught:
        main(["--config", "/not-read.yaml", *args])
    assert caught.value.code == 2
