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

:func:`prepare` is shared with ``setup_apps.py`` beside it.
"""

from __future__ import annotations

import argparse
import importlib.util
import tempfile
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
    launcher.serve(android_server, args.port)


if __name__ == "__main__":
    main()
