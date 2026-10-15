"""All browsing entry points share directory, source history and initial-run selection."""

import json
import sys
from pathlib import Path

from spectragent.cli import main
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.config_source import ConfigSource


def test_runs_list_filters_source_without_reading_current_yaml(tmp_path: Path, capsys) -> None:
    config = tmp_path / "deleted.yaml"
    for name, source in [
        ("selected", ConfigSource.from_path(config, data=b"old")),
        ("unknown", None),
    ]:
        WALWriter(tmp_path / "runs", name).init_run(TraceConfig(config_source=source))
    assert (
        main(["--json", "runs", "list", "--root", str(tmp_path / "runs"), "--config", str(config)])
        == 0
    )
    rows = json.loads(capsys.readouterr().out)["runs"]
    assert [row["key"] for row in rows] == ["selected"]


def test_view_passes_scope_without_reinterpreting_config(tmp_path: Path, monkeypatch) -> None:
    from spectragent.viewer import server

    calls = []
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert (
        main(
            [
                "view",
                "--root",
                str(tmp_path),
                "--config",
                "missing.yaml",
                "--run",
                "nested/run",
                "--config-hash",
                "abc",
            ]
        )
        == 0
    )
    assert calls[0][0] == (tmp_path,)
    assert calls[0][1]["config_source"] == Path("missing.yaml")
    assert calls[0][1]["initial_run"] == "nested/run"
    assert calls[0][1]["config_hash"] == "abc"


def test_tui_passes_the_same_scope(tmp_path: Path, monkeypatch) -> None:
    from spectragent import tui

    calls = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(tui, "browse", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert (
        main(
            [
                "tui",
                "--root",
                str(tmp_path),
                "--config",
                "missing.yaml",
                "--run",
                "nested/run",
                "--config-hash",
                "abc",
            ]
        )
        == 0
    )
    assert calls == [
        (
            (tmp_path,),
            {
                "initial_run": "nested/run",
                "config_source": Path("missing.yaml"),
                "config_hash": "abc",
            },
        )
    ]
