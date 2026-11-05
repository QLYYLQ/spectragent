# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Run AndroidWorld's own task service against one run's emulator, for the ``androidworld`` setup.

This file runs under AndroidWorld's interpreter (its own venv, Python 3.11),
never Spectragent's, and imports ``android_world``. So it is not a module of
Spectragent: its directory has no ``__init__.py``, discovery never walks it, and
nothing imports it; the ``androidworld`` setup component runs it as a program
(``spectragent/benchmarks/androidworld/service.py``,
``docs/architecture_design.md`` §11.2).

It starts the official FastAPI app (``src/server/android_server.py`` of the
pinned checkout) on a loopback port, after patching, at run time, what only
fits AndroidWorld's Docker image or would harm the run; the checkout stays as
it was cloned:

- the adb binary, the console port and the gRPC port the app hardcodes, and
  its one-time app setup on every start (never here);
- android_env always passes ``-P 5037`` to adb and deletes
  ``ANDROID_ADB_SERVER_PORT``: it uses the run's private adb server instead;
- android_env runs ``adb kill-server`` after any failed device command, which
  would kill the server the run shares: the command is retried instead;
- android_env turns on the touch and pointer overlays whenever it connects,
  which every screenshot would show: they stay off;
- AndroidWorld shows an agent's answer on the screen through ``adb shell am
  broadcast ... "<answer>"``, so the device's shell reads the answer: a lone
  double quote in it fails the answer action, ``$(...)`` in it runs as root,
  and the broadcast now and then hangs until adb's timeout: the answer is kept
  for the score and stays off the screen, which nobody looks at after the run;
- android_env downloads its accessibility forwarder on every start: the cached
  build is used;
- the emulator's gRPC listens on loopback only behind a token
  (``-grpc-use-token``): every call carries ``authorization: Bearer <token>``,
  read from the emulator's discovery file, and goes to the exact address the
  emulator reported (with no discovery file given, for an emulator started
  without a token, the calls carry none);
- Android deletes AndroidWorld's per-app snapshots at every boot, so the app
  states tasks restore are taken again once the service is up;
- AndroidWorld's one-time app setup gives Markor, Simple Gallery Pro and VLC
  All files access, which a fresh boot of AndroidWorld's AVD lacks: Markor and
  Simple Gallery Pro get it again once the service is up
  (:func:`grant_all_files_access`); VLC asks for it itself, since VLC granted
  it beforehand never starts its media library;
- AndroidWorld clears an app's table before a task, and when the table is
  missing it launches the app and deletes 7 s later, while OsmAnd, started on a
  fresh boot, makes its markers table 12-16 s after its process starts: the
  launcher waits until the table is there (:func:`wait_for_the_table`);
