"""The Android capability on a real emulator, booted once for the whole module.

Opt-in (marker ``requires_android_emulator``): set ``SPECTRAGENT_TEST_AVD`` to
the name of an AVD. The capability is composed from YAML, as a run composes
it, with the ``local_emulator`` provider. The one boot covers, in file order:

- the key hygiene of ``docs/architecture_design.md`` §11.2 ("两把钥匙"): the
  emulator hands the guest the run's own key (its log's "Sending adb public
  key" carries the run's ``adbkey.pub``) and the user's ``~/.android/adbkey*``
  stay as they were;
- the emulator's ``HOME`` is the run's: its console auth token is there, and a
  console command sent through adb with that ``HOME`` reaches the emulator;
- the run's own ``netsimd``: its ``netsim.ini`` is in the run's ``TMPDIR``, the
  user's ``$TMPDIR/netsim.ini`` stays as it was, and the guest joins its Wi-Fi;
- the screenshot's geometry;
- ``open_app`` with the foreground app checked, and the back and home keys;
- a tap that opens Settings' search, and ``type_text`` into its field;
- a scroll down that shows what is further down, measured on the screenshots;
- a clean teardown: no process of the run is left, nothing listens on its
  private adb port, its directory is gone, nothing listens on adb's default port
  5037 that did not before, every other emulator and ``netsimd`` (another
  run's or another tool's) that ran when the teardown began still runs, and
  the run added nothing to the top level of the user's home and left the
  user's ``~/.emulator_console_auth_token`` as it was.

The tap point on Settings' search bar was read off a Pixel 6 profile
(1080 x 2400, 420 dpi) with the API 33 Google APIs image; on another screen the
test that taps there skips.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import socket
import subprocess
import tempfile
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image, ImageChops

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
from spectragent.core.capabilities.android.backends.local_emulator import (
    LocalEmulatorProvider,
    find_sdk,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.observations import (
    FOREGROUND_APP,
    SCREENSHOT,
    AndroidForegroundAppObs,
    AndroidScreenshotObs,
)
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.image import image_size

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.asyncio(loop_scope="module"),
]

#: The AVD to boot; the marker skips this module when it is unset (``tests/conftest.py``).
AVD_VARIABLE = "SPECTRAGENT_TEST_AVD"
SETTINGS = "com.android.settings"
SETTINGS_SEARCH = "com.google.android.settings.intelligence"
#: Where Settings' search bar is on a 1080 x 2400 screen.
SEARCH_BAR = (540, 660)
USER_KEYS = (Path.home() / ".android" / "adbkey", Path.home() / ".android" / "adbkey.pub")
#: The console auth token an emulator started with the user's HOME keeps there.
USER_TOKEN = Path.home() / ".emulator_console_auth_token"
#: Where every emulator started with the user's TMPDIR finds the netsimd it shares.
USER_NETSIM = Path(tempfile.gettempdir()) / "netsim.ini"


@dataclass
class Booted:
    """The booted device, and what the run and the user had before it."""

    capability: AndroidCapability
    session: AndroidProviderSession
    run_directory: Path
    emulator_pid: int
    emulator_group: int
    adb_server_pid: int
    user_keys: list[tuple[str, float] | None]
    user_netsim: tuple[str, float] | None
    #: The names at the top level of the user's home before the boot.
    user_home: set[str]
    user_token: tuple[str, float] | None
    listening_on_5037: bool
    cycle: int = 0


def _file_state(path: Path) -> tuple[str, float] | None:
    """*path*'s digest and modification time, or ``None`` when there is no such file."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime


def _key_state() -> list[tuple[str, float] | None]:
    """Each of the user's adb key files: its digest and modification time, or ``None``."""
    return [_file_state(key) for key in USER_KEYS]


