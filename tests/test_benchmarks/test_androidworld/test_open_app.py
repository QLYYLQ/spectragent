"""AndroidWorld's ``open_app`` name table, read from beside the task lists (``docs/architecture_design.md`` §11.2).

``task_list.py`` writes it under AndroidWorld's interpreter from the pinned
checkout (``test_androidworld_live.py`` runs the writer); the benchmark reads
it and refuses one it cannot use, saying how to write it again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent import PACKAGE_ROOT
from spectragent.benchmarks.androidworld.open_app import (
    OPEN_APP_FILE,
    OPEN_APP_FORMAT,
    read_open_app_table,
)
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.core.data_loader._common import BenchmarkDataError

WRITER = PACKAGE_ROOT / "benchmarks/androidworld/androidworld_service/task_list.py"
APPS = {"clock": "com.google.android.deskclock/com.android.deskclock.DeskClock"}


def _written(directory: Path, table: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / OPEN_APP_FILE).write_text(json.dumps(table))
    return directory


def test_the_writer_and_the_reader_name_one_format() -> None:
    text = WRITER.read_text()
    assert f'OPEN_APP_FORMAT = "{OPEN_APP_FORMAT}"' in text
    assert f'(out / "{OPEN_APP_FILE}").write_text(' in text


def test_a_table_of_the_pinned_commit_is_read(tmp_path: Path) -> None:
    table = {"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT, "apps": APPS}
    assert read_open_app_table(_written(tmp_path, table)) == APPS


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"format": "androidworld-open-app-0", "commit": PINNED_COMMIT, "apps": APPS}, "not an"),
        ({"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT, "apps": {}}, "not an"),
        ({"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT}, "not an"),
        (
            {"format": OPEN_APP_FORMAT, "commit": "f" * 40, "apps": APPS},
            "read from AndroidWorld ffff",
        ),
    ],
    ids=["another format", "no names", "no apps", "another commit"],
)
def test_a_table_it_cannot_use_is_refused_saying_how_to_write_it(
    tmp_path: Path, table: object, message: str
) -> None:
    with pytest.raises(BenchmarkDataError, match=message) as refused:
        read_open_app_table(_written(tmp_path, table))
    assert "task_list.py writes it beside the task lists" in str(refused.value)


def test_no_table_is_refused_saying_how_to_write_it(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkDataError, match="has no AndroidWorld open_app table"):
        read_open_app_table(tmp_path)