- AndroidWorld and android_env send ordered broadcasts (``adb shell am
  broadcast``: the MiniWoB++ app's reads, Clipper's clipboard, Retro Music's
  media scan, the accessibility forwarder's settings), which after a boot wait
  in the background queue behind Play Services' burst, AndroidWorld's past the
  10 s it gives each: every one goes to the foreground queue
  (:func:`broadcast_in_the_foreground`);
- AndroidWorld taps a button by its text (``actuation.find_and_click_element``:
  its contacts helper taps SAVE while a task adds contacts) by finding its
  index in one read of the UI tree and tapping that index in a second read,
  so a screen that changes in between (a warm keyboard opening) fails the
  initialize before anything is tapped: the find and tap are done again
  (:func:`tap_on_a_settled_screen`);
- AndroidWorld's MiniWoB++ initialize resets the task's page a fixed 1 s after
  loading it and reads its instruction right after: a page that has not
  loaded by then starts no episode, and a read before the page has made the
  instruction hands over an empty one. The reset waits for the page to load,
  and the read for its episode to start (:func:`wait_for_the_cover`).

It also adds one endpoint the app lacks. AndroidWorld's runner ends a MiniWoB++
episode itself, when the page has reported a reward, and asks after every step
(``miniwob_base.is_episode_terminated``); ``GET /task/episode_over`` answers
that question for the setup component, which asks after each of the agent's
steps (:func:`reward_ends_episode`).

The setup component starts it with a whitelisted environment: android_env logs its
whole environment when its log level is INFO, which this file never sets.

VenusBench's launcher (``spectragent/benchmarks/venusbench/venusbench_service/serve.py``)
loads this file by its path, under VenusBench's interpreter, and runs the same
steps on VenusBench's checkout: :func:`argument_parser`, :func:`enter_checkout`,
:func:`patch` and :func:`serve`, with AndroidWorld's app file in between
(VenusBench has no service of its own; ``docs/architecture_design.md`` §11.2
"VenusBench-Mobile 的接入方案"). Every patch applies there too: the names it
replaces are all in the fork.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
import time
from collections.abc import Callable, Collection
from pathlib import Path

#: The android_env these patches replace private names of: the version the
#: pinned AndroidWorld's requirements.txt pins. In another one a renamed name
#: would be added, not patched (a missed _restart_server would run adb
#: kill-server on the run's own server), so the launcher refuses to start.
ANDROID_ENV = "1.2.3"
#: How long after launching an app the launcher looks for the table AndroidWorld
#: is about to clear (:func:`wait_for_the_table`).
TABLE_WAIT_S = 30.0
#: The pause between two looks; each look pulls the database's whole directory.
TABLE_POLL_S = 1.0
#: The apps the launcher gives All files access (:func:`grant_all_files_access`), as
#: AndroidWorld's one-time app setup (``env/setup_device/apps.py``) does:
#: ``MarkorApp`` and ``SimpleGalleryProApp`` tap "Allow access to manage all files"
#: on the system's page. ``VlcApp`` does too, and VLC is left out: granted
#: beforehand, VLC never starts its media library.
ALL_FILES_ACCESS = ("net.gsantner.markor", "com.simplemobiletools.gallery.pro")
#: The id of the cover MiniWoB++'s ``core.js`` puts over the task when its page
#: has loaded (``core.startEpisode``, which every page's ``window.onload`` runs),
#: until an episode starts (``core.startEpisodeReal`` hides it).
COVER = "sync-task-cover"
#: How long the launcher waits for a MiniWoB++ page to load, and then for its
#: episode to start (:func:`wait_for_the_cover`).
PAGE_WAIT_S = 30.0
#: The pause between two looks; each look reads AndroidWorld's UI tree.
PAGE_POLL_S = 0.5

#: How many times the launcher finds and taps a button whose index went stale
#: between AndroidWorld's two reads (:func:`tap_on_a_settled_screen`).
TAP_TRIES = 3
#: The pause before it finds the button again.
TAP_RETRY_S = 1.0
#: The words AndroidWorld's ``execute_adb_action`` refuses a stale index with.
STALE_INDEX = "Invalid element index"

#: AndroidWorld's ``sqlite_utils.delete_all_rows_from_table``: it deletes every
#: row of a table, given the table, the database's path on the device, the
#: environment, the app and a timeout.
Clear = Callable[[str, str, object, str, float | None], object]
#: android_env's ``AdbController.execute_command``: it runs one adb command,
#: given the controller, the command's arguments, a timeout (``None``: the
#: controller's default) and whether the command is the device's, and returns
#: what adb printed.
Execute = Callable[[object, list[str], float | None, bool], bytes]


def wait_for_the_table(
    clear: Clear,
    *,
    exists: Callable[[str, str, object], bool],
    launch: Callable[[str, object], object],
    warn: Callable[[str], object],
    wait_s: float = TABLE_WAIT_S,
    poll_s: float = TABLE_POLL_S,
) -> Clear:
    """AndroidWorld's *clear*, run once the app has made the table it clears.

    AndroidWorld clears an app's table before a task. When the table is
    missing it launches the app, sleeps a fixed 7 s and deletes, and the delete
    raises ``no such table`` when the app has not made it yet. The snapshot
    OsmAnd's tasks restore has no markers table (AndroidWorld's setup closes
    OsmAnd before it makes one), and OsmAnd, started on a fresh boot, made it
    12-16 s after its process started (2026-11-03). So OsmAndMarker failed
    every initialize there (``docs/architecture_design.md`` §11.2).

    The function returned takes *clear*'s arguments. It looks with *exists*
    (AndroidWorld's ``table_exists``). When the table is missing, it launches
    the app with *launch*, as AndroidWorld does, and looks again until the
    table is there, at most *wait_s* after the launch. A look whose pull finds
    a listed file gone counts as "not there yet" (:func:`pull_lost_a_file`).
    Then it hands the same arguments to *clear*, which finds the table and
    skips its own launch and sleep. If *wait_s* passed first, *clear* launches,
    sleeps and fails as it always did.

    *clear* pulls the directory twice itself, and a pull of either can lose a
    file the app is still replacing. Both pulls come before it changes anything
    on the device, so after such a failure the function looks until the table
    is there again and runs *clear* once more. Any other failure of *clear* is
    raised. That includes the ``rm -r`` emptying the directory before the push,
    which can print the same words after it has deleted files. *warn* hears how
    each wait ended and each second run.
    """

    def look(table_name: str, remote_db_file_path: str, env: object) -> bool:
        try:
            return exists(table_name, remote_db_file_path, env)
        except Exception as error:
            if not pull_lost_a_file(error):
                raise
            return False

    def clear_once_the_table_is_there(
        table_name: str,
        remote_db_file_path: str,
        env: object,
        app_name: str,
        timeout_sec: float | None = None,
    ) -> None:
        def there() -> bool:
            return look(table_name, remote_db_file_path, env)

        if not there():
            launched = time.monotonic()
            launch(app_name, env)
            made = _look_until(there, launched + wait_s, poll_s)
            warn(
                f"{app_name} {'made' if made else 'had made no'} table {table_name} "
                f"{time.monotonic() - launched:.1f} s after its launch"
            )
        try:
            clear(table_name, remote_db_file_path, env, app_name, timeout_sec)
        except Exception as error:
            if not pull_lost_a_file(error):
                raise
            warn(
                f"{app_name} replaced a file while AndroidWorld read it to clear {table_name}; "
                "clearing again once the table is there"
            )
            _look_until(there, time.monotonic() + wait_s, poll_s)
            clear(table_name, remote_db_file_path, env, app_name, timeout_sec)

    return clear_once_the_table_is_there


def tap_on_a_settled_screen(
    find_and_click: Callable[..., object],
    *,
    warn: Callable[[str], object],
    tries: int = TAP_TRIES,
    retry_s: float = TAP_RETRY_S,
    sleep: Callable[[float], object] = time.sleep,
) -> Callable[..., object]:
    """AndroidWorld's ``find_and_click_element``, found and tapped again when the index went stale.

    It finds the element by its text in one read of the UI tree and taps the
    index it found in a second read; ``execute_adb_action`` refuses an index
    past the second read's elements (``ValueError: Invalid element index``)
    before it taps anything. Its contacts helper (``contacts_utils.add_contact``)
    taps SAVE in the editor its intent opens, and on a device kept from a task
    whose agent typed, the keyboard, already warm, opened in the editor between
    the two reads: VenusBench's GSATrackingSMSHandleIncomingKeywords and
    GSATrackingSimpleSmsNotifyDelivery failed their initialize, each the fifth
    task on its device (2026-11-05), and both initialized on a device where no
    agent had typed. On fresh devices and in AndroidWorld's runs it was never
    seen (``docs/architecture_design.md`` §11.2).

    The function returned takes *find_and_click*'s arguments. On that refusal
    it waits *retry_s* and finds and taps again, at most *tries* times in all;
    *warn* hears each retry. Any other failure, and the last refusal, is raised.
    """

    def find_and_click_element(*arguments: object, **named: object) -> object:
        for attempt in range(1, tries + 1):
            try:
                return find_and_click(*arguments, **named)
            except ValueError as error:
                if not str(error).startswith(STALE_INDEX) or attempt == tries:
                    raise
                warn(
                    f"the screen changed between AndroidWorld's two reads ({error}); tapping again"
                )
                sleep(retry_s)
        raise AssertionError("unreachable")

    return find_and_click_element


def _look_until(look: Callable[[], bool], deadline: float, poll_s: float) -> bool:
    """Ask *look* until it says yes or the monotonic clock passes *deadline*; whether it said yes."""
    while not look():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)
    return True


