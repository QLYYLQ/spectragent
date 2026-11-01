"""The device-side APK: how a build is made, what its sidecar says, and how a run checks one.

``docs/architecture_design.md`` §11.2 ("设备端", "实验身份"). The APK is built
from ``android/service/`` at the root of the repository, a Gradle project, by
:func:`build`::

    uv run python -m spectragent.core.capabilities.android.device.apk build \\
        [--accessibility-tool] [--out DIR] [-- GRADLE_ARGS...]

The build needs JDK 17 (``JAVA_HOME``) and the Android SDK with platform 33 and
build-tools 34.0.0 (``ANDROID_HOME``); Gradle arguments after ``--`` reach
Gradle as they are (a proxy: ``-Dhttps.proxyHost=... -Dhttps.proxyPort=...``).
It copies the APK, with a sidecar beside it, into a directory of its own named
by the APK's SHA-256, under a cache directory outside the repository
(``$XDG_CACHE_HOME/spectragent/android-service``, else
``~/.cache/spectragent/android-service``), and prints the APK's path. A run's
configuration names that path. Build outputs stay out of the repository: one
commit builds the same APK on every machine (the signing key is committed,
``android/service/build.gradle``), so its SHA-256 identifies it.

The sidecar (``<apk stem>.json``) records what the run's identity needs: the
SHA-256, the versions, the protocol and ``isAccessibilityTool``.
:meth:`ServiceApk.load` refuses an APK without one, an APK whose bytes are not
the ones the sidecar describes, and one built for another protocol than this
host speaks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spectragent.core.capabilities.android.device.channel import PROTOCOL_VERSION

#: The APK's package.
PACKAGE = "org.spectragent.device"
#: The accessibility service, as ``enabled_accessibility_services`` names it.
SERVICE_COMPONENT = f"{PACKAGE}/.DeviceService"
#: The shell helper's main class, which ``app_process`` runs.
HELPER_CLASS = f"{PACKAGE}.ShellHelper"
#: The APK's file name in the cache.
APK_NAME = "spectragent-device.apk"
_SIDECAR_FORMAT = "spectragent-device-apk-1"


class ServiceApkError(ValueError):
    """An APK a run cannot use, or a build that failed; the message says what to do."""


class ServiceApkInfo(BaseModel):
    """What the sidecar says about its APK.

    Attributes:
        format: The sidecar's own format.
        package: The APK's package.
        version_code: Its ``versionCode``.
        version_name: Its ``versionName``.
        protocol: The channel protocol it was built for.
        accessibility_tool: Its ``isAccessibilityTool``, set when it was built.
        sha256: The SHA-256 of the APK file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["spectragent-device-apk-1"]
    package: str
    version_code: int
    version_name: str
    protocol: int
    accessibility_tool: bool
    sha256: str


@dataclass(frozen=True)
class ServiceApk:
    """An APK and its sidecar, checked against each other and against this host."""

    path: Path
    info: ServiceApkInfo

    @classmethod
    def load(cls, path: str | Path) -> ServiceApk:
        """The APK at *path* with its sidecar, once both check out.

        Raises:
            ServiceApkError: There is no APK or no sidecar at *path*, the
                sidecar is not one, the APK's bytes are not the ones the
                sidecar describes, or it was built for another protocol or
                package.
        """
        apk = Path(path).expanduser()
        rebuild = f"build one with `uv run python -m {__name__} build`"
        if not apk.is_file():
            raise ServiceApkError(f"there is no device-side APK at {apk}; {rebuild}")
        sidecar = apk.with_suffix(".json")
        try:
            info = ServiceApkInfo.model_validate_json(sidecar.read_bytes())
        except FileNotFoundError:
            raise ServiceApkError(
                f"the APK at {apk} has no sidecar ({sidecar.name}), which says what it is; {rebuild}"
            ) from None
        except ValidationError as exc:
            raise ServiceApkError(f"{sidecar} is not an APK's sidecar ({exc}); {rebuild}") from None
        sha256 = _sha256(apk)
        if sha256 != info.sha256:
            raise ServiceApkError(
                f"the APK at {apk} is not the build its sidecar describes (SHA-256 {sha256}, the "
                f"sidecar says {info.sha256}); {rebuild}"
            )
        if info.package != PACKAGE:
            raise ServiceApkError(f"the APK at {apk} is {info.package}, not {PACKAGE}")
        if info.protocol != PROTOCOL_VERSION:
            raise ServiceApkError(
                f"the APK at {apk} speaks protocol {info.protocol} and this host speaks "
                f"{PROTOCOL_VERSION}; {rebuild} from this checkout"
            )
        return cls(apk, info)


