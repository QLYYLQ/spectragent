"""``spectragent benchmark setup venusbench``: VenusBench's installation (``docs/architecture_design.md`` §4.10.1 "数据", §11.2).

The installation the ``venusbench`` setup component runs the service from
(``service.py``'s :class:`~spectragent.benchmarks.venusbench.service.VenusBenchInstallation`),
by default ``android/venusbench/installation`` in the shared cache:

1. ``src/``: VenusBench's repository at the pinned commit, that one commit
   only (a shallow fetch: its branch's history is not needed);
2. ``.venv/``: a CPython 3.11 venv synced to ``venusbench_service/requirements.lock.txt``
   (``uv pip sync``, as AndroidWorld's); the checkout is not installed into
   it: the launcher puts it on ``sys.path``, and its protocol buffers' code is
   committed in it;
3. ``server/android_server.py``: AndroidWorld's app file at the commit its
   launcher is written against, and ``cache/a11y/``, the accessibility
   forwarder, as AndroidWorld's installation caches it;
4. ``files/files_gui_browsing/``: VenusBench's files on Google Drive, each
   checked against the manifest that pins it (``files_gui_browsing.tsv``);
5. ``cache/fitbook/``: FitBook's arm64 build of the release VenusBench ships
   for x86_64 only, which the AVD's one-time setup installs in its place.

Each step is skipped when its result is in place. What it cannot install it
names, as AndroidWorld's installation does: ffmpeg on ``PATH``, and the AVD
the benchmark boots. VenusBench's AVD is AndroidWorld's set-up AVD with
VenusBench's apps added (``venusbench_service/setup_apps.py``, on a running
emulator, which this command does not start); without AndroidWorld's, it
names ``spectragent benchmark setup androidworld`` first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from spectragent.benchmarks.androidworld.install import (
    avd_missing,
    cache_forwarder,
    program,
    sync_venv,
)
from spectragent.benchmarks.androidworld.service import checkout_commit
from spectragent.benchmarks.data import (
    BenchmarkSetupError,
    Say,
    download_file,
    file_sha256,
    run_program,
)
from spectragent.benchmarks.venusbench.service import (
    SERVICE_APP_SHA256,
    SERVICE_APP_URL,
    VenusBenchInstallation,
    drive_files,
)

#: The packages the venv holds.
LOCK = Path(__file__).resolve().parent / "venusbench_service" / "requirements.lock.txt"
#: The program that sets VenusBench's apps up in its AVD, run under its interpreter.
SETUP_APPS = Path(__file__).resolve().parent / "venusbench_service" / "setup_apps.py"
#: FitBook's arm64 build of the release VenusBench ships (2.0.28), and its SHA-256.
FITBOOK_URL = (
    "https://github.com/brandonp2412/FitBook/releases/download/2.0.28/app-arm64-v8a-release.apk"
)
FITBOOK_SHA256 = "8cf0a00dff3d31df4baed230a8d3c669cc2c2d9db4bde67fa8ce51befc4f158b"
#: Where Google Drive serves a shared file by its id, without its page.
DRIVE_URL = "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"
#: The AVD the shipped androidworld benchmark boots, which VenusBench's starts from.
ANDROIDWORLD_AVD = "AndroidWorldAvd"
#: How to make VenusBench's AVD from AndroidWorld's set-up one.
AVD_STEPS = (
    "make it from AndroidWorld's set-up AVD: copy {home}/{androidworld}.avd to {home}/{avd}.avd "
    "(cp -cR shares its blocks on APFS) and write {home}/{avd}.ini as {androidworld}.ini is, "
    "naming the copy; boot it writable with gRPC (emulator -avd {avd} -no-snapshot -grpc 8554) and "
    "run {python} -I {setup_apps} --src {src} --files {files} --adb <the SDK's adb> "
    "--adb-server-port 5037 --console-port 5554 --grpc-target localhost:8554 --forwarder-apk "
    "{forwarder} --fitbook-apk {fitbook}, which installs VenusBench's apps and sets the new ones "
    "up; then adb shell sync and adb emu kill"
)


@dataclass(frozen=True)
class VenusBenchInstaller:
    """VenusBench's installation at *installation*, and the AVD the benchmark boots, by name.

    Attributes:
        installation: Where it goes.
        avd: The AVD the benchmark block's ``local_emulator`` backend names;
            ``None`` when the block names none (the run configuration does).
        avd_home: That backend's ``avd_home``; ``None``: where the emulator looks.
    """

    installation: VenusBenchInstallation
    avd: str | None = None
    avd_home: str | None = None

    def problems(self) -> list[str]:
        found = self.installation.problems()
        home = None if self.avd is None else avd_missing(self.avd, self.avd_home)
        if home is None:
            return found
        if avd_missing(ANDROIDWORLD_AVD, self.avd_home) is not None:
            found.append(
                f"there is no AVD named {self.avd} in {home}, nor AndroidWorld's set-up AVD "
                f"{ANDROIDWORLD_AVD} it is made from: spectragent benchmark setup androidworld "
                f"names how to make that one first"
            )
            return found
        installation = self.installation
        steps = AVD_STEPS.format(
            home=home,
            androidworld=ANDROIDWORLD_AVD,
            avd=self.avd,
            python=installation.python,
            setup_apps=SETUP_APPS,
            src=installation.src,
            files=installation.files,
            forwarder=installation.forwarder_apk,
            fitbook=installation.fitbook_apk,
        )
        found.append(f"there is no AVD named {self.avd} in {home}: {steps}")
        return found

    def install(self, say: Say) -> None:
        installation = self.installation
        self._check_out(say)
        sync_venv(installation.python, LOCK, say, "VenusBench")
        _download(SERVICE_APP_URL, SERVICE_APP_SHA256, installation.service_app, say)
        cache_forwarder(installation.forwarder_apk, say)
        self._download_the_files(say)
        _download(FITBOOK_URL, FITBOOK_SHA256, installation.fitbook_apk, say)

    def _check_out(self, say: Say) -> None:
        src = self.installation.src
        code = self.installation.code
        git = program("git", code.name)
        if checkout_commit(src) == code.commit:
            say(f"VenusBench's checkout {src} is at {code.commit}")
            return
        if not src.exists():
            say(f"fetching {code.commit} of {code.repository} into {src}")
            src.mkdir(parents=True)
            run_program([git, "-C", src, "init", "--quiet"], "make VenusBench's checkout")
            run_program(
                [git, "-C", src, "remote", "add", "origin", code.repository],
                "name VenusBench's repository",
            )
        elif not (src / ".git").exists():
            raise BenchmarkSetupError(
                f"{src} is there and is no git checkout: move it away, and VenusBench is "
                f"fetched there"
            )
        else:
            say(f"fetching {code.commit} into {src}")
        run_program(
            [git, "-C", src, "fetch", "--quiet", "--depth", "1", "origin", code.commit],
            "fetch VenusBench's pinned commit",
        )
        run_program(
            [git, "-C", src, "-c", "advice.detachedHead=false", "checkout", "--quiet"]
            + ["--detach", code.commit],
            "check out VenusBench's pinned commit",
        )
        found = checkout_commit(src)
        if found != code.commit:
            raise BenchmarkSetupError(
                f"the checkout {src} is at {found} after checking out {code.commit}"
            )

    def _download_the_files(self, say: Say) -> None:
        files = drive_files()
        folder = self.installation.files
        missing = [
            item
            for item in files
            if not (folder / item.path).is_file()
            or (folder / item.path).stat().st_size != item.size
            or file_sha256(folder / item.path) != item.sha256
        ]
        if not missing:
            say(f"VenusBench's {len(files)} external files are in {folder}")
            return
        say(f"downloading {len(missing)} of VenusBench's {len(files)} external files into {folder}")
        for item in missing:
            _download(DRIVE_URL.format(id=item.drive_id), item.sha256, folder / item.path, say)


def _download(url: str, sha256: str, target: Path, say: Say) -> None:
    """Download *url* to *target*, checked against *sha256*, unless *target* already is that file."""
    if target.is_file() and file_sha256(target) == sha256:
        say(f"{target} is in place")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    download_file(url, sha256, partial, say)
    partial.replace(target)


__all__ = [
    "AVD_STEPS",
    "DRIVE_URL",
    "FITBOOK_SHA256",
    "FITBOOK_URL",
    "LOCK",
    "SETUP_APPS",
    "VenusBenchInstaller",
]