def pull_lost_a_file(error: Exception) -> bool:
    """Whether *error* is android_env's failed ``adb pull`` of a file that was gone.

    AndroidWorld reads a database by listing its directory and pulling each
    file. An app making its database meanwhile replaces some of them (OsmAnd
    swaps its journal for WAL files), and the pull of one fails with ``No such
    file or directory``; nothing on the device has changed then. Only a pull
    counts: another command can print the same words after it changed
    something (``rm -r <dir>/*`` when a file went first).
    """
    command, _, output = str(error).partition("\n")
    return (
        command.startswith("Error executing adb command: [")
        and " pull " in command
        and "No such file or directory" in output
    )


def grant_all_files_access(
    shell: Callable[[list[str]], str], *, warn: Callable[[str], object]
) -> None:
    """Give the apps in :data:`ALL_FILES_ACCESS` All files access, as AndroidWorld's setup does, where it is missing.

    AndroidWorld's setup opens Markor, Simple Gallery Pro and VLC and taps
    "Allow access to manage all files" on the system's All files access page,
    which sets the app's ``MANAGE_EXTERNAL_STORAGE`` app op to allow for its
    uid. A fresh boot of AndroidWorld's AVD has every runtime permission and
    the overlay app op the same setup grants, but not this one for any of the
    three (``appops get`` answers "No operations", 2026-11-03). So every run
    of a task in those apps met the app's storage dialog and the system page
    first: in our seed-42 runs, 16 paid 4-7 steps for it; AndroidWorld's own
    runner keeps its device, so only its first such task of a slice did
    (``docs/architecture_design.md`` §11.2).

    VLC is left to ask itself. AndroidWorld's setup opens it from the
    launcher (``monkey``), whose start "runs setup logic not available using
    ``adb shell am start``", its comment says, and grants the access through
    VLC's own prompt. A task opens VLC at ``.gui.MainActivity``, the activity
    AndroidWorld's app table names. There VLC asks for the access when it
    lacks it, and its media library works once the access is granted through
    that prompt. With the access granted beforehand, VLC asks nothing and its
    library never starts: its Video and Playlists tabs say "Loading" (both
    seen on fresh boots, 2026-11-04). In our regression run that day they did
    for whole episodes, its "Add to playlist" sheet found no playlist after
    saves, and VlcCreatePlaylist and VlcCreateTwoPlaylists failed. Both had
    passed in the run before, where the agent granted the access through
    VLC's prompt at the first step. AndroidWorld's own runner met the same
    "Loading" on the second VLC task of its device, the first having granted
    the access.

    For each package it asks the uid's mode with *shell* (``appops get --uid
    <package> MANAGE_EXTERNAL_STORAGE``) and, unless it is allow, sets it as
    the system's page does (``appops set --uid <package>
    MANAGE_EXTERNAL_STORAGE allow``). *warn* hears each grant.
    """
    for package in ALL_FILES_ACCESS:
        op = ["--uid", package, "MANAGE_EXTERNAL_STORAGE"]
        if "MANAGE_EXTERNAL_STORAGE: allow" in shell(["appops", "get", *op]):
            continue
        shell(["appops", "set", *op, "allow"])
        warn(f"{package} lacked the All files access AndroidWorld's app setup gives it: granted")


