"""OsmAndMarker prepared on a fresh boot by AndroidWorld's own service, whose launcher waits for OsmAnd's table.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). OsmAndMarker's initialize clears OsmAnd's
``map_markers`` table, twice: before and after it restores OsmAnd's snapshot.
That snapshot has no such table (AndroidWorld's setup closes OsmAnd before it
makes one), so AndroidWorld launches OsmAnd each time and deletes 7 s later.
On a fresh boot OsmAnd made the table 12-16 s after its process started, so
initialize failed with ``no such table: map_markers``. The launcher now waits
for the table (``docs/architecture_design.md`` §11.2). The tests check:

- setup succeeds: the service initialized the task and reset the device;
- OsmAnd's markers table is on the device, and empty;
- the unsolved task scores 0.

Measured 2026-11-03 on AndroidWorld's AVD: without the wait, initialize failed
on each of four fresh boots (two bench runs, two reproductions); with it,
initialize took 46 s.
"""

from __future__ import annotations

import base64
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    installation_problem,
    write_task_list,
)

TASK = "OsmAndMarker"
MARKERS_DB = "/data/data/net.osmand/databases/map_markers_db"

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
    pytest.mark.asyncio(loop_scope="module"),
]


@dataclass
class Prepared:
    env: AndroidEnvironment
    capability: AndroidCapability


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def prepared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Prepared]:
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    capability = androidworld_capability()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env)
    started = time.monotonic()
    await env.setup(context.env)
    print(f"setup, the task's initialize included, took {time.monotonic() - started:.1f} s")
    try:
        yield Prepared(env=env, capability=capability)
    finally:
        await env.teardown()


async def test_osmands_markers_table_is_on_the_device_and_empty(
    prepared: Prepared, tmp_path: Path
) -> None:
    session = prepared.capability._session
    assert session is not None
    client = AdbClient(session.endpoint.host, session.endpoint.port)
    encoded = (await client.shell(session.serial, f"base64 {MARKERS_DB}")).stdout
    copy = tmp_path / "map_markers_db"
    copy.write_bytes(base64.b64decode(b"".join(encoded.split())))

    with closing(sqlite3.connect(copy)) as db:
        (rows,) = db.execute("SELECT COUNT(*) FROM map_markers").fetchone()

    assert rows == 0


async def test_the_unsolved_task_scores_0(prepared: Prepared) -> None:
    (unsolved,) = await prepared.env.verify_live(timeout_s=60, answer=None)

    assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