def _processes() -> list[tuple[int, int, str]]:
    """Every process on the machine: its PID, its process group and its command."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,command="], capture_output=True, text=True, check=True
    ).stdout
    rows = [line.split(maxsplit=2) for line in listing.splitlines()]
    return [(int(pid), int(group), command) for pid, group, command in rows]


def _emulators(*, besides: int) -> set[int]:
    """The PIDs of every emulator and netsimd running now outside the process group *besides*."""
    return {
        pid
        for pid, group, command in _processes()
        if group != besides and ("qemu-system" in command or command.split()[0].endswith("netsimd"))
    }


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def booted() -> AsyncIterator[Booted]:
    user_keys, user_netsim, listening = _key_state(), _file_state(USER_NETSIM), _listening(5037)
    user_home, user_token = set(os.listdir(Path.home())), _file_state(USER_TOKEN)
    raw = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {
                    "provider_id": "local_emulator",
                    "avd": os.environ[AVD_VARIABLE],
                },
            }
        ]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    await capability.setup()
    try:
        provider, session = capability.backend, capability._session
        assert isinstance(provider, LocalEmulatorProvider) and session is not None
        run = provider._runs[session.session_id]  # what the run owns, for the checks below
        assert run.emulator is not None and run.emulator_group is not None
        assert run.server is not None
        yield Booted(
            capability=capability,
            session=session,
            run_directory=run.directory,
            emulator_pid=run.emulator.pid,
            emulator_group=run.emulator_group.id,
            adb_server_pid=run.server.process.pid,
            user_keys=user_keys,
            user_netsim=user_netsim,
            user_home=user_home,
            user_token=user_token,
            listening_on_5037=listening,
        )
    finally:
        await capability.teardown()


async def _act(booted: Booted, name: str, **params: Any) -> ActionResult:
    return await booted.capability.execute_action(Action(name=f"android.{name}", params=params))


async def _observe(booted: Booted, key: str) -> FragmentBase:
    booted.cycle += 1
    await booted.capability.begin_observation(booted.cycle)
    return await booted.capability.resolve_fragment(key)


async def _foreground(booted: Booted) -> AndroidForegroundAppObs:
    front = await _observe(booted, FOREGROUND_APP)
    assert isinstance(front, AndroidForegroundAppObs)
    return front


async def _screenshot(booted: Booted) -> Image.Image:
    shot = await _observe(booted, SCREENSHOT)
    assert isinstance(shot, AndroidScreenshotObs) and shot.image_bytes is not None
    return Image.open(io.BytesIO(shot.image_bytes)).convert("L")


async def _settings_from_scratch(booted: Booted) -> None:
    """Settings, freshly started, in front: no page left open by an earlier test."""
    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    for package in (SETTINGS, SETTINGS_SEARCH):
        await client.shell(booted.session.serial, f"am force-stop {package}")
    assert (await _act(booted, "open_app", app_name="Settings")).success
    assert (await _foreground(booted)).package == SETTINGS


async def test_the_emulator_got_the_runs_own_key_and_the_users_keys_are_untouched(
    booted: Booted,
) -> None:
    log = (booted.run_directory / "emulator.log").read_text(errors="replace")
    sent = re.search(r"Sending adb public key \[(\S+)", log)
    assert sent is not None, "the emulator's log names the key it hands the guest"
    run_key = (booted.run_directory / ".android" / "adbkey.pub").read_text().split()[0]

    assert sent[1] == run_key
    assert _key_state() == booted.user_keys


async def test_a_console_command_sent_through_adb_reaches_the_emulator(booted: Booted) -> None:
    """The emulator keeps its console auth token in the run's HOME, where adb looks for it when it
    runs with that HOME; with two different homes the command fails without a word
    (``docs/architecture_design.md`` §11.2)."""
    assert (booted.run_directory / ".emulator_console_auth_token").is_file()
    provider = booted.capability.backend
    assert isinstance(provider, LocalEmulatorProvider)
    adb = find_sdk(provider.sdk_root, os.environ) / "platform-tools" / "adb"
    answer = subprocess.run(
        [str(adb), "-P", str(booted.session.endpoint.port), "-s", booted.session.serial]
        + ["emu", "avd", "name"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HOME": str(booted.run_directory)},
    )
    assert answer.stdout.split() == [provider.avd, "OK"], answer.stderr


async def test_the_run_has_a_netsimd_of_its_own_and_the_guest_is_online(booted: Booted) -> None:
    """netsimd carries the guest's network; one shared through the user's TMPDIR would die with
    whichever emulator's group started it (``docs/architecture_design.md`` §11.2)."""
    assert (booted.run_directory / "tmp" / "netsim.ini").is_file()
    assert _file_state(USER_NETSIM) == booted.user_netsim

    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    status = ""
    for _ in range(60):  # Wi-Fi joined about 5 s after the boot completed; at most 30 s
        status = (await client.shell(booted.session.serial, "cmd wifi status")).stdout.decode()
        if "Wifi is connected to" in status:
            break
        await asyncio.sleep(0.5)
    assert "Wifi is connected to" in status, status