def broadcast_in_the_foreground(execute: Execute) -> Execute:
    """android_env's ``AdbController.execute_command``, every ``shell am broadcast`` sent to the foreground queue.

    ``am broadcast`` sends an ordered broadcast and waits until its receivers
    have answered; without a flag the broadcast waits in Android's background
    queue. After a boot, Play Services queues about 200 ordered broadcasts
    there (``gms.phenotype.UPDATE``). On freshly booted emulators a broadcast
    nobody receives took up to 17.1 s in that queue and at most 0.29 s with
    ``--receiver-foreground`` (64 probes, 2026-11-03). AndroidWorld gives each
    broadcast's adb command 10 s, which android_env tries twice and then
    raises on: 14 of 92 MiniWoB++ tasks failed their initialize or first
    episode question on it (2026-11-03), and MarkorCreateNoteFromClipboard its
    initialize, on ``clipper.set`` (2026-11-04). With the flag, that
    initialize's ``clipper.set`` and ``clipper.get`` finished 4-102 ms after
    they were enqueued, 8 times on four fresh boots; on the first boot a
    broadcast sent to the background queue meanwhile waited 13.7 s
    (2026-11-04; ``docs/architecture_design.md`` §11.2).

    Every broadcast the service sends goes through ``execute_command`` (not
    every adb command: android_env's logcat stream runs adb itself):
    AndroidWorld's through ``adb_utils.issue_generic_request`` (the MiniWoB++
    app's reads, Clipper's clipboard, Retro Music's media scan) and
    android_env's own (the accessibility forwarder's two settings, which wait
    up to android_env's 120 s). The function returned takes its arguments. A
    command that starts with ``shell am broadcast`` gets
    ``--receiver-foreground`` after ``broadcast``; *execute* runs it, and every
    other command as it came, with the same timeout and device flag, and hands
    back what adb printed. Its two tries, and AndroidWorld's own retries and
    parsing above it, stay as they were. The flag only moves the broadcast to
    the foreground queue: the same receivers answer it the same way.
    """

    def execute_command(
        self: object, args: list[str], timeout: float | None = None, device_specific: bool = True
    ) -> bytes:
        if args[:3] == ["shell", "am", "broadcast"]:
            args = [*args[:3], "--receiver-foreground", *args[3:]]
        return execute(self, args, timeout, device_specific)

    return execute_command


