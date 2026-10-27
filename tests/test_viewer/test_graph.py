"""Graph reads retain the viewer's root boundary and committed-step failures."""

from pathlib import Path

from fastapi.testclient import TestClient

from spectragent.viewer import create_app
from tests._helpers.trace_builder import build_reference_trace


def test_graph_is_a_small_read_even_when_source_is_outside_configuration_filter(
    tmp_path: Path,
) -> None:
    build_reference_trace(tmp_path, "source")
    client = TestClient(create_app(tmp_path, config_hash="other"), base_url="http://127.0.0.1")
    assert client.get("/api/runs").json() == []
    response = client.get("/api/graph", params={"key": "./source/"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["steps"]
    assert (
        "calls" not in payload
        and "config" not in payload
        and "llm_output" not in payload["steps"][0]
    )
    assert client.get("/api/graph", params={"key": "../outside"}).status_code == 400
    assert client.get("/api/graph", params={"key": "absent"}).status_code == 404


def test_graph_reports_a_broken_committed_step_instead_of_serving_an_empty_success(
    tmp_path: Path,
) -> None:
    fixture = build_reference_trace(tmp_path, "broken")
    (fixture.run_dir / "steps" / "000.json").write_text("{")
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    response = client.get("/api/graph", params={"key": "broken"})
    assert response.status_code == 400
    assert "000.json" in response.json()["detail"]
