"""The device-side APK: how a build is made, what its sidecar says, and how a run checks one.

``docs/architecture_design.md`` §11.2 ("设备端", "实验身份"). The APK is built
from ``android/service/`` at the root of the repository, a Gradle project, by
:func:`build`::

    uv run python -m spectragent.core.capabilities.android.device.apk build \\
        [--accessibility-tool] [--out DIR] [-- GRADLE_ARGS...]

The build needs JDK 17 (``JAVA_HOME``) and the Android SDK with platform 33 and
build-tools 34.0.0 (``ANDROID_HOME`` or ``ANDROID_SDK_ROOT``, else where
Android Studio puts it, :func:`android_sdk`); Gradle arguments after ``--`` reach
Gradle as they are (a proxy: ``-Dhttps.proxyHost=... -Dhttps.proxyPort=...``).
It copies the APK, with a sidecar beside it, into a directory of its own named
by the APK's SHA-256, under a cache directory outside the repository
(``$XDG_CACHE_HOME/spectragent/android-service``, else
``~/.cache/spectragent/android-service``), and prints the APK's path. A run's
configuration names that path. Build outputs stay out of the repository: one
commit builds the same APK on every machine (the signing key is committed,
``android/service/build.gradle``), so its SHA-256 identifies it.

**Signing.** The committed key is the default, for emulators, CI and tests.
Anyone can sign with it, so an APK it signed must never reach a real device:
whoever reaches that device's adb could install a build of their own over
ours, and it would keep our accessibility grant. A build for a real device is
signed with a keystore kept outside the repository: ``--keystore PATH
--key-alias ALIAS``, the passwords in ``SPECTRAGENT_KEYSTORE_PASSWORD`` and
``SPECTRAGENT_KEY_PASSWORD`` (the store's when unset), from the environment
only, never a command line. The provider for connected devices, when it
exists, refuses an APK whose :attr:`ServiceApk.signed_with_test_key` is true.

The sidecar (``<apk stem>.json``) records what the run's identity needs: the
SHA-256, the versions, the protocol, ``isAccessibilityTool`` and the SHA-256 of
the signing certificate, read from the APK with ``apksigner``.
:meth:`ServiceApk.load` refuses an APK without one, an APK whose bytes are not
the ones the sidecar describes, and one built for another protocol than this
host speaks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
#: The build tools the Gradle project builds with (``buildToolsVersion``), whose
#: ``apksigner`` reads the signing certificate.
BUILD_TOOLS = "34.0.0"
#: The SHA-256 of the committed test key's certificate (``android/service/signing/``).
TEST_KEY_CERTIFICATE_SHA256 = "9d148d72d24c2b08d17a64c92a4cac5ca15ca5bff41467b32931834a1a4810b3"
_SIDECAR_FORMAT = "spectragent-device-apk-2"
#: Where apksigner prints the first signer's certificate digest.
_CERTIFICATE = re.compile(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})")


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
        signing_certificate_sha256: The SHA-256 of the certificate it is signed
            with, read from the APK.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["spectragent-device-apk-2"]
    package: str
    version_code: int
    version_name: str
    protocol: int
    accessibility_tool: bool
    sha256: str
    signing_certificate_sha256: str


@dataclass(frozen=True)
class ServiceApk:
    """An APK and its sidecar, checked against each other and against this host."""

    path: Path
    info: ServiceApkInfo

    @property
    def signed_with_test_key(self) -> bool:
        """Whether the committed test key signed it: never for a real device (module docstring)."""
        return self.info.signing_certificate_sha256 == TEST_KEY_CERTIFICATE_SHA256

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
    keystore: Path | None = None,
    key_alias: str | None = None,
    gradle_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> ServiceApk:
    """Build the APK with Gradle, copy it and its sidecar into the cache, and return it.

    Signed with the committed test key unless *keystore* (with *key_alias*)
    names one outside the repository (module docstring). Gradle's own output
    goes to this process's stdout and stderr. Its daemon is not left running.

    Raises:
        ServiceApkError: A Gradle argument would set what the sidecar records,
            a keystore lacks its alias or password, the Gradle project is not
            there (an installed package carries no APK sources), there is no
            Android SDK where :func:`android_sdk` looks, or the build failed.
    """
    environment = dict(os.environ if env is None else env)
    overriding = [arg for arg in gradle_args if arg.startswith("-Pspectragent.")]
    if overriding:
        raise ServiceApkError(
            f"{' '.join(overriding)} would set what the sidecar records; use this command's "
            "own options"
        )
    for name in ("SPECTRAGENT_KEYSTORE", "SPECTRAGENT_KEY_ALIAS"):
        environment.pop(name, None)  # only the options choose the key
    if keystore is not None:
        if not key_alias or not environment.get("SPECTRAGENT_KEYSTORE_PASSWORD"):
            raise ServiceApkError(
                "a keystore needs --key-alias and SPECTRAGENT_KEYSTORE_PASSWORD in the "
                "environment (and SPECTRAGENT_KEY_PASSWORD when the key's differs)"
            )
        environment.update(
            SPECTRAGENT_KEYSTORE=str(keystore.expanduser().resolve()),
            SPECTRAGENT_KEY_ALIAS=key_alias,
        )
    project = project_dir()
    gradlew = project / "gradlew"
    if not gradlew.is_file():
        raise ServiceApkError(
            f"the APK's sources are not at {project}: an installed package does not carry them; "
            "build from a checkout of the repository"
        )
    # Gradle never looks where Android Studio puts the SDK, and refuses two SDK
    # variables that name different SDKs; it gets the one SDK this module reads.
    sdk = android_sdk(environment)
    if not sdk.is_dir():
        raise ServiceApkError(
            f"no Android SDK at {sdk}: set ANDROID_HOME to an SDK with platform 33 and "
            f"build-tools {BUILD_TOOLS}"
        )
    environment["ANDROID_HOME"] = str(sdk)
    environment.pop("ANDROID_SDK_ROOT", None)
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
        *gradle_args,
        f"-Pspectragent.accessibilityTool={flag}",
        "assembleRelease",
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
        signing_certificate_sha256=_signing_certificate(apk, environment),
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


def android_sdk(environment: Mapping[str, str]) -> Path:
    """The Android SDK: ``ANDROID_HOME`` or ``ANDROID_SDK_ROOT``, else where Android Studio puts it."""
    sdk = environment.get("ANDROID_HOME") or environment.get("ANDROID_SDK_ROOT")
    default = "Library/Android/sdk" if sys.platform == "darwin" else "Android/Sdk"
    return Path(sdk) if sdk else Path.home() / default


def _signing_certificate(apk: Path, environment: Mapping[str, str]) -> str:
    """The SHA-256 of the certificate that signed *apk*, as the SDK's apksigner reads it."""
    apksigner = android_sdk(environment) / "build-tools" / BUILD_TOOLS / "apksigner"
    result = subprocess.run(
        [str(apksigner), "verify", "--print-certs", str(apk)],
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    found = _CERTIFICATE.search(result.stdout)
    if result.returncode != 0 or found is None:
        raise ServiceApkError(
            f"{apksigner} could not read the signature of {apk}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return found[1]


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
    build_command.add_argument(
        "--keystore",
        type=Path,
        help=(
            "sign with this keystore, kept outside the repository, instead of the committed "
            "test key; its password comes from SPECTRAGENT_KEYSTORE_PASSWORD"
        ),
    )
    build_command.add_argument("--key-alias", help="the key's alias in --keystore")
    build_command.add_argument("gradle_args", nargs="*", help="arguments for Gradle, after --")
    args = parser.parse_args(argv)
    try:
        apk = build(
            accessibility_tool=args.accessibility_tool,
            out=args.out,
            keystore=args.keystore,
            key_alias=args.key_alias,
            gradle_args=args.gradle_args,
        )
    except ServiceApkError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "apk": str(apk.path),
                **apk.info.model_dump(),
                "signed_with_test_key": apk.signed_with_test_key,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
