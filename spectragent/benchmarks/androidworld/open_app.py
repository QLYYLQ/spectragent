"""AndroidWorld's ``open_app`` name table, read from beside its task lists.

``docs/architecture_design.md`` §11.2 "``open_app`` 的名称表", §4.10.1. The
table is computed from the pinned AndroidWorld code, never copied by hand:
``androidworld_service/task_list.py``, run under AndroidWorld's own
interpreter (its pinned dependencies would clash with Spectragent's), reads
``adb_utils._PATTERN_TO_ACTIVITY`` off the checkout and writes every name of
every pattern with the component AndroidWorld starts for it, checked against
AndroidWorld's own matcher, into ``open_app.json`` beside the task lists it
writes. That is the table cached per pinned commit: the file records the
commit it was read from, and a table from any other commit is refused.

The AndroidWorld benchmark hands it to the Android capability as its part of
the capability configuration (``AndroidWorldBenchmark.computed_capability``):
``apps`` of the ``android.open_app`` entry, so ``open_app`` opens the activity
AndroidWorld opens. The part sets options on that entry and adds none: a run
configuration whose actions leave ``android.open_app`` out has no use for the
table, and the merge leaves it unused (user ruling 2026-11-04).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.core.data_loader._common import BenchmarkDataError

#: The file beside the task lists that holds the table.
OPEN_APP_FILE = "open_app.json"
#: The ``format`` the file declares.
OPEN_APP_FORMAT = "androidworld-open-app-1"


class OpenAppTable(BaseModel):
    """AndroidWorld's app names, as ``task_list.py`` writes them.

    Attributes:
        format: :data:`OPEN_APP_FORMAT`.
        commit: The AndroidWorld commit the table was read from.
        apps: Each name → the component (``package/activity``) AndroidWorld
            starts for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format: Literal["androidworld-open-app-1"]
    commit: str
    apps: dict[str, str] = Field(min_length=1)


def read_open_app_table(data_dir: Path) -> dict[str, str]:
    """The table in *data_dir*, read from the pinned commit: name → component.

    Raises:
        BenchmarkDataError: There is none, it cannot be read, it is not the
            table, or it was read from another commit; the message says how
            to write it again.
    """
    path = data_dir / OPEN_APP_FILE
    rewrite = (
        "task_list.py writes it beside the task lists (spectragent/benchmarks/androidworld/"
        "androidworld_service/); run it again with the pinned checkout"
    )
    try:
        table = OpenAppTable.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        raise BenchmarkDataError(
            f"{data_dir} has no AndroidWorld open_app table ({OPEN_APP_FILE}): {rewrite}"
        ) from None
    except OSError as exc:
        raise BenchmarkDataError(
            f"cannot read the AndroidWorld open_app table {path}: {exc}"
        ) from exc
    except ValidationError as exc:
        raise BenchmarkDataError(
            f"{path} is not an AndroidWorld open_app table (format {OPEN_APP_FORMAT}): {exc}; "
            f"{rewrite}"
        ) from None
    if table.commit != PINNED_COMMIT:
        raise BenchmarkDataError(
            f"{path} was read from AndroidWorld {table.commit}, and the benchmark runs "
            f"{PINNED_COMMIT}: {rewrite}"
        )
    return dict(table.apps)


__all__ = ["OPEN_APP_FILE", "OPEN_APP_FORMAT", "OpenAppTable", "read_open_app_table"]
