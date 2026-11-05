# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Run AndroidWorld's task service on VenusBench's checkout, for the ``venusbench`` setup.

This file runs under VenusBench's interpreter (its own venv, Python 3.11),
never Spectragent's, and imports VenusBench's ``android_world``. Like
AndroidWorld's launcher it is not a module of Spectragent: its directory has
no ``__init__.py`` and the ``venusbench`` setup component runs it as a program
(``spectragent/benchmarks/venusbench/service.py``,
``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案").

VenusBench forked AndroidWorld before AndroidWorld had a service, so it runs
AndroidWorld's own app file (``--service-app``, from the pinned AndroidWorld
commit, kept in the installation, never in VenusBench's checkout) through
AndroidWorld's launcher, loaded by its path: its arguments, its patches and
its ``/task/episode_over`` all apply, since every name they replace is in the
fork. Before them it does the two things only VenusBench needs, leaving the
checkout as it was cloned:

- VenusBench reads its settings from a singleton (``config.config``) when its
  modules are imported: the controller its adb path, three task modules the
  local paths of its external files. They are filled for this run before
  anything imports them: the run's adb, the installation's files
  (``--files``), the device's paths, and no LLM judge.
- ``task_evals/single/audio_recorder.py`` sets the temporary directory of the
  whole process to ``<working directory>/temp`` when it is imported, and the
  working directory is the checkout, which has no ``temp/``: android_env then
  fails to write the forwarder it installs, and the service never starts. It
  is set back to ``TMPDIR`` once the app has imported every task.

It also fixes one bug of VenusBench's at run time (the user's ruling of
2026-11-05: a bug of the official harness that affects runs or scoring is
fixed on our side, and the deviation recorded): the GSATracking tasks start a
thread at initialize that calls or texts the device on the wall clock, and
only their ``is_successful`` stops it; ``tear_down`` never does. VenusBench's
own runner calls ``is_successful`` only when the agent declared the task done,
and a sweep that keeps its device skips the score when the benchmark lists no
verifier or the answer or score call fails; the thread then rings or texts the
next task's device. :func:`stop_background_threads` makes ``TaskEval.tear_down``
stop and join a task's thread first.

:func:`prepare` is shared with ``setup_apps.py`` beside it.
"""

from __future__ import annotations

import argparse
import importlib.util
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

#: AndroidWorld's launcher, whose steps this one runs.
ANDROIDWORLD_LAUNCHER = (
    Path(__file__).resolve().parents[2] / "androidworld" / "androidworld_service" / "serve.py"
)
#: Each folder of VenusBench's external files, as its settings name it.
LOCAL_FOLDERS = {
    "gui_browsing_video": "Video",
    "gui_browsing_p1": "p1",
    "gui_browsing_p2": "p2",
    "gui_browsing_p3": "p3",
    "gui_browsing_p4": "p4",
    "gui_browsing_figure1": "figure1",
    "gui_browsing_figure2": "figure2",
    "gui_browsing_bestbuy": "BestBuy",
    "zipxtract_zip": "zip",
    "simple_draw_guim": "GUIM",
}
#: The flag and thread of each background loop a GSATracking task starts.
BACKGROUND_THREADS = (("_stop_calling", "call_thread"), ("_stop_sending", "sms_thread"))
#: How long tear-down waits for a stopped thread: each looks at its flag at least
#: every 10 s (its pause between two calls or texts).
THREAD_JOIN_S = 15.0
#: Where its tasks put files on the device, as its own settings file says.
REMOTE_PATHS = {"gui_browsing": "/sdcard/GUIBrowsing", "test": "/sdcard/test", "sdcard": "/sdcard"}


def load(name: str, path: Path | str) -> ModuleType:
    """The Python file at *path*, run as the module *name*."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """What VenusBench's programs take beyond AndroidWorld's launcher's arguments."""
    parser.add_argument("--files", required=True, help="VenusBench's external files.")


def settings(src: str, files: str, adb: str) -> dict[str, object]:
    """VenusBench's settings for this run: the run's adb, the installation's files, no judge."""
    checkout_files = Path(src) / "venusbench_mobile_files"
    return {
        "adb_path": adb,
        "apk_root": str(checkout_files / "apk"),
        "local_paths": {
            **{key: str(Path(files) / folder) for key, folder in LOCAL_FOLDERS.items()},
            "gt_locating_screenshot": str(checkout_files / "gt_answer" / "locating_gt_screenshot"),
        },
        "remote_paths": dict(REMOTE_PATHS),
        "verification_model_url": "",
    }


def stop_background_threads(
    task_eval: type, *, warn: Callable[[str], object], join_s: float = THREAD_JOIN_S
) -> None:
    """Make *task_eval*'s ``tear_down`` stop a task's call or SMS thread before it runs.

    *task_eval* is VenusBench's ``TaskEval``, the class every task's
    ``tear_down`` reaches through ``super()``. For each background thread a
    task started (:data:`BACKGROUND_THREADS`), its flag is set, as the task's
    own ``is_successful`` sets it, and the thread is joined, at most *join_s*;
    *warn* hears a thread still running then. A task with no thread is torn
    down as before.
    """
    tear_down = task_eval.tear_down

    def tear_down_after_its_threads(self: object, env: object) -> object:
        for flag, name in BACKGROUND_THREADS:
            thread = getattr(self, name, None)
            if thread is None:
                continue
            setattr(self, flag, True)
            thread.join(timeout=join_s)
            if thread.is_alive():
                warn(f"{type(self).__name__}'s {name} still runs {join_s:g} s after it was stopped")
        return tear_down(self, env)

    task_eval.tear_down = tear_down_after_its_threads


def prepare(launcher: ModuleType, args: argparse.Namespace) -> None:
    """Enter the checkout, fill VenusBench's settings, and apply AndroidWorld's launcher's patches."""
    launcher.enter_checkout(args.src)
    from config import config  # VenusBench's settings singleton, in its checkout

    config._config = settings(args.src, args.files, args.adb)
    launcher.patch(args)


def main() -> None:
    launcher = load("androidworld_launcher", ANDROIDWORLD_LAUNCHER)
    parser = launcher.argument_parser("Serve VenusBench's tasks for one run.")
    add_arguments(parser)
    parser.add_argument("--service-app", required=True, help="AndroidWorld's app file.")
    args = parser.parse_args()
    prepare(launcher, args)
    android_server = load("android_server", args.service_app)
    # audio_recorder.py, imported with the app's registry, pointed it at the checkout.
    tempfile.tempdir = None
    from absl import logging
    from android_world.task_evals import task_eval

    stop_background_threads(
        task_eval.TaskEval, warn=lambda note: logging.warning("venusbench serve.py: %s", note)
    )
    launcher.serve(android_server, args.port)


if __name__ == "__main__":
    main()
