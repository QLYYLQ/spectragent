# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Run AndroidWorld's own task service against one run's emulator, for the ``androidworld`` oracle.

This file runs under AndroidWorld's interpreter (its own venv, Python 3.11),
never Spectragent's, and imports ``android_world``. So it is not a module of
Spectragent: its directory has no ``__init__.py``, discovery never walks it, and
nothing imports it; the oracle runs it as a program
(``spectragent/core/capabilities/android/oracle/androidworld.py``,
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
  emulator reported;
- Android deletes AndroidWorld's per-app snapshots at every boot, so the app
  states tasks restore are taken again once the service is up;
- AndroidWorld clears an app's table before a task, and when the table is
  missing it launches the app and deletes 7 s later, while OsmAnd, started on a
  fresh boot, makes its markers table 12-16 s after its process starts: the
  launcher waits until the table is there (:func:`wait_for_the_table`);
- AndroidWorld reads the MiniWoB++ app (the instruction at initialize, the
  reward for the score and the episode question) through ordered broadcasts,
  which after a boot wait in the background queue behind Play Services' burst,
  past adb's 10 s timeout: they go to the foreground queue
  (:func:`read_in_the_foreground`).

It also adds one endpoint the app lacks. AndroidWorld's runner ends a MiniWoB++
episode itself, when the page has reported a reward, and asks after every step
(``miniwob_base.is_episode_terminated``); ``GET /task/episode_over`` answers
that question for the oracle, which asks after each of the agent's steps
(:func:`reward_ends_episode`).

The oracle starts it with a whitelisted environment: android_env logs its
whole environment when its log level is INFO, which this file never sets.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
import time
from collections.abc import Callable
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

#: AndroidWorld's ``sqlite_utils.delete_all_rows_from_table``: it deletes every
#: row of a table, given the table, the database's path on the device, the
#: environment, the app and a timeout.
Clear = Callable[[str, str, object, str, float | None], object]


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


def read_in_the_foreground(
    issue: Callable[[list[str], object], str],
    parse: Callable[[str], str | None],
    *,
    pause_s: float = 1.0,
) -> Callable[[str, object], str | None]:
    """AndroidWorld's ``miniwob_base._extract_data``, its broadcasts sent to the foreground queue.

    AndroidWorld reads the MiniWoB++ app through this one function: the
    instruction at initialize, the reward for the score, and the reward the
    launcher's ``/task/episode_over`` reads. It sends ``am broadcast -a
    <action>``, an ordered broadcast in the background queue, within
    AndroidWorld's 10 s adb timeout, which android_env tries twice and then
    raises on. After a boot, Play Services queues about 200 ordered broadcasts
    there (``gms.phenotype.UPDATE``). On freshly booted emulators a broadcast
    nobody receives took up to 17.1 s in that queue and at most 0.29 s with
    ``--receiver-foreground`` (64 probes, 2026-11-03), and 14 of 92 MiniWoB++
    tasks failed their initialize or first episode question on the timeout
    (``docs/architecture_design.md`` §11.2). The flag only moves the broadcast
    to the foreground queue: the same receiver answers it the same way.

    The function returned takes ``_extract_data``'s arguments, the action and
    android_env's environment. It sends ``am broadcast --receiver-foreground
    -a <action>`` with *issue* (AndroidWorld's ``issue_generic_request``, here
    handing back the command's output) and reads the answer with *parse*
    (AndroidWorld's ``extract_broadcast_data``). As AndroidWorld does, it
    returns the first answer that is not ``None``, and ``None`` after three
    broadcasts *pause_s* apart.
    """

    def extract_data(action: str, env: object) -> str | None:
        for _ in range(3):
            answer = parse(
                issue(["shell", "am", "broadcast", "--receiver-foreground", "-a", action], env)
            )
            if answer is not None:
                return answer
            time.sleep(pause_s)
        return None

    return extract_data


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
    through the same broadcast ``miniwob_base`` reads it with (three tries, a
    second apart). A reward the app does not give answers HTTP 500.
    """
    import fastapi
    from android_world.task_evals.miniwob import miniwob_base

    # A renamed private name would fail every question at run time; refuse to start.
    if not callable(getattr(miniwob_base, "_extract_data", None)) or not isinstance(
        getattr(miniwob_base, "_APP_NAME", None), str
    ):
        raise SystemExit(
            "serve.py reads a MiniWoB++ reward through miniwob_base._extract_data and "
            "_APP_NAME, which this AndroidWorld does not define"
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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve AndroidWorld's tasks for one run.")
    parser.add_argument("--port", type=int, required=True, help="Loopback port to serve on.")
    parser.add_argument("--src", required=True, help="The AndroidWorld checkout.")
    parser.add_argument("--adb", required=True, help="The adb binary of the run's server.")
    parser.add_argument("--adb-server-port", type=int, required=True)
    parser.add_argument("--console-port", type=int, required=True)
    parser.add_argument("--grpc-target", required=True, help="host:port the emulator reported.")
    parser.add_argument("--grpc-token-file", required=True, help="The emulator's discovery file.")
    parser.add_argument("--forwarder-apk", required=True, help="The cached forwarder APK.")
    return parser.parse_args()


def _patch(args: argparse.Namespace) -> None:
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
    from android_world.env import adb_utils, env_launcher, interface
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

    # A renamed function would be added, not replaced, and MiniWoB++'s reads
    # would queue behind Play Services' broadcasts again after a boot.
    if "_extract_data" not in vars(miniwob_base) or not all(
        callable(getattr(adb_utils, name, None))
        for name in ("issue_generic_request", "extract_broadcast_data")
    ):
        raise SystemExit(
            "serve.py sends MiniWoB++'s reads to the foreground queue by replacing "
            "miniwob_base._extract_data, through adb_utils.issue_generic_request and "
            "adb_utils.extract_broadcast_data, which this AndroidWorld does not all define"
        )
    miniwob_base._extract_data = read_in_the_foreground(
        lambda command, env: adb_utils.issue_generic_request(command, env).generic.output.decode(
            "utf-8"
        ),
        adb_utils.extract_broadcast_data,
    )

    adb_init = adb_controller.AdbController.__init__

    def private_server(self, config):
        config.adb_server_port = args.adb_server_port
        adb_init(self, config)

    def no_restart(self, timeout=None):
        logging.warning("serve.py: not restarting the adb server the run shares; retrying")

    adb_controller.AdbController.__init__ = private_server
    adb_controller.AdbController._restart_server = no_restart

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
        channel = grpc.intercept_channel(self._channel, Bearer(bearer_token()))
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
        from android_world.env.setup_device import setup
        from android_world.utils import app_snapshot

        for app in setup._APPS:
            try:
                app_snapshot.save_snapshot(app.app_name, env.controller)
            except Exception as exc:  # a task restoring that app then starts from its state now
                logging.warning("serve.py: no snapshot of %s: %s", app.app_name, exc)
        return env

    env_launcher.load_and_setup_env = load_for_this_run


def main() -> None:
    args = _arguments()
    sys.path.insert(0, args.src)
    os.chdir(args.src)
    _patch(args)
    import uvicorn
    from server import android_server

    _add_episode_over(android_server)
    uvicorn.run(android_server.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