def project_dir() -> Path:
    """The APK's Gradle project, ``android/service/`` at the root of the repository."""
    return Path(__file__).resolve().parents[5] / "android" / "service"


def default_cache_dir(env: Mapping[str, str]) -> Path:
    """Where :func:`build` puts APKs unless told: under ``$XDG_CACHE_HOME``, else ``~/.cache``."""
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "spectragent" / "android-service"


def build(
    *,
    accessibility_tool: bool = False,
    out: Path | None = None,
    gradle_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> ServiceApk:
    """Build the APK with Gradle, copy it and its sidecar into the cache, and return it.

    Gradle's own output goes to this process's stdout and stderr. Its daemon is
    not left running.

    Raises:
        ServiceApkError: The Gradle project is not there (an installed package
            carries no APK sources), or the build failed.
    """
    environment = dict(os.environ if env is None else env)
    project = project_dir()
    gradlew = project / "gradlew"
    if not gradlew.is_file():
        raise ServiceApkError(
            f"the APK's sources are not at {project}: an installed package does not carry them; "
            "build from a checkout of the repository"
        )
    cache = (out or default_cache_dir(environment)).expanduser().resolve()
    # The Android Gradle plugin writes into ANDROID_USER_HOME (~/.android by
    # default), next to the user's adb keys; it gets a directory of its own.
    environment["ANDROID_USER_HOME"] = str(cache / "android-user-home")
    flag = "true" if accessibility_tool else "false"
    command = [
        str(gradlew),
        "--no-daemon",
        "-p",
        str(project),
        f"-Pspectragent.accessibilityTool={flag}",
        "assembleRelease",
        *gradle_args,
    ]
    if subprocess.run(command, env=environment, check=False).returncode != 0:
        raise ServiceApkError(f"the Gradle build failed: {' '.join(command)}")
    outputs = project / "build" / "outputs" / "apk" / "release"
    metadata = _OutputMetadata.model_validate_json((outputs / "output-metadata.json").read_bytes())
    (element,) = metadata.elements
    built = outputs / element.output_file
    sha256 = _sha256(built)
    directory = cache / sha256[:16]
    directory.mkdir(parents=True, exist_ok=True)
    apk = directory / APK_NAME
    shutil.copyfile(built, apk)
    info = ServiceApkInfo(
        format=_SIDECAR_FORMAT,
        package=metadata.application_id,
        version_code=element.version_code,
        version_name=element.version_name,
        protocol=PROTOCOL_VERSION,
        accessibility_tool=accessibility_tool,
        sha256=sha256,
    )
    apk.with_suffix(".json").write_text(info.model_dump_json(indent=2) + "\n")
    return ServiceApk.load(apk)


class _OutputElement(BaseModel):
    output_file: str = Field(alias="outputFile")
    version_code: int = Field(alias="versionCode")
    version_name: str = Field(alias="versionName")


class _OutputMetadata(BaseModel):
    """The ``output-metadata.json`` the Android Gradle plugin writes beside an APK (its other keys ignored)."""

    application_id: str = Field(alias="applicationId")
    elements: tuple[_OutputElement, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"python -m {__name__}", description="Build Spectragent's device-side APK."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser(
        "build", help="build the APK and print the path a run's configuration names"
    )
    build_command.add_argument(
        "--accessibility-tool",
        action="store_true",
        help="declare isAccessibilityTool (false unless given)",
    )
    build_command.add_argument(
        "--out", type=Path, help="the cache directory (default: ~/.cache/spectragent/...)"
    )
    build_command.add_argument("gradle_args", nargs="*", help="arguments for Gradle, after --")
    args = parser.parse_args(argv)
    try:
        apk = build(
            accessibility_tool=args.accessibility_tool, out=args.out, gradle_args=args.gradle_args
        )
    except ServiceApkError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps({"apk": str(apk.path), **apk.info.model_dump()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
