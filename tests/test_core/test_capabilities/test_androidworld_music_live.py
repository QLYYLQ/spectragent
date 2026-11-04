"""A Retro Music task prepared on a real emulator by AndroidWorld's own service: its mp3 files reach the device.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). AndroidWorld's four Retro Music tasks make
their mp3 files at initialize with pydub, which runs ffmpeg from PATH; the
service's PATH reaches the host's ffmpeg only through the link the setup makes
(``docs/architecture_design.md`` §11.2). With the system's
directories alone, initialize failed and the task never started.
``RetroCreatePlaylist`` writes the 15 songs its parameters name into
``/sdcard/Music``:

- setup succeeds: the service initialized the task and reset the device;
- each song is on the device, an mp3 file starting with the ID3 tag ffmpeg
  writes;
- the unsolved task scores 0.

Measured 2026-11-03 on AndroidWorld's AVD: with the system's directories alone,
``/task/initialize`` answered HTTP 500 and the service's log ended with pydub's
"Couldn't find ffmpeg"; with the link, initialize took 5.9 s and the unsolved
task scored 0.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    androidworld_setup,
    bind_androidworld,
    installation_problem,
    write_task_list,
)

TASK = "RetroCreatePlaylist"
#: Each file in the music directory as ``<name>|<size>|<first 3 bytes>``.
LISTING = (
    'for f in /sdcard/Music/*; do [ -f "$f" ] && '
    'echo "${f##*/}|$(stat -c %s "$f")|$(head -c 3 "$f")"; done'
)

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
    pytest.mark.asyncio(loop_scope="module"),
]


@dataclass
class Prepared:
    env: AndroidEnvironment
    capability: AndroidCapability
    #: The songs the task's parameters name, playlist and noise alike.
    songs: list[str]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def prepared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Prepared]:
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    oracle_task = context.env["oracle_task"]
    assert isinstance(oracle_task, dict) and isinstance(oracle_task["params"], dict)
    songs: list[str] = []
    for key in ("files", "noise_files"):
        names = oracle_task["params"][key]
        assert isinstance(names, list)
        songs += [str(name) for name in names]
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    bind_androidworld(env, context.env, setup)
    started = time.monotonic()
    await env.setup(context.env)
    print(f"setup, the task's initialize included, took {time.monotonic() - started:.1f} s")
    try:
        yield Prepared(env=env, capability=capability, songs=songs)
    finally:
        await env.teardown()


async def test_each_song_the_task_names_is_an_mp3_file_on_the_device(prepared: Prepared) -> None:
    session = prepared.capability._session
    assert session is not None
    client = AdbClient(session.endpoint.host, session.endpoint.port)
    listing = (await client.shell(session.serial, LISTING)).stdout.decode(errors="replace")
    files = {}
    for line in listing.splitlines():
        name, size, start = line.split("|")
        files[name] = (int(size), start)

    assert len(prepared.songs) == 15
    for song in prepared.songs:
        assert song in files, f"{song} is not on the device; /sdcard/Music holds {sorted(files)}"
        size, start = files[song]
        assert size > 0 and start == "ID3", f"{song}: {size} bytes starting {start!r}"


async def test_the_unsolved_task_scores_0(prepared: Prepared) -> None:
    (unsolved,) = await prepared.env.verify_live(timeout_s=60, answer=None)

    assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
