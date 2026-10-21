"""The local HTTP viewer preserves the repository's evidence boundaries."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import RepositoryError
from spectragent.schemas.config_source import ConfigSource
from spectragent.viewer import create_app
from tests._helpers.trace_builder import PNG_BYTES, agent_result, build_reference_trace, eval_result


@pytest.fixture
def client(tmp_path: Path):
    build_reference_trace(tmp_path)
    return TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")


def test_workspace_without_initial_focus_keeps_the_root_scope(client, tmp_path: Path):
    assert client.get("/api/workspace").json() == {
        "root": str(tmp_path.resolve()),
        "initial_run": None,
        "config_source": None,
        "config_hash": None,
        "annotations": None,
    }


def test_initial_run_focus_preserves_other_runs_in_the_workspace(tmp_path: Path):
    build_reference_trace(tmp_path / "nested", "one")
    build_reference_trace(tmp_path, "two")
    client = TestClient(
        create_app(tmp_path, initial_run="nested/one"), base_url="http://127.0.0.1:8765"
    )
    assert client.get("/api/workspace").json()["initial_run"] == "nested/one"
    assert {row["key"] for row in client.get("/api/runs").json()} == {"nested/one", "two"}
    assert client.get("/api/run", params={"key": "two"}).status_code == 200


def test_initial_run_is_validated_before_server_startup(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="No recorded run"):
        create_app(tmp_path, initial_run="missing")
    with pytest.raises(RepositoryError, match="relative path"):
        create_app(tmp_path, initial_run="../outside")


def test_single_run_root_can_focus_its_dot_key(tmp_path: Path):
    fixture = build_reference_trace(tmp_path)
    client = TestClient(
        create_app(fixture.run_dir, initial_run="."), base_url="http://127.0.0.1:8765"
    )
    assert client.get("/api/workspace").json()["initial_run"] == "."
    assert client.get("/api/run", params={"key": "."}).status_code == 200


def test_configuration_file_history_is_a_filter_not_a_new_root(tmp_path: Path):
    source = (tmp_path / "configs" / "experiment.yaml").resolve()
    hashes = {}
    for name, version in [("earlier", "a"), ("later", "b"), ("unknown", None)]:
        config = TraceConfig(
            config_source=ConfigSource(path=str(source), sha256=version * 64) if version else None,
            resolved={"variant": name},
        )
        writer = WALWriter(tmp_path, name)
        writer.init_run(config)
        writer.write_result(agent_result())
        writer.finalize(TraceStatus.COMPLETE)
        hashes[name] = config.configuration_digest()
    assert not source.exists()
    client = TestClient(
        create_app(tmp_path, initial_run="later", config_source=source),
        base_url="http://127.0.0.1:8765",
    )
    workspace = client.get("/api/workspace").json()
    assert workspace["root"] == str(tmp_path.resolve())
    assert workspace["config_source"] == str(source.resolve())
    assert {row["key"] for row in client.get("/api/runs").json()} == {"earlier", "later"}
    assert client.get("/api/run", params={"key": "unknown"}).status_code == 200
    with pytest.raises(RepositoryError, match="does not match"):
        create_app(tmp_path, initial_run="unknown", config_source=source)
    exact = TestClient(
        create_app(tmp_path, config_source=source, config_hash=hashes["earlier"]),
        base_url="http://127.0.0.1:8765",
    )
    assert [row["key"] for row in exact.get("/api/runs").json()] == ["earlier"]
    assert exact.get("/api/workspace").json()["config_hash"] == hashes["earlier"]
    with pytest.raises(RepositoryError, match="does not match"):
        create_app(tmp_path, initial_run="later", config_hash=hashes["earlier"])


def test_config_source_without_history_does_not_require_the_yaml_file(tmp_path: Path):
    build_reference_trace(tmp_path)
    source = tmp_path / "missing.yaml"
    client = TestClient(
        create_app(tmp_path, config_source=source), base_url="http://127.0.0.1:8765"
    )
    assert client.get("/api/runs").json() == []
    assert client.get("/api/workspace").json()["config_source"] == str(source.resolve())


def test_run_and_step_endpoints_preserve_independent_outcomes_and_calls(client):
    rows = client.get("/api/runs").json()
    assert len(rows) == 1
    assert rows[0]["agent_done"] == "timeout"
    assert rows[0]["trace_status"] == "complete"
    key = rows[0]["key"]
    run = client.get("/api/run", params={"key": key})
    assert run.status_code == 200
    assert run.json()["trailing_calls"][0]["status"] == "cancelled"
    step = client.get("/api/step", params={"key": key, "index": 0}).json()
    assert [call["status"] for call in step["calls"]] == ["failed", "completed"]
    assert step["output_schemas"]
    assert step["fragments"]["browser.browser_meta"]["error"] == "TimeoutError"
    assert client.get("/api/runs", params={"search": "does-not-exist"}).json() == []


def test_committed_screenshot_has_raster_content_type(client):
    response = client.get("/api/screenshot", params={"key": "run-test", "index": 0})
    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("endpoint", ["run", "step", "screenshot", "report"])
@pytest.mark.parametrize("key", ["../outside", "/etc/passwd", "a/../../outside", "..\\outside"])
def test_file_keys_cannot_escape_repository(client, endpoint: str, key: str):
    response = client.get(f"/api/{endpoint}", params={"key": key, "index": 0})
    assert response.status_code == 400
    assert "relative repository key" in response.json()["detail"]


def test_missing_record_and_invalid_step_are_explicit(client):
    assert client.get("/api/run", params={"key": "missing"}).status_code == 404
    assert client.get("/api/step", params={"key": "run-test", "index": 400}).status_code == 404
    assert client.get("/api/step", params={"key": "run-test", "index": -1}).status_code == 422
    assert client.get("/api/step", params={"key": "run-test", "index": "no"}).status_code == 422


def test_corruption_is_json_error_and_does_not_hide_other_runs(tmp_path: Path):
    broken = build_reference_trace(tmp_path, "broken")
    build_reference_trace(tmp_path, "healthy")
    (broken.run_dir / "steps" / "000.json").write_text("{")
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")
    rows = {row["key"]: row for row in client.get("/api/runs").json()}
    assert rows["broken"]["read_error"]
    assert rows["healthy"]["trace_status"] == "complete"
    response = client.get("/api/run", params={"key": "broken"})
    assert response.status_code == 400
    assert "000.json" in response.json()["detail"]


def test_read_only_routes_never_expose_arbitrary_files(tmp_path: Path):
    build_reference_trace(tmp_path)
    (tmp_path / "private.json").write_text('{"secret":"private"}')
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")
    assert client.post("/api/run", json={"key": "run-test"}).status_code == 405
    assert client.delete("/api/run", params={"key": "run-test"}).status_code == 405
    for url in [
        "/private.json",
        "/api/file?path=private.json",
        "/run-test/config.json",
        "/api/report?key=private.json",
    ]:
        assert client.get(url).status_code == 404


def test_reports_use_shared_report_reader(tmp_path: Path):
    data = {"format": "af-independent-evaluation-1", "coverage": 0.5, "success_rate": None}
    report = tmp_path / "eval" / "report.json"
    report.parent.mkdir()
    report.write_text(json.dumps(data))
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")
    assert client.get("/api/reports").json()[0]["key"] == "eval/report.json"
    assert client.get("/api/report", params={"key": "eval/report.json"}).json()["data"] == data


def test_comparison_reads_multiple_runs_and_bounds_request(tmp_path: Path):
    build_reference_trace(tmp_path, "one")
    build_reference_trace(tmp_path, "two")
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")
    comparison = client.get("/api/compare", params=[("key", "one"), ("key", "two")])
    assert comparison.status_code == 200
    assert len(comparison.json()["runs"]) == 2
    assert client.get("/api/compare", params={"key": "one"}).status_code == 422
    assert client.get("/api/compare", params=[("key", "one")] * 9).status_code == 422


def test_experiment_comparison_exposes_matched_verdicts(tmp_path: Path):
    hashes = []
    for name, verdict in [("first", 1), ("second", 0)]:
        config = TraceConfig(task_id="shared-task", resolved={"variant": name})
        hashes.append(config.configuration_digest())
        writer = WALWriter(tmp_path, name)
        writer.init_run(config)
        writer.write_result(agent_result())
        writer.finalize(TraceStatus.COMPLETE)
        (writer.run_dir / "evals" / "judge.json").write_text(
            eval_result(predicted_label=verdict).model_dump_json()
        )
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765")
    response = client.get(
        "/api/compare-groups",
        params={"left": hashes[0], "right": hashes[1], "evaluator": "judge"},
    )
    assert response.status_code == 200
    assert response.json()["counts"]["left_only_passed"] == 1
    assert response.json()["rows"][0]["task_id"] == "shared-task"
    assert (
        client.get(
            "/api/compare-groups",
            params={"left": hashes[0], "right": hashes[0], "evaluator": "judge"},
        ).status_code
        == 400
    )


def test_static_app_is_packaged_and_uses_only_local_scripts(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "All4One" in response.text
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
    assert scripts and all(script.startswith("/assets/") for script in scripts)
    for script in scripts:
        assert client.get(script).status_code == 200
    brand = client.get("/brand/mark.svg")
    assert brand.status_code == 200
    assert brand.headers["content-type"].startswith("image/svg+xml")
    assert b"Spectragent prism mark" in brand.content
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/openapi.json").status_code == 404


def test_serve_defaults_to_loopback_without_opening_browser(tmp_path: Path, monkeypatch, capsys):
    import socket

    from spectragent.viewer import server

    captured = {}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setattr(server.uvicorn.Config, "bind_socket", lambda self: listener)
    monkeypatch.setattr(
        server.uvicorn.Server,
        "run",
        lambda self, **kwargs: captured.update(config=self.config, **kwargs),
    )
    monkeypatch.setattr(
        server.webbrowser, "open", lambda *args: pytest.fail("unexpected browser open")
    )
    server.serve(tmp_path)
    assert captured["config"].host == "127.0.0.1"
    assert captured["config"].port == 8765
    assert captured["config"].timeout_graceful_shutdown == 2
    assert captured["sockets"] == [listener]
    assert listener.fileno() == -1
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out


def test_public_serve_forwards_initial_run_to_the_app(tmp_path: Path, monkeypatch):
    from spectragent.viewer import serve, server

    build_reference_trace(tmp_path)
    apps = []
    monkeypatch.setattr(
        server.uvicorn.Server, "run", lambda self, **kwargs: apps.append(self.config.app)
    )
    serve(tmp_path, port=0, initial_run="run-test")
    assert (
        TestClient(apps[0], base_url="http://127.0.0.1:8765")
        .get("/api/workspace")
        .json()["initial_run"]
        == "run-test"
    )
