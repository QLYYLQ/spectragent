"""AndroidWorld's ``open_app`` name table, read from beside its task lists.

``docs/architecture_design.md`` §11.2 "``open_app`` 的名称表", §4.10.1. The
table is computed from the pinned AndroidWorld code, never copied by hand:
``androidworld_service/task_list.py``, run under AndroidWorld's own
interpreter (its pinned dependencies would clash with Spectragent's), reads
``adb_utils._PATTERN_TO_ACTIVITY`` off the checkout and writes every name of
every pattern with the component AndroidWorld starts for it, checked against
AndroidWorld's own matcher, and every name of ``adb_utils._DEFAULT_URIS`` with
the data URI whose default app AndroidWorld opens for it (its ``launch_app``
checks those names first, only as written), into ``open_app.json`` beside the
task lists it writes. That is the table cached per pinned commit: the file
records the commit it was read from, and a table from any other commit, or of
an earlier format, is refused.
VenusBench's fork keeps AndroidWorld's table, and its fetch writes it the
same way from its own checkout (:class:`~spectragent.benchmarks.androidworld.service.PinnedCode`
names the commit to check).

The AndroidWorld benchmark hands it to the Android capability as its part of
the capability configuration (``AndroidWorldBenchmark.computed_capability``):
``apps`` and ``default_app_uris`` of the ``android.open_app`` entry, so
``open_app`` opens what AndroidWorld opens. The part sets options on that entry and adds none: a run
configuration whose actions leave ``android.open_app`` out has no use for the
table, and the merge leaves it unused (user ruling 2026-11-04).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from spectragent.benchmarks.androidworld.service import ANDROIDWORLD, PinnedCode
from spectragent.core.data_loader._common import BenchmarkDataError, validation_summary

#: The file beside the task lists that holds the table.
OPEN_APP_FILE = "open_app.json"
#: The ``format`` the file declares.
OPEN_APP_FORMAT = "androidworld-open-app-2"
#: The action whose options the table sets.
OPEN_APP_ACTION = "android.open_app"


class OpenAppTable(BaseModel):
    """AndroidWorld's app names, as ``task_list.py`` writes them.

    Attributes:
        format: :data:`OPEN_APP_FORMAT`.
        commit: The AndroidWorld commit the table was read from.
        apps: Each name → the component (``package/activity``) AndroidWorld
            starts for it.
        default_app_uris: Each name of ``_DEFAULT_URIS`` → the data URI
            whose default app AndroidWorld opens for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    format: Literal["androidworld-open-app-2"]
    commit: str
    apps: dict[str, str] = Field(min_length=1)
    default_app_uris: dict[str, str] = Field(min_length=1)

    def action_entry(self) -> dict[str, JsonValue]:
        """The ``android.open_app`` entry of a benchmark's part of the capability configuration."""
        return {
            "action_id": OPEN_APP_ACTION,
            "apps": dict(self.apps),
            "default_app_uris": dict(self.default_app_uris),
        }


def read_open_app_table(data_dir: Path, code: PinnedCode = ANDROIDWORLD) -> OpenAppTable:
    """The table in *data_dir*, read from *code*'s pinned commit.

    Raises:
        BenchmarkDataError: There is none, it cannot be read, it is not the
            table (one of an earlier format included), or it was read from
            another commit; the message says how to write it again.
    """
    path = data_dir / OPEN_APP_FILE
    rewrite = (
        "task_list.py writes it beside the task lists; fetch them again with spectragent "
        f"benchmark fetch {code.benchmark}"
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
            f"{path} is not an AndroidWorld open_app table of format {OPEN_APP_FORMAT} "
            f"({validation_summary(exc)}): {rewrite}"
        ) from None
    if table.commit != code.commit:
        raise BenchmarkDataError(
            f"{path} was read from {code.name} {table.commit}, and the benchmark runs "
            f"{code.commit}: {rewrite}"
        )
    return table


__all__ = [
    "OPEN_APP_ACTION",
    "OPEN_APP_FILE",
    "OPEN_APP_FORMAT",
    "OpenAppTable",
    "read_open_app_table",
]