async def test_a_screenshot_reports_the_screens_geometry(booted: Booted) -> None:
    shot = await _observe(booted, SCREENSHOT)
    assert isinstance(shot, AndroidScreenshotObs) and shot.image_bytes is not None
    size = booted.capability.screen_size

    assert image_size(shot.image_bytes) == (shot.image_width, shot.image_height) == size
    assert (shot.viewport_width, shot.viewport_height) == size
    assert shot.producer == "screencap"


async def test_open_app_brings_settings_to_the_front_and_back_and_home_leave_it(
    booted: Booted,
) -> None:
    await _settings_from_scratch(booted)

    assert (await _act(booted, "press_key", key="back")).success
    after_back = await _foreground(booted)
    await _settings_from_scratch(booted)
    assert (await _act(booted, "press_key", key="home")).success
    after_home = await _foreground(booted)

    for front in (after_back, after_home):
        assert front.package is not None and "launcher" in front.package


async def test_a_tap_opens_settings_search_and_type_text_writes_into_its_field(
    booted: Booted,
) -> None:
    if booted.capability.screen_size != (1080, 2400):
        pytest.skip("the search bar's point was measured on a 1080 x 2400 screen")
    await _settings_from_scratch(booted)

    assert (
        await _act(
            booted, "click_coordinate", coordinate_x=SEARCH_BAR[0], coordinate_y=SEARCH_BAR[1]
        )
    ).success
    assert (await _foreground(booted)).package == SETTINGS_SEARCH
    before = await _screenshot(booted)
    assert (await _act(booted, "type_text", text="bluetooth")).success
    after = await _screenshot(booted)

    # The results the query brings appear between the search field and the keyboard.
    results = (0, 300, 1080, 1400)
    changed = ImageChops.difference(before.crop(results), after.crop(results)).histogram()
    assert sum(changed[32:]) > 10_000


async def test_a_scroll_down_shows_what_is_further_down(booted: Booted) -> None:
    await _settings_from_scratch(booted)
    before = await _screenshot(booted)

    assert (await _act(booted, "scroll", direction="down")).success
    after = await _screenshot(booted)

    assert _content_shift(before, after) > 100, "the content moved up the screen"


def _content_shift(before: Image.Image, after: Image.Image) -> int:
    """How far the content moved up between the two screens, in pixels (negative: down).

    Each row of pixels with some content in it votes for the offset at which
    the same row appears in the other screen; the most common offset wins.
    """
    width, height = before.size

    def rows(image: Image.Image) -> list[bytes]:
        data = image.crop((60, 0, width - 60, height)).tobytes()
        row = width - 120
        return [data[i * row : (i + 1) * row] for i in range(height)]

    positions: dict[bytes, list[int]] = {}
    for y, row in enumerate(rows(before)):
        if len(set(row)) > 8:
            positions.setdefault(row, []).append(y)
    votes = Counter(
        y_before - y_after
        for y_after, row in enumerate(rows(after))
        for y_before in positions.get(row, ())
    )
    votes.pop(0, None)  # what did not move: the status bar
    return votes.most_common(1)[0][0] if votes else 0


async def test_teardown_stops_what_the_run_started_and_removes_its_directory(
    booted: Booted,
) -> None:
    # Taken just before: another tool may stop its own emulator during the boot.
    others = _emulators(besides=booted.emulator_group)
    await booted.capability.teardown()

    processes = _processes()
    left = {pid for pid, group, _ in processes if group == booted.emulator_group}
    assert left == set(), "nothing of the emulator's process group is left, netsimd included"
    running = {pid for pid, _, _ in processes}
    assert booted.emulator_pid not in running and booted.adb_server_pid not in running
    assert others <= running, "every other emulator and netsimd still runs"
    assert not _listening(booted.session.endpoint.port), "the run's adb server is gone"
    assert not booted.run_directory.exists()
    assert _listening(5037) == booted.listening_on_5037
    assert _key_state() == booted.user_keys
    assert _file_state(USER_NETSIM) == booted.user_netsim
    assert set(os.listdir(Path.home())) - booted.user_home == set()
    assert _file_state(USER_TOKEN) == booted.user_token
