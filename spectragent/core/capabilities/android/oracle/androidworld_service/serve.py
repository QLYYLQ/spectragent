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
  launcher waits until the table is there (:func:`launch_and_wait_for_table`).

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
#: is about to clear (:func:`launch_and_wait_for_table`).
TABLE_WAIT_S = 30.0
#: The pause between two looks; each look pulls the database's whole directory.
TABLE_POLL_S = 1.0


def launch_and_wait_for_table(
    launch: Callable[[], object],
    table_made: Callable[[], bool],
    *,
    wait_s: float = TABLE_WAIT_S,
    poll_s: float = TABLE_POLL_S,
) -> float | None:
    """Launch an app with *launch*, then look with *table_made* until its table is there.

    AndroidWorld clears an app's table before a task
    (``sqlite_utils.delete_all_rows_from_table``). When the table is missing it
    launches the app, sleeps a fixed 7 s and deletes, and the delete raises
    ``no such table`` when the app has not made it yet. The snapshot OsmAnd's
    tasks restore has no markers table (AndroidWorld's setup closes OsmAnd
    before it makes one), and OsmAnd, started on a fresh boot, made it 12-16 s
    after its process started (2026-11-03). So OsmAndMarker failed every
    initialize there. The launcher launches the app the same way and looks
    again until the table is there, at most *wait_s* after the launch
    (``docs/architecture_design.md`` §11.2).

    Returns:
        The seconds from the launch to the look that saw the table; ``None``
        when none did within *wait_s*.
    """
    launched = time.monotonic()
    launch()
    while not table_made():
        if time.monotonic() - launched >= wait_s:
            return None
        time.sleep(poll_s)
    return time.monotonic() - launched


def lost_a_file(error: Exception) -> bool:
    """Whether *error*, raised reading a database's directory, says adb found a listed file gone.

    AndroidWorld lists the directory and then pulls each file. An app making its
    database meanwhile replaces some of them (OsmAnd swaps its journal for WAL
    files), and the pull of one fails with ``No such file or directory``. The
    table is then not there yet, which is all the look needs to know.
    """
    return "No such file or directory" in str(error)


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
    from android_env.components import adb_controller, config_classes, errors
    from android_env.components.simulators.emulator import emulator_simulator
    from android_env.proto import emulator_controller_pb2_grpc, snapshot_service_pb2_grpc
    from android_env.wrappers import a11y_grpc_wrapper
    from android_world.env import adb_utils, env_launcher, interface
    from android_world.task_evals.utils import sqlite_utils

    logging.set_verbosity(logging.WARNING)

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
    clear = sqlite_utils.delete_all_rows_from_table

    def clear_once_the_app_made_its_table(
        table_name: str,
        remote_db_file_path: str,
        env,
        app_name: str,
        timeout_sec: float | None = None,
    ) -> None:
        def table_made() -> bool:
            try:
                return sqlite_utils.table_exists(table_name, remote_db_file_path, env)
            except errors.AdbControllerError as exc:
                if not lost_a_file(exc):
                    raise
                return False

        if not table_made():
            took = launch_and_wait_for_table(
                lambda: adb_utils.launch_app(app_name, env.controller), table_made
            )
            if took is None:
                logging.warning(
                    "serve.py: %s had made no table %s %g s after its launch",
                    app_name,
                    table_name,
                    TABLE_WAIT_S,
                )
            else:
                logging.warning(
                    "serve.py: %s made table %s %.1f s after its launch", app_name, table_name, took
                )
        # With the table there, AndroidWorld's own function skips its launch and sleep.
        clear(table_name, remote_db_file_path, env, app_name, timeout_sec)

    sqlite_utils.delete_all_rows_from_table = clear_once_the_app_made_its_table

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
