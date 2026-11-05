"""VenusBench's installation: its checkout, its venv, AndroidWorld's service app and its external files.

``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案". VenusBench
is a fork of AndroidWorld made before AndroidWorld had a service, so its tasks
are served by AndroidWorld's own app (``server/android_server.py`` at the
commit the AndroidWorld launcher is written against), run under VenusBench's
interpreter against VenusBench's checkout by VenusBench's launcher
(``venusbench_service/serve.py``), which loads the AndroidWorld launcher by its
path and runs its steps. Everything else about the service (its process
group, its port, its calls) is AndroidWorld's (``androidworld/service.py``).

The installation (``spectragent benchmark setup venusbench``, ``install.py``)
lays out, under its root:

- ``.venv/``, ``src/`` and ``cache/a11y/`` as AndroidWorld's do, ``src/`` at
  :data:`~spectragent.benchmarks.venusbench.tasks.VENUSBENCH`'s commit;
- ``server/android_server.py``: AndroidWorld's app file at the pinned
  AndroidWorld commit, checked against its SHA-256 (:data:`SERVICE_APP_SHA256`);
- ``files/files_gui_browsing/``: the files VenusBench keeps on Google Drive,
  which three of its first version's tasks push to the device, each as the
  pinned manifest records it (``files_gui_browsing.tsv``);
- ``cache/fitbook/``: FitBook's arm64 build, which only its AVD's one-time
  setup installs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from spectragent.benchmarks.androidworld.service import PINNED_COMMIT as ANDROIDWORLD_COMMIT
from spectragent.benchmarks.androidworld.service import Installation, PinnedCode
from spectragent.benchmarks.data import file_sha256
from spectragent.benchmarks.venusbench.tasks import VENUSBENCH

_HERE = Path(__file__).resolve().parent
#: VenusBench's launcher, run by VenusBench's interpreter; Spectragent never imports it.
LAUNCHER = _HERE / "venusbench_service" / "serve.py"
#: AndroidWorld's app file the service runs, its URL at the pinned AndroidWorld
#: commit, and its SHA-256 there (git's blob hash for it: 583b40e7f0472edac2af2645a9684a3462fa0664).
SERVICE_APP = "server/android_server.py"
SERVICE_APP_URL = (
    f"https://raw.githubusercontent.com/google-research/android_world/{ANDROIDWORLD_COMMIT}/"
    f"{SERVICE_APP}"
)
SERVICE_APP_SHA256 = "1a4a66a1b7e1128c9cc89bc3b2ebfb59fe71d83dffc7238bfea4e62e7fe7f0b9"
#: The pinned manifest of VenusBench's files on Google Drive.
FILES_MANIFEST = _HERE / "files_gui_browsing.tsv"
#: The folder those files go in, as VenusBench's settings name it.
FILES_FOLDER = "files_gui_browsing"


@dataclass(frozen=True)
class DriveFile:
    """One of VenusBench's files on Google Drive, as the manifest pins it."""

    sha256: str
    size: int
    drive_id: str
    path: str


def drive_files() -> list[DriveFile]:
    """Every file the manifest pins, in its order."""
    files: list[DriveFile] = []
    for line in FILES_MANIFEST.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        sha256, size, drive_id, path = line.split("\t")
        files.append(DriveFile(sha256, int(size), drive_id, path))
    return files


@dataclass(frozen=True)
class VenusBenchInstallation(Installation):
    """A VenusBench installation, laid out as the ``venusbench`` setup's ``root`` describes."""

    code: ClassVar[PinnedCode] = VENUSBENCH
    launcher: ClassVar[Path] = LAUNCHER
    checkout_file: ClassVar[str] = "android_world/registry.py"

    @property
    def service_app(self) -> Path:
        return self.root / SERVICE_APP

    @property
    def files(self) -> Path:
        return self.root / "files" / FILES_FOLDER

    @property
    def fitbook_apk(self) -> Path:
        return self.root / "cache" / "fitbook" / "app-arm64-v8a-release.apk"

    def launcher_arguments(self) -> list[str]:
        """AndroidWorld's app file, and where VenusBench's external files are."""
        return ["--service-app", str(self.service_app), "--files", str(self.files)]

    def own_problems(self) -> list[str]:
        """AndroidWorld's app file at its pinned commit, and every external file at its pinned size."""
        found: list[str] = []
        if not self.service_app.is_file():
            found.append(f"there is no AndroidWorld service app at {self.service_app}")
        elif file_sha256(self.service_app) != SERVICE_APP_SHA256:
            found.append(
                f"{self.service_app} is not AndroidWorld's {SERVICE_APP} at {ANDROIDWORLD_COMMIT}"
            )
        missing = [
            item.path
            for item in drive_files()
            if not ((self.files / item.path).is_file())
            or (self.files / item.path).stat().st_size != item.size
        ]
        if missing:
            found.append(
                f"{len(missing)} of VenusBench's external files are missing from {self.files} or "
                f"have another size (the first: {missing[0]})"
            )
        return found


__all__ = [
    "FILES_FOLDER",
    "FILES_MANIFEST",
    "LAUNCHER",
    "SERVICE_APP",
    "SERVICE_APP_SHA256",
    "SERVICE_APP_URL",
    "DriveFile",
    "VenusBenchInstallation",
    "drive_files",
]
