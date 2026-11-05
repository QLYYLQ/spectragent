"""AndroidWorld's ``open_app`` name table, read from beside the task lists (``docs/architecture_design.md`` §11.2).

``task_list.py`` writes it under AndroidWorld's interpreter from the pinned
checkout (``test_androidworld_live.py`` runs the writer on the real one; here
it runs on a checkout of a few lines); the benchmark reads it and refuses one
it cannot use, saying how to write it again.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
from tests._helpers.benchmark_data import ANDROIDWORLD_DEFAULT_APP_URIS, open_app_table

WRITER = PACKAGE_ROOT / "benchmarks/androidworld/androidworld_service/task_list.py"
APPS = {"clock": "com.google.android.deskclock/com.android.deskclock.DeskClock"}
#: An AndroidWorld checkout's ``adb_utils``, cut to what the writer reads: its
#: table, its default-app names and its matcher, as at the pinned commit
#: (``android_world/env/adb_utils.py``, lines 35-169, 180-186 and 589-592).
ADB_UTILS = """
import re

_PATTERN_TO_ACTIVITY = {
    'clock': 'com.google.android.deskclock/com.android.deskclock.DeskClock',
    'contacts': (
        'com.google.android.contacts/com.android.contacts.activities.PeopleActivity'
    ),
}

# Special app names that will trigger opening the default app.
_DEFAULT_URIS: dict[str, str] = {
    'calendar': 'content://com.android.calendar',
    'browser': 'http://',
    'contacts': 'content://contacts/people/',
    'email': 'mailto:',
    'gallery': 'content://media/external/images/media/',
}


def get_adb_activity(app_name):
  for pattern, activity in _PATTERN_TO_ACTIVITY.items():
    if re.match(pattern.lower(), app_name.lower()):
      return activity
"""


def _written(directory: Path, table: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / OPEN_APP_FILE).write_text(json.dumps(table))
    return directory


def test_the_writer_and_the_reader_name_one_format() -> None:
    text = WRITER.read_text()
    assert f'OPEN_APP_FORMAT = "{OPEN_APP_FORMAT}"' in text
    assert f'(out / "{OPEN_APP_FILE}").write_text(' in text
    assert '"default_app_uris": uris' in text


def test_the_writer_reads_the_table_and_the_default_app_names_off_the_checkout(
    tmp_path: Path,
) -> None:
    env = tmp_path / "src" / "android_world" / "env"
    env.mkdir(parents=True)
    (env.parent / "__init__.py").write_text("")
    (env / "__init__.py").write_text("")
    (env / "adb_utils.py").write_text(ADB_UTILS)
    read = (
        "import json, runpy, sys; sys.path.insert(0, sys.argv[1]); "
        "writer = runpy.run_path(sys.argv[2]); "
        "print(json.dumps([writer['_open_app_table'](), writer['_default_app_uris']()]))"
    )
    ran = subprocess.run(
        [sys.executable, "-I", "-c", read, str(tmp_path / "src"), str(WRITER)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    apps, uris = json.loads(ran.stdout)

    assert uris == ANDROIDWORLD_DEFAULT_APP_URIS
    # Both hold contacts: AndroidWorld opens the default app for it written so,
    # and its table's activity for Contacts.
    assert apps == {
        "clock": "com.google.android.deskclock/com.android.deskclock.DeskClock",
        "contacts": "com.google.android.contacts/com.android.contacts.activities.PeopleActivity",
    }


def test_a_table_of_the_pinned_commit_is_read(tmp_path: Path) -> None:
    table = read_open_app_table(_written(tmp_path, open_app_table(PINNED_COMMIT, APPS)))

    assert table.apps == APPS
    assert table.default_app_uris == ANDROIDWORLD_DEFAULT_APP_URIS
    assert table.action_entry() == {
        "action_id": "android.open_app",
        "apps": APPS,
        "default_app_uris": ANDROIDWORLD_DEFAULT_APP_URIS,
    }


@pytest.mark.parametrize(
    ("table", "message"),
    [
        (
            {"format": "androidworld-open-app-1", "commit": PINNED_COMMIT, "apps": APPS},
            "not an AndroidWorld open_app table of format androidworld-open-app-2",
        ),
        ({**open_app_table(PINNED_COMMIT, APPS), "format": "androidworld-open-app-0"}, "not an"),
        (open_app_table(PINNED_COMMIT, {}), "not an"),
        (open_app_table(PINNED_COMMIT, APPS, {}), "not an"),
        ({"format": OPEN_APP_FORMAT, "commit": PINNED_COMMIT}, "not an"),
        (open_app_table("f" * 40, APPS), "read from AndroidWorld ffff"),
    ],
    ids=[
        "the earlier format",
        "another format",
        "no names",
        "no default-app names",
        "no apps",
        "another commit",
    ],
)
def test_a_table_it_cannot_use_is_refused_saying_how_to_write_it(
    tmp_path: Path, table: object, message: str
) -> None:
    with pytest.raises(BenchmarkDataError, match=message) as refused:
        read_open_app_table(_written(tmp_path, table))
    assert "task_list.py writes it beside the task lists; fetch them again with spectragent" in (
        str(refused.value)
    )


def test_no_table_is_refused_saying_how_to_write_it(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkDataError, match="has no AndroidWorld open_app table"):
        read_open_app_table(tmp_path)
