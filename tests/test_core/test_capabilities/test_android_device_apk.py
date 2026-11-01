"""The device-side APK's sidecar, and the one protocol constant both ends are built from.

``spectragent/core/capabilities/android/device/apk.py``: a run names an APK
path, and :meth:`ServiceApk.load` refuses one it cannot trust. The Gradle build
(``android/service/build.gradle``) reads ``PROTOCOL_VERSION`` from the host's
``channel.py``, so the two ends cannot be built from different versions.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.core.capabilities.android.device import channel
from spectragent.core.capabilities.android.device.apk import (
    BUILD_TOOLS,
    PACKAGE,
    TEST_KEY_CERTIFICATE_SHA256,
    ServiceApk,
    ServiceApkError,
    android_sdk,
    build,
    project_dir,
)
from spectragent.core.capabilities.android.device.channel import PROTOCOL_VERSION

APK_BYTES = b"PK\x03\x04 an APK"
#: The directory the build keeps it in.
BUILD = hashlib.sha256(APK_BYTES).hexdigest()[:16]


def _write(
    tmp_path: Path, *, data: bytes = APK_BYTES, changes: Mapping[str, JsonValue] | None = None
) -> Path:
    """An APK and its sidecar where the build keeps them: in the directory its SHA-256 names."""
    apk = tmp_path / BUILD / "spectragent-device.apk"
    apk.parent.mkdir(exist_ok=True)
    apk.write_bytes(data)
    sidecar: dict[str, JsonValue] = {
        "format": "spectragent-device-apk-2",
        "package": PACKAGE,
        "version_code": 3,
        "version_name": "3",
        "protocol": PROTOCOL_VERSION,
        "accessibility_tool": True,
        "sha256": hashlib.sha256(APK_BYTES).hexdigest(),
        "signing_certificate_sha256": TEST_KEY_CERTIFICATE_SHA256,
        **(changes or {}),
    }
    apk.with_suffix(".json").write_text(json.dumps(sidecar))
    return apk


def test_an_apk_whose_sidecar_matches_it_loads_with_what_the_sidecar_says(tmp_path: Path) -> None:
    apk = ServiceApk.load(_write(tmp_path))

    assert apk.path == tmp_path / BUILD / "spectragent-device.apk"
    assert (apk.info.version_code, apk.info.accessibility_tool, apk.info.protocol) == (
        3,
        True,
        PROTOCOL_VERSION,
    )
    assert apk.signed_with_test_key


def test_an_apk_signed_with_a_key_of_ones_own_is_not_the_test_keys(tmp_path: Path) -> None:
    apk = ServiceApk.load(_write(tmp_path, changes={"signing_certificate_sha256": "ab" * 32}))

    assert not apk.signed_with_test_key


def test_a_missing_apk_is_refused_with_how_to_build_one(tmp_path: Path) -> None:
    with pytest.raises(ServiceApkError, match="no device-side APK at .* build one with `uv run"):
        ServiceApk.load(tmp_path / "nothing.apk")


def test_an_apk_without_its_sidecar_is_refused(tmp_path: Path) -> None:
    apk = _write(tmp_path)
    apk.with_suffix(".json").unlink()

    with pytest.raises(ServiceApkError, match="has no sidecar"):
        ServiceApk.load(apk)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format": "something-else"}, "is not an APK's sidecar"),
        ({"format": "spectragent-device-apk-1"}, "is not an APK's sidecar"),
        ({"surprise": 1}, "is not an APK's sidecar"),
        ({"protocol": PROTOCOL_VERSION + 1}, f"speaks protocol {PROTOCOL_VERSION + 1}"),
        ({"package": "com.example.other"}, "is com.example.other"),
    ],
)
def test_a_sidecar_that_does_not_fit_this_host_is_refused(
    tmp_path: Path, changes: dict[str, JsonValue], message: str
) -> None:
    with pytest.raises(ServiceApkError, match=message):
        ServiceApk.load(_write(tmp_path, changes=changes))


def test_an_apk_outside_the_directory_its_build_names_is_refused(tmp_path: Path) -> None:
    """A run records only the APK's path; the path names the build through that directory."""
    built = _write(tmp_path)
    moved = tmp_path / "elsewhere" / built.name
    moved.parent.mkdir()
    for path in (built, built.with_suffix(".json")):
        path.rename(moved.parent / path.name)

    with pytest.raises(ServiceApkError, match=f"not in the directory its build names .{BUILD}."):
        ServiceApk.load(moved)


