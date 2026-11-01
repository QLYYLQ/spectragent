"""The Android capability on a real emulator, booted once for the whole module.

Opt-in (marker ``requires_android_emulator``): set ``SPECTRAGENT_TEST_AVD`` to
the name of an AVD. The capability is composed from YAML, as a run composes
it, with the ``local_emulator`` provider. The one boot covers, in file order:

- the key hygiene of ``docs/architecture_design.md`` §11.2 ("两把钥匙"): the
  emulator hands the guest the run's own key (its log's "Sending adb public
  key" carries the run's ``adbkey.pub``) and the user's ``~/.android/adbkey*``
  stay as they were;
- the screenshot's geometry;
- ``open_app`` with the foreground app checked, and the back and home keys;
- a tap that opens Settings' search, and ``type_text`` into its field;
- a scroll down that shows what is further down, measured on the screenshots;
- a clean teardown: no process of the run is left, its directory is gone, and
  nothing listens on adb's default port 5037 that did not before.

The tap point on Settings' search bar was read off a Pixel 6 profile
(1080 x 2400, 420 dpi) with the API 33 Google APIs image; on another screen the
test that taps there skips.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import socket
import subprocess
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
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.observations import (
    FOREGROUND_APP,
    SCREENSHOT,
    AndroidForegroundAppObs,
    AndroidScreenshotObs,
)
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action, ActionResult
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


@dataclass
class Booted:
    """The booted device, and what the run and the user had before it."""

    capability: AndroidCapability
    session: AndroidProviderSession
    run_directory: Path
    emulator_pid: int
    adb_server_pid: int
    user_keys: list[tuple[str, float] | None]
    listening_on_5037: bool
    cycle: int = 0


def _key_state() -> list[tuple[str, float] | None]:
    """Each of the user's adb key files: its digest and modification time, or ``None``."""
    return [
        (hashlib.sha256(key.read_bytes()).hexdigest(), key.stat().st_mtime)
        if key.exists()
        else None
        for key in USER_KEYS
    ]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def booted() -> AsyncIterator[Booted]:
    user_keys, listening = _key_state(), _listening(5037)
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
        assert run.emulator is not None and run.server is not None
        yield Booted(
            capability=capability,
            session=session,
            run_directory=run.directory,
            emulator_pid=run.emulator.pid,
            adb_server_pid=run.server.process.pid,
            user_keys=user_keys,
            listening_on_5037=listening,
        )
    finally:
        await capability.teardown()


async def _act(booted: Booted, name: str, **params: Any) -> ActionResult:
    return await booted.capability.execute_action(Action(name=f"android.{name}", params=params))


async def _observe(booted: Booted, key: str) -> Any:
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
    await booted.capability.teardown()

    group = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True, check=True
    ).stdout.split()
    left = {
        int(pid)
        for pid, pgid in zip(group[::2], group[1::2], strict=True)
        if int(pgid) == booted.emulator_pid
    }
    assert left == set(), "nothing of the emulator's process group is left"
    for pid in (booted.emulator_pid, booted.adb_server_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # signal 0 delivers nothing; it only asks whether the PID exists
    assert not booted.run_directory.exists()
    assert _listening(5037) == booted.listening_on_5037
    assert _key_state() == booted.user_keys
