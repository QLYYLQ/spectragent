"""MarkorCreateNoteFromClipboard prepared on a fresh boot by AndroidWorld's own service, its clipboard broadcasts in the foreground queue.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). The task's initialize writes its text to the
clipboard and reads it back, each through Clipper with ``adb shell am
broadcast`` (``clipper.set``, ``clipper.get``). That is an ordered broadcast,
which without a flag waits in Android's background queue, within AndroidWorld's
10 s adb timeout: in a regression run on 2026-11-04 the write timed out there.
The launcher sends every broadcast to the foreground queue (``serve.py``'s
``broadcast_in_the_foreground``, ``docs/architecture_design.md`` §11.2). The
test checks:

- setup succeeds: AndroidWorld read back the text it wrote;
- the device's broadcast history holds both broadcasts in its foreground
  queue, each finished within 2 s of being enqueued.

It prints how long each took, from the history's uptime clock: AndroidWorld
sets the device's date at initialize, which the history's dates follow.

Measured 2026-11-04 on AndroidWorld's AVD, 8 initializes on four fresh boots:
``clipper.set`` finished 24-102 ms and ``clipper.get`` 4-50 ms after being
enqueued, while a broadcast sent to the background queue meanwhile waited up
to 13.7 s.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    androidworld_setup,
    installation_problem,
    write_task_list,
)

TASK = "MarkorCreateNoteFromClipboard"
#: ``Intent.FLAG_RECEIVER_FOREGROUND``, which ``--receiver-foreground`` sets.
RECEIVER_FOREGROUND = 0x10000000
#: Each field of a duration as dumpsys prints it (``TimeUtils.formatDuration``): ``+1s234ms``.
_FIELD = re.compile(r"(\d+)(d|h|ms|m|s)")
_UNIT_MS = {"d": 86_400_000, "h": 3_600_000, "m": 60_000, "s": 1_000, "ms": 1}

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]


def _ms(duration: str) -> int:
    """The milliseconds of a duration dumpsys printed: ``0``, ``+13ms``, ``+1s234ms``."""
    return sum(int(count) * _UNIT_MS[unit] for count, unit in _FIELD.findall(duration))


def _clipper_broadcasts(history: str) -> dict[str, tuple[str, int, int]]:
    """Each Clipper broadcast in ``dumpsys activity broadcasts history``, the newest of each action.

    For each: the queue it went through, its intent's flags, and the
    milliseconds from its enqueue to its finish.
    """
    found: dict[str, tuple[str, int, int]] = {}
    for record in history.split("  Historical Broadcast ")[1:]:
        intent = re.search(r"Intent \{ act=(clipper\.\w+) flg=0x([0-9a-f]+)", record)
        times = re.search(r"\((\S+) since enq\) finishTime=\S+ \((\S+) since disp\)", record)
        if intent and times:
            queue = record.split(" ", 1)[0]
            found.setdefault(intent[1], (queue, int(intent[2], 16), _ms(times[1]) + _ms(times[2])))
    return found


async def test_the_clipboard_broadcasts_of_initialize_go_to_the_foreground_queue(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env, setups=[setup])
    env.bind_setups([setup])
    started = time.monotonic()
    try:  # teardown also releases the emulator of a setup that failed part way
        await env.setup(context.env)
        setup_s = time.monotonic() - started
        session = capability._session
        assert session is not None
        client = AdbClient(session.endpoint.host, session.endpoint.port)
        history = await client.shell(session.serial, "dumpsys activity broadcasts history")
    finally:
        await env.teardown()

    broadcasts = _clipper_broadcasts(history.stdout.decode(errors="replace"))
    print(f"setup, the task's initialize included, took {setup_s:.1f} s; {broadcasts}")
    assert sorted(broadcasts) == ["clipper.get", "clipper.set"]
    for action, (queue, flags, took_ms) in broadcasts.items():
        assert queue == "foreground" and flags & RECEIVER_FOREGROUND, (action, queue, hex(flags))
        assert took_ms < 2000, (action, took_ms)