def wait_for_the_cover(
    shown: Callable[[], Collection[str]],
    *,
    there: bool,
    warn: Callable[[str], object],
    wait_s: float = PAGE_WAIT_S,
    poll_s: float = PAGE_POLL_S,
) -> None:
    """Wait until a MiniWoB++ page's cover is on the screen (*there*), or gone from it.

    AndroidWorld's initialize (``MiniWoBTask.initialize_task``) loads the
    task's page, sleeps a fixed 1 s, sends the app ``reset`` and ``step``, and
    reads the instruction. ``reset`` runs ``core.startEpisodeReal()`` in the
    page: it makes the problem (unseeded, ``Math.random``), logs its
    instruction, which the app keeps for the read, and then hides the cover
    the page's ``window.onload`` put over the task. On a fresh boot the app
    starts cold, and its page can take far longer than that second. Then the
    episode went wrong (2026-11-03): the instruction was still empty when
    AndroidWorld read it, so the goal was the fixed part alone (count-sides;
    in a reproduction click-pie's page showed up 10 s after initialize began,
    running an episode its reset had made, while AndroidWorld had read an
    empty instruction at 8 s); or the script made the problem before
    ``onload``, failed to hide the cover it did not have yet, and ``onload``
    then covered the page for good (click-option's agent spent 30 steps on
    the black cover). The read can beat the script on a loaded page too: with
    the reset sent to a loaded page, count-shape's instruction still came
    back empty.

    So the launcher's initialize waits twice. After AndroidWorld's 1 s it
    waits until the cover is there: the page has loaded and waits for its
    episode, as AndroidWorld's sleep assumes. After the reset and the step it
    waits until the cover is gone: the episode has started, and its
    instruction, logged before the cover went, is there for the read. It
    looks at the screen with *shown*, the ids of the visible elements in
    AndroidWorld's UI tree, *poll_s* apart, at most *wait_s*; *warn* hears how
    long a wait took when the first look missed.

    Raises:
        RuntimeError: The cover was not there, or still there, after *wait_s*.
            The message says what the tree showed last: it can lag behind the
            screen, and the status bar's ids alone are not a page without its
            cover.
    """
    began = time.monotonic()
    last: Collection[str] = ()

    def done() -> bool:
        nonlocal last
        last = shown()
        return (COVER in last) == there

    if done():
        return
    if not _look_until(done, began + wait_s, poll_s):
        raise RuntimeError(
            (
                f"the MiniWoB++ page's cover was not in AndroidWorld's UI tree {wait_s:g} s "
                "after AndroidWorld's 1 s"
                if there
                else f"the MiniWoB++ page's cover was still in AndroidWorld's UI tree {wait_s:g} s "
                "after its reset"
            )
            + f"; the tree last showed {sorted(last) or 'no element ids'}"
        )
    took = time.monotonic() - began
    warn(
        f"the MiniWoB++ page loaded {took:.1f} s after AndroidWorld's 1 s; its reset waited"
        if there
        else f"the MiniWoB++ page started its episode {took:.1f} s after its reset; "
        "the instruction's read waited"
    )


def start_the_episode(
    task_name: str,
    env: object,
    *,
    start: Callable[[object, list[str]], object],
    shown: Callable[[object], Collection[str]],
    read: Callable[[object], str],
    warn: Callable[[str], object],
    sleep: Callable[[float], object] = time.sleep,
    wait_s: float = PAGE_WAIT_S,
    poll_s: float = PAGE_POLL_S,
) -> str:
    """AndroidWorld's MiniWoB++ initialize after its task's own part, step by step, with the two waits.

    ``MiniWoBTask.initialize_task`` loads the page of the task *task_name*,
    sleeps a fixed 1 s, sends ``reset`` and ``step``, and reads the
    instruction. This takes the same steps in the same order with *start*
    (AndroidWorld's ``start_activity`` of the MiniWoB++ app's activity, given
    *env* and the intent's extras), *sleep* and *read* (its
    ``_get_episode_utterance``), and waits for the cover to be there before
    the reset and gone before the read (:func:`wait_for_the_cover`, through
    *shown*, *warn*, *wait_s* and *poll_s*). It returns the instruction.
    """
    task_config = f'{{"task":"{task_name}"}}'
    start(env, ["--es", "RL_TASK_APP_CONFIG", f"'{task_config}'"])
    sleep(1)
    wait_for_the_cover(lambda: shown(env), there=True, warn=warn, wait_s=wait_s, poll_s=poll_s)
    start(env, ["--ez", "reset", "true"])
    start(env, ["--ez", "step", "true"])
    wait_for_the_cover(lambda: shown(env), there=False, warn=warn, wait_s=wait_s, poll_s=poll_s)
    return read(env)


