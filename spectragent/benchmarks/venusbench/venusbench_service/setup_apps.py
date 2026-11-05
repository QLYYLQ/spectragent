# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Install VenusBench's apps into its AVD and set the new ones up: its AVD's one-time setup.

Runs under VenusBench's interpreter, like ``serve.py`` beside it, against an
emulator booted writable from the AVD (a copy of AndroidWorld's set-up AVD,
``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案");
``spectragent benchmark setup venusbench`` names the steps::

    emulator -avd VenusBenchAvd -no-snapshot -port 5554 -grpc 8554
    <root>/.venv/bin/python -I setup_apps.py --src <root>/src --files <root>/files/files_gui_browsing \\
        --adb <adb> --adb-server-port 5037 --console-port 5554 --grpc-target localhost:8554 \\
        --forwarder-apk <root>/cache/a11y/<forwarder> --fitbook-apk <root>/cache/fitbook/<apk>

It connects as the service does (``serve.py``'s :func:`prepare`: VenusBench's
settings and AndroidWorld's launcher's patches, so no touch overlay is written
into the AVD and the adb server is never restarted), then:

1. installs the seven APKs VenusBench ships (``venusbench_mobile_files/apk/``)
   with android_env's flags (``adb install -r -t -g``); FitBook's is built for x86_64 only, so its
   arm64 build of the same release (``--fitbook-apk``) goes in its place.
   ADBKeyboard, the input method VenusBench's own agents type with, is
   installed and left unselected;
2. runs the fork's own ``setup.setup_app`` for each new app that has a setup
   (CalcYou, Pomodoro, Medilog, FitBook, ZipXtract, Calculator); Calculator's
   clears Simple Calculator's data, which is not installed, and fails without
   harm, as it did on the fork's own image.

The emulator is then shut down by hand (``adb emu kill`` once ``sync`` ran), so
the AVD keeps what was installed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

#: VenusBench's launcher, whose arguments and preparation this program takes.
VENUSBENCH_LAUNCHER = Path(__file__).resolve().parent / "serve.py"
#: The APK VenusBench ships for FitBook, built for x86_64 only.
FITBOOK_APK = "fitbook.apk"


def main() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("venusbench_launcher", VENUSBENCH_LAUNCHER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {VENUSBENCH_LAUNCHER}")
    venusbench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(venusbench)
    launcher = venusbench.load("androidworld_launcher", venusbench.ANDROIDWORLD_LAUNCHER)
    parser = launcher.argument_parser("Set VenusBench's apps up in its AVD, once.", serving=False)
    venusbench.add_arguments(parser)
    parser.add_argument("--fitbook-apk", required=True, help="FitBook's arm64 build.")
    args = parser.parse_args()
    venusbench.prepare(launcher, args)

    from absl import logging
    from android_world.env import adb_utils, env_launcher
    from android_world.env.setup_device import apps, setup

    tempfile.tempdir = None
    env = env_launcher.load_and_setup_env()
    try:
        shipped = Path(args.src) / "venusbench_mobile_files" / "apk"
        for apk in sorted(shipped.glob("*.apk")):
            path = Path(args.fitbook_apk) if apk.name == FITBOOK_APK else apk
            # android_env's own flags: replace, test builds, every runtime permission.
            adb_utils.issue_generic_request(
                ["install", "-r", "-t", "-g", str(path)], env.controller, timeout_sec=120.0
            )
            print(f"installed {path.name}", flush=True)
        for app in (
            apps.Calcyou,
            apps.Pomodoro,
            apps.Medilog,
            apps.Fitbook,
            apps.Zipxtract,
            apps.Calculator,
        ):
            try:
                setup.setup_app(app, env)
                print(f"set up {app.app_name}", flush=True)
            except Exception as exc:  # Calculator's clears an app that is not installed
                logging.warning("setup_apps.py: %s's setup failed: %s", app.app_name, exc)
        adb_utils.issue_generic_request(["shell", "sync"], env.controller)
    finally:
        env.close()


if __name__ == "__main__":
    main()
