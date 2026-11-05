"""``spectragent benchmark setup androidworld``: AndroidWorld's installation (``docs/architecture_design.md`` §4.10.1 "数据", §11.2).

The installation the setup component runs AndroidWorld's service from
(``service.py``'s :class:`~spectragent.benchmarks.androidworld.service.Installation`),
by default ``android/androidworld/installation`` in the shared cache:

1. ``src/``: AndroidWorld's repository at the pinned commit, that one commit
   fetched (:func:`check_out`, shared with VenusBench's installation);
2. ``.venv/``: a CPython 3.11 venv made by ``uv``, synced to the packages
   of ``androidworld_service/requirements.lock.txt`` (``uv pip sync``: what
   the lock lists is installed at its version, and anything else removed),
   then the checkout itself, editable and without its dependencies (that
   build compiles its two protocol buffers into ``src/``); so after every
   setup the venv holds the lock and the checkout, nothing more;
3. ``cache/a11y/``: the accessibility forwarder android_env installs on the
   device, downloaded once and checked against its SHA-256, so the service
   need not download it at every start.

Each step is skipped when its result is in place. What it cannot install it
names: ffmpeg on ``PATH`` (AndroidWorld makes its music tasks' audio with it),
and the AVD the benchmark boots, which AndroidWorld's own one-time setup
prepares on a running emulator (this command starts none).

VenusBench's installation (``spectragent/benchmarks/venusbench/install.py``)
shares the steps that do not depend on AndroidWorld's checkout:
:func:`check_out`, :func:`program`, :func:`sync_venv`, :func:`cache_forwarder`,
:func:`download_to`, :func:`avd_missing` and :func:`installation_of`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, JsonValue

from spectragent.benchmarks.androidworld.service import (
    FORWARDER_APK,
    Installation,
    PinnedCode,
    checkout_commit,
)
from spectragent.benchmarks.data import (
    BenchmarkSetupError,
    Say,
    download_file,
    file_sha256,
    run_program,
)
from spectragent.core.capabilities.android.backends.local_emulator import find_avd_home

#: An installation of a benchmark that runs AndroidWorld's code.
Kind = TypeVar("Kind", bound=Installation)
#: The packages the venv holds.
LOCK = Path(__file__).resolve().parent / "androidworld_service" / "requirements.lock.txt"
#: The Python AndroidWorld runs on (its pyproject.toml: ``>=3.11``; its Dockerfile: 3.11).
PYTHON = "3.11"
#: Where android_env downloads the forwarder from, and its SHA-256 (measured 2026-11-01).
FORWARDER_URL = f"https://storage.googleapis.com/android_env-tasks/{FORWARDER_APK}"
FORWARDER_SHA256 = "97a56a544e44d79f9b3181fc7dbdd72cffa908efd3d53c82afad1773061a350a"
#: How to prepare the AVD, which needs a running emulator and a writable boot.
AVD_STEPS = (
    "create it as AndroidWorld's README says (Pixel 6, API 33, Google APIs; on Apple silicon "
    "the arm64-v8a image) and set hw.keyboard=yes in its config.ini, which the one-time setup "
    "needs; then boot it writable with gRPC (emulator -avd {avd} -no-snapshot -grpc 8554) and "
    "run AndroidWorld's one-time setup from {src} with {python} (run.py --perform_emulator_setup, "
    "README.md there), which installs its apps and their permissions into the AVD"
)


@dataclass(frozen=True)
class AndroidWorldInstallation:
    """AndroidWorld's installation at *installation*, and the AVD the benchmark boots, by name.

    Attributes:
        installation: Where it goes.
        avd: The AVD the benchmark block's ``local_emulator`` backend names;
            ``None`` when the block names none (the run configuration does).
        avd_home: That backend's ``avd_home``; ``None``: where the emulator looks.
    """

    installation: Installation
    avd: str | None = None
    avd_home: str | None = None

    def problems(self) -> list[str]:
        found = self.installation.problems()
        if self.avd is not None:
            home = avd_missing(self.avd, self.avd_home)
            if home is not None:
                steps = AVD_STEPS.format(
                    avd=self.avd, src=self.installation.src, python=self.installation.python
                )
                found.append(f"there is no AVD named {self.avd} in {home}: {steps}")
        return found

    def install(self, say: Say) -> None:
        check_out(self.installation.src, self.installation.code, say)
        sync_venv(self.installation.python, LOCK, say, "AndroidWorld")
        run_program(
            [
                program("uv", "AndroidWorld"),
                "pip",
                "install",
                "--python",
                self.installation.python,
                "--no-deps",
                "-e",
                self.installation.src,
            ],
            "install AndroidWorld's checkout",
        )
        cache_forwarder(self.installation.forwarder_apk, say)


def check_out(src: Path, code: PinnedCode, say: Say) -> None:
    """Check *code*'s pinned commit out at *src*, fetching that one commit; nothing when it is there.

    A new checkout is made empty and fetches the commit alone (``--depth 1``:
    the branch's history is not needed); a checkout at another commit fetches
    the pinned one the same way. A directory there that is no git checkout is
    left alone.

    Raises:
        BenchmarkSetupError: Git is not on PATH, the directory is no checkout,
            or the checkout is not at the commit afterwards.
        ProgramError: A git command failed.
    """
    git = program("git", code.name)
    if checkout_commit(src) == code.commit:
        say(f"{code.name}'s checkout {src} is at {code.commit}")
        return
    if not src.exists():
        say(f"fetching {code.commit} of {code.repository} into {src}")
        src.mkdir(parents=True)
        run_program([git, "-C", src, "init", "--quiet"], f"make {code.name}'s checkout")
        run_program(
            [git, "-C", src, "remote", "add", "origin", code.repository],
            f"name {code.name}'s repository",
        )
    elif not (src / ".git").exists():
        raise BenchmarkSetupError(
            f"{src} is there and is no git checkout: move it away, and {code.name} is fetched there"
        )
    else:
        say(f"fetching {code.commit} into {src}")
    run_program(
        [git, "-C", src, "fetch", "--quiet", "--depth", "1", "origin", code.commit],
        f"fetch {code.name}'s pinned commit",
    )
    run_program(
        [git, "-C", src, "-c", "advice.detachedHead=false", "checkout", "--quiet"]
        + ["--detach", code.commit],
        f"check out {code.name}'s pinned commit",
    )
    found = checkout_commit(src)
    if found != code.commit:
        raise BenchmarkSetupError(
            f"the checkout {src} is at {found} after checking out {code.commit}"
        )


def sync_venv(python: Path, lock: Path, say: Say, name: str) -> None:
    """Make a CPython :data:`PYTHON` venv for *python* unless there is one, and sync it to *lock*.

    ``uv pip sync``: what the lock lists is installed at its version, and
    anything else removed. *name* is what messages call the code it runs.
    """
    uv = program("uv", name)
    venv = python.parent.parent
    if not python.is_file():
        say(f"making a Python {PYTHON} venv at {venv}")
        run_program([uv, "venv", "--quiet", "--python", PYTHON, venv], f"make {name}'s venv")
    say(f"syncing {name}'s dependencies in {venv} to {lock.name}")
    run_program([uv, "pip", "sync", "--python", python, lock], f"install {name}'s dependencies")


def cache_forwarder(target: Path, say: Say) -> None:
    """Download the accessibility forwarder android_env installs to *target*, unless it is there."""
    download_to(FORWARDER_URL, FORWARDER_SHA256, target, say)


def download_to(url: str, sha256: str, target: Path, say: Say) -> None:
    """Download *url* to *target*, checked against *sha256*, unless *target* already is that file.

    Written beside *target* first and moved into place, so an interrupted
    download leaves no file that looks installed.
    """
    if target.is_file() and file_sha256(target) == sha256:
        say(f"{target} is in place")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    download_file(url, sha256, partial, say)
    partial.replace(target)


def installation_of(config: BaseModel, kind: type[Kind]) -> Kind:
    """The installation of *kind* a benchmark block's setup component looks for: at its ``root``, else the default."""
    root: object = getattr(getattr(config, "setup", None), "root", None)
    if not (root is None or isinstance(root, str)):
        raise TypeError(f"the {kind.code.benchmark} setup's root is {root!r}")
    return kind.at(root)


def avd_missing(avd: str, avd_home: str | None) -> Path | None:
    """Where the emulator looks for AVDs (*avd_home*, else its default) when the AVD *avd* is not there; ``None`` when it is."""
    home = find_avd_home(avd_home, os.environ)
    return None if (home / f"{avd}.ini").is_file() else home


def program(name: str, installing: str) -> str:
    """The executable *name* on PATH, which installing *installing* needs.

    Raises:
        BenchmarkSetupError: There is none.
    """
    found = shutil.which(name)
    if found is None:
        raise BenchmarkSetupError(
            f"installing {installing} needs {name} on PATH, and there is none"
        )
    return found


def named_emulator(config: BaseModel) -> tuple[str | None, str | None]:
    """The AVD and ``avd_home`` a ``local_emulator`` backend names in the benchmark block *config*'s capability list."""
    blocks: object = getattr(config, "capability", ())
    if not isinstance(blocks, list | tuple):
        return None, None
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("capability_id") != "android":
            continue
        backend: JsonValue = block.get("backend")
        if isinstance(backend, Mapping) and backend.get("provider_id") == "local_emulator":
            avd, home = backend.get("avd"), backend.get("avd_home")
            return (
                avd if isinstance(avd, str) else None,
                home if isinstance(home, str) else None,
            )
    return None, None


__all__ = [
    "AVD_STEPS",
    "FORWARDER_SHA256",
    "FORWARDER_URL",
    "LOCK",
    "PYTHON",
    "AndroidWorldInstallation",
    "avd_missing",
    "cache_forwarder",
    "check_out",
    "download_to",
    "installation_of",
    "named_emulator",
    "program",
    "sync_venv",
]