def reward_ends_episode(raw: str) -> bool:
    """Whether a MiniWoB++ page's stored raw reward *raw* ends its episode, as AndroidWorld's runner says.

    AndroidWorld ends the episode when the reward the app stores is not empty
    and not zero (``miniwob_base.is_episode_terminated``). It reads the reward
    with ``int``, which raises on the fractional rewards seven tasks give
    (``use-colorwheel``, ``tic-tac-toe``'s draw ...), and its runner records an
    exception for the episode. Read as a number instead, those episodes end
    like the rest, and their score still fails as on AndroidWorld
    (``is_successful`` reads with ``int``): they stay unscored
    (``docs/architecture_design.md`` §11.2).

    Raises:
        ValueError: *raw* is no number.
    """
    return bool(raw) and float(raw) != 0.0


def _add_episode_over(android_server) -> None:
    """Add ``GET /task/episode_over`` to the app: whether the addressed task's episode is over.

    As AndroidWorld's runner decides: only a task whose name starts with
    ``miniwob`` has an episode the environment ends, and it is over when the
    reward the MiniWoB++ app stores ends it (:func:`reward_ends_episode`), read
    through ``miniwob_base._extract_data`` as ``miniwob_base`` reads it (its
    broadcast goes to the foreground queue, :func:`broadcast_in_the_foreground`).
    A reward the app does not give answers HTTP 500.
    """
    import fastapi
    from android_world.task_evals.miniwob import miniwob_base

    # A renamed private name would fail every question at run time; refuse to start.
    if not isinstance(getattr(miniwob_base, "_APP_NAME", None), str) or not callable(
        getattr(miniwob_base, "_extract_data", None)
    ):
        raise SystemExit(
            "serve.py asks the MiniWoB++ app for its reward by its package name, "
            "miniwob_base._APP_NAME, through miniwob_base._extract_data, which this "
            "AndroidWorld does not both define"
        )
    app = android_server.app

    @app.get("/task/episode_over")
    async def episode_over(task_type: str, task_idx: int):
        task = app.state.suite[task_type][task_idx]
        if not task.name.lower().startswith("miniwob"):
            return {"over": False}
        reward = miniwob_base._extract_data(
            f"{miniwob_base._APP_NAME}.app.GET_REWARD_ACTION",
            app.state.app_android_env.controller.env,
        )
        if reward is None:
            raise fastapi.HTTPException(
                status_code=500, detail="the MiniWoB++ app gave no reward when asked"
            )
        return {"over": reward_ends_episode(reward)}


def argument_parser(description: str, *, serving: bool = True) -> argparse.ArgumentParser:
    """The arguments every launcher takes: the checkout and the run's device, and where to serve
    when *serving* (a one-time setup serves nothing)."""
    parser = argparse.ArgumentParser(description=description)
    if serving:
        parser.add_argument("--port", type=int, required=True, help="Loopback port to serve on.")
    parser.add_argument("--src", required=True, help="The checkout of AndroidWorld's code.")
    parser.add_argument("--adb", required=True, help="The adb binary of the run's server.")
    parser.add_argument("--adb-server-port", type=int, required=True)
    parser.add_argument("--console-port", type=int, required=True)
    parser.add_argument("--grpc-target", required=True, help="host:port the emulator reported.")
    parser.add_argument(
        "--grpc-token-file",
        help="The emulator's discovery file, whose token every gRPC call carries; none for an "
        "emulator started without -grpc-use-token (a one-time setup's writable boot).",
    )
    parser.add_argument("--forwarder-apk", required=True, help="The cached forwarder APK.")
    return parser


def enter_checkout(src: str) -> None:
    """Make the checkout at *src* importable and the working directory, as AndroidWorld's runner expects."""
    sys.path.insert(0, src)
    os.chdir(src)