def test_an_apk_that_is_not_the_build_its_sidecar_describes_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ServiceApkError, match="is not the build its sidecar describes"):
        ServiceApk.load(_write(tmp_path, data=b"PK\x03\x04 another build"))


def test_a_gradle_argument_that_would_contradict_the_sidecar_is_refused(tmp_path: Path) -> None:
    """The sidecar records isAccessibilityTool from the build's own option; a Gradle property
    set behind its back would build the other one."""
    with pytest.raises(ServiceApkError, match="would set what the sidecar records"):
        build(out=tmp_path, gradle_args=["-Pspectragent.accessibilityTool=true"])


def test_a_keystore_without_its_alias_or_password_is_refused(tmp_path: Path) -> None:
    with pytest.raises(
        ServiceApkError, match="needs --key-alias and SPECTRAGENT_KEYSTORE_PASSWORD"
    ):
        build(out=tmp_path, keystore=tmp_path / "own.p12", env={})


def test_the_build_hands_gradle_the_sdk_where_android_studio_puts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no SDK variable set this module reads the SDK where Android Studio puts it, a place
    Gradle never looks; the build hands Gradle that SDK."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sdk = android_sdk({})
    assert sdk.is_relative_to(tmp_path)
    sdk.mkdir(parents=True)
    handed: list[str | None] = []

    def gradle(
        command: list[str], *, env: Mapping[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        handed.append(env.get("ANDROID_HOME"))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", gradle)
    with pytest.raises(ServiceApkError, match="the Gradle build failed"):
        build(out=tmp_path / "cache", env={})
    assert handed == [str(sdk)]


def test_the_build_drops_a_second_sdk_variable_gradle_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gradle refuses ANDROID_HOME and ANDROID_SDK_ROOT naming different SDKs; the build keeps
    the one this module reads, ANDROID_HOME's."""
    home_sdk, root_sdk = tmp_path / "home-sdk", tmp_path / "root-sdk"
    home_sdk.mkdir()
    root_sdk.mkdir()
    handed: list[dict[str, str]] = []

    def gradle(
        command: list[str], *, env: Mapping[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        handed.append({k: v for k, v in env.items() if k in ("ANDROID_HOME", "ANDROID_SDK_ROOT")})
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", gradle)
    with pytest.raises(ServiceApkError, match="the Gradle build failed"):
        build(
            out=tmp_path / "cache",
            env={"ANDROID_HOME": str(home_sdk), "ANDROID_SDK_ROOT": str(root_sdk)},
        )
    assert handed == [{"ANDROID_HOME": str(home_sdk)}]


def test_a_build_without_an_android_sdk_is_refused_before_gradle_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gradle(*_: object, **__: object) -> None:
        raise AssertionError("Gradle ran without an Android SDK")

    monkeypatch.setattr(subprocess, "run", gradle)
    with pytest.raises(ServiceApkError, match="no Android SDK at"):
        build(out=tmp_path / "cache", env={"ANDROID_HOME": str(tmp_path / "missing")})


def test_the_build_tools_that_read_the_signature_are_the_ones_the_project_builds_with() -> None:
    build_file = (project_dir() / "build.gradle").read_text()

    assert f'buildToolsVersion "{BUILD_TOOLS}"' in build_file


def test_the_gradle_build_reads_the_protocol_version_from_the_hosts_channel_module() -> None:
    build = (project_dir() / "build.gradle").read_text()
    path = re.search(r'def channelModule = file\("([^"]+)"\)', build)
    pattern = re.search(r"channelModule\.text =~ /(.+)/\n", build)
    assert path is not None and pattern is not None

    assert (project_dir() / path[1]).resolve() == Path(channel.__file__).resolve()
    found = re.search(pattern[1], Path(channel.__file__).read_text())
    assert found is not None and int(found[1]) == PROTOCOL_VERSION