def patch(args: argparse.Namespace) -> None:
    """Patch android_env and AndroidWorld, at run time, as the module docstring lists."""
    found = importlib.metadata.version("android_env")
    if found != ANDROID_ENV:
        raise SystemExit(
            f"serve.py patches android_env {ANDROID_ENV}, the version the pinned AndroidWorld "
            f"requires, and this venv has {found}: reinstall it from the checkout's "
            f"requirements.txt"
        )
    import grpc
    from absl import logging
    from android_env.components import adb_controller, config_classes
    from android_env.components.simulators.emulator import emulator_simulator
    from android_env.proto import emulator_controller_pb2_grpc, snapshot_service_pb2_grpc
    from android_env.wrappers import a11y_grpc_wrapper
    from android_world.env import (
        actuation,
        adb_utils,
        android_world_controller,
        env_launcher,
        interface,
        representation_utils,
    )
    from android_world.task_evals.miniwob import miniwob_base
    from android_world.task_evals.utils import sqlite_utils

    logging.set_verbosity(logging.WARNING)

    def warn(note: str) -> None:
        logging.warning("serve.py: %s", note)

    settings_init = config_classes.DeviceSettingsConfig.__init__

    def no_overlays(self, *rest, **named):
        settings_init(self, *rest, **named)
        self.show_touches = False
        self.show_pointer_location = False

    config_classes.DeviceSettingsConfig.__init__ = no_overlays

    def answer_off_the_screen(self, message, header=""):
        # The answer action keeps the answer for the score before it calls this.
        return None

    # A renamed method would be added, not replaced, and the device's root shell
    # would read every answer again.
    if "display_message" not in vars(interface.AsyncAndroidEnv):
        raise SystemExit(
            "serve.py keeps answers off the screen by replacing "
            "AsyncAndroidEnv.display_message, which this AndroidWorld does not define"
        )
    interface.AsyncAndroidEnv.display_message = answer_off_the_screen

    # A renamed function would be added, not replaced, and OsmAndMarker's
    # initialize would race its 7 s again.
    if not all(
        callable(getattr(sqlite_utils, name, None))
        for name in ("delete_all_rows_from_table", "table_exists")
    ):
        raise SystemExit(
            "serve.py waits for the table AndroidWorld clears by replacing "
            "sqlite_utils.delete_all_rows_from_table and asking sqlite_utils.table_exists, "
            "which this AndroidWorld does not both define"
        )

    def launch(app_name: str, env) -> None:  # env: AndroidWorld's AsyncEnv
        adb_utils.launch_app(app_name, env.controller)

    sqlite_utils.delete_all_rows_from_table = wait_for_the_table(
        sqlite_utils.delete_all_rows_from_table,
        exists=sqlite_utils.table_exists,
        launch=launch,
        warn=warn,
    )

    # A renamed function would be added, not replaced, and a stale index would
    # fail an initialize again.
    if not callable(getattr(actuation, "find_and_click_element", None)):
        raise SystemExit(
            "serve.py taps a button again when the screen changed under AndroidWorld by "
            "replacing actuation.find_and_click_element, which this AndroidWorld does not define"
        )
    actuation.find_and_click_element = tap_on_a_settled_screen(
        actuation.find_and_click_element, warn=warn
    )

    # A renamed method would be added, not replaced, and a page AndroidWorld
    # resets or reads too early would start no episode or give no goal again.
    task_class = miniwob_base.MiniWoBTask
    if (
        "initialize_task" not in vars(task_class)
        or not isinstance(getattr(miniwob_base, "_MAIN_ACTIVITY", None), str)
        or not callable(getattr(miniwob_base, "_get_episode_utterance", None))
        or not callable(getattr(adb_utils, "start_activity", None))
        or not callable(
            getattr(android_world_controller.AndroidWorldController, "get_ui_elements", None)
        )
        or "resource_name"
        not in getattr(representation_utils.UIElement, "__dataclass_fields__", {})
    ):
        raise SystemExit(
            "serve.py waits for a MiniWoB++ page by replacing MiniWoBTask.initialize_task with "
            "its own steps, through adb_utils.start_activity, miniwob_base._MAIN_ACTIVITY and "
            "miniwob_base._get_episode_utterance, and looks at the page through "
            "AndroidWorldController.get_ui_elements and UIElement.resource_name, which this "
            "AndroidWorld does not all define"
        )

    def start(env, extras: list[str]) -> None:  # env: AndroidWorld's AsyncEnv
        adb_utils.start_activity(miniwob_base._MAIN_ACTIVITY, extras, env.controller)

    def shown(env) -> set[str]:
        elements = env.controller.get_ui_elements()  # visible ones, from its forwarder's tree
        return {element.resource_name for element in elements if element.resource_name}

    def read(env) -> str:
        return miniwob_base._get_episode_utterance(env.controller)

    def initialize_task(self, env) -> None:  # self: a MiniWoBTask
        super(task_class, self).initialize_task(env)
        self._params["utterance"] = start_the_episode(
            self.params["task_name"], env, start=start, shown=shown, read=read, warn=warn
        )

    task_class.initialize_task = initialize_task

    adb_init = adb_controller.AdbController.__init__

    def private_server(self, config):
        config.adb_server_port = args.adb_server_port
        adb_init(self, config)

    def no_restart(self, timeout=None):
        logging.warning("serve.py: not restarting the adb server the run shares; retrying")

    adb_controller.AdbController.__init__ = private_server
    adb_controller.AdbController._restart_server = no_restart
    adb_controller.AdbController.execute_command = broadcast_in_the_foreground(
        adb_controller.AdbController.execute_command
    )

    a11y_grpc_wrapper._get_accessibility_forwarder_apk = lambda: Path(
        args.forwarder_apk
    ).read_bytes()

    def bearer_token() -> str:
        for line in Path(args.grpc_token_file).read_text().splitlines():
            if line.startswith("grpc.token="):
                return line.partition("=")[2].strip()
        raise RuntimeError(f"no grpc.token in {args.grpc_token_file}")

    class Details(grpc.ClientCallDetails):
        def __init__(self, details, metadata):
            self.method, self.timeout, self.metadata = details.method, details.timeout, metadata
            self.credentials = details.credentials
            self.wait_for_ready = getattr(details, "wait_for_ready", None)
            self.compression = getattr(details, "compression", None)

    class Bearer(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor):
        def __init__(self, token):
            self._header = ("authorization", f"Bearer {token}")

        def _details(self, details):
            metadata = [*(details.metadata or []), self._header]
            if hasattr(details, "_replace"):
                return details._replace(metadata=metadata)
            return Details(details, metadata)

        def intercept_unary_unary(self, continuation, details, request):
            return continuation(self._details(details), request)

        def intercept_unary_stream(self, continuation, details, request):
            return continuation(self._details(details), request)

    def connect(self, grpc_port, timeout_sec=100):
        # android_env's own _connect_to_emulator, with the reported address
        # instead of localhost:<port>, no HTTP proxy, and the token on every call.
        options = [
            ("grpc.max_send_message_length", -1),
            ("grpc.max_receive_message_length", -1),
            ("grpc.enable_http_proxy", 0),
        ]
        try:
            self._channel = grpc.secure_channel(
                args.grpc_target, grpc.local_channel_credentials(), options=options
            )
            grpc.channel_ready_future(self._channel).result(timeout=timeout_sec)
        except (grpc.RpcError, grpc.FutureTimeoutError) as error:
            raise emulator_simulator.EmulatorBootError(
                "Failed to connect to the emulator."
            ) from error
        channel = (
            grpc.intercept_channel(self._channel, Bearer(bearer_token()))
            if args.grpc_token_file
            else self._channel
        )
        return (
            emulator_controller_pb2_grpc.EmulatorControllerStub(channel),
            snapshot_service_pb2_grpc.SnapshotServiceStub(channel),
        )

    emulator_simulator.EmulatorSimulator._connect_to_emulator = connect

    load = env_launcher.load_and_setup_env

    def load_for_this_run(*rest, **named):
        named.update(
            adb_path=args.adb,
            console_port=args.console_port,
            grpc_port=int(args.grpc_target.rpartition(":")[2]),
            emulator_setup=False,
        )
        env = load(*rest, **named)
        grant_all_files_access(
            lambda command: adb_utils.issue_generic_request(
                ["shell", *command], env.controller
            ).generic.output.decode("utf-8"),
            warn=warn,
        )
        from android_world.env.setup_device import setup
        from android_world.utils import app_snapshot

        for app in setup._APPS:
            try:
                app_snapshot.save_snapshot(app.app_name, env.controller)
            except Exception as exc:  # a task restoring that app then starts from its state now
                logging.warning("serve.py: no snapshot of %s: %s", app.app_name, exc)
        return env

    env_launcher.load_and_setup_env = load_for_this_run


def serve(android_server, port: int) -> None:
    """Add ``GET /task/episode_over`` to AndroidWorld's app module *android_server*, and serve it on *port*."""
    import uvicorn

    _add_episode_over(android_server)
    uvicorn.run(android_server.app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    args = argument_parser("Serve AndroidWorld's tasks for one run.").parse_args()
    enter_checkout(args.src)
    patch(args)
    from server import android_server

    serve(android_server, args.port)


if __name__ == "__main__":
    main()
