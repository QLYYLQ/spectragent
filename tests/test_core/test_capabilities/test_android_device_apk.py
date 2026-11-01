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
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.core.capabilities.android.device import channel
from spectragent.core.capabilities.android.device.apk import (
    PACKAGE,
    ServiceApk,
    ServiceApkError,
    project_dir,
)
from spectragent.core.capabilities.android.device.channel import PROTOCOL_VERSION

APK_BYTES = b"PK\x03\x04 an APK"


def _write(
    tmp_path: Path, *, data: bytes = APK_BYTES, changes: Mapping[str, JsonValue] | None = None
) -> Path:
    apk = tmp_path / "spectragent-device.apk"
    apk.write_bytes(data)
    sidecar: dict[str, JsonValue] = {
        "format": "spectragent-device-apk-1",
        "package": PACKAGE,
        "version_code": 3,
        "version_name": "3",
        "protocol": PROTOCOL_VERSION,
        "accessibility_tool": True,
        "sha256": hashlib.sha256(APK_BYTES).hexdigest(),
        **(changes or {}),
    }
    apk.with_suffix(".json").write_text(json.dumps(sidecar))
    return apk


def test_an_apk_whose_sidecar_matches_it_loads_with_what_the_sidecar_says(tmp_path: Path) -> None:
    apk = ServiceApk.load(_write(tmp_path))

    assert apk.path == tmp_path / "spectragent-device.apk"
    assert (apk.info.version_code, apk.info.accessibility_tool, apk.info.protocol) == (
        3,
        True,
        PROTOCOL_VERSION,
    )


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


def test_an_apk_that_is_not_the_build_its_sidecar_describes_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ServiceApkError, match="is not the build its sidecar describes"):
        ServiceApk.load(_write(tmp_path, data=b"PK\x03\x04 another build"))


def test_the_gradle_build_reads_the_protocol_version_from_the_hosts_channel_module() -> None:
    build = (project_dir() / "build.gradle").read_text()
    path = re.search(r'def channelModule = file\("([^"]+)"\)', build)
    pattern = re.search(r"channelModule\.text =~ /(.+)/\n", build)
    assert path is not None and pattern is not None

    assert (project_dir() / path[1]).resolve() == Path(channel.__file__).resolve()
    found = re.search(pattern[1], Path(channel.__file__).read_text())
    assert found is not None and int(found[1]) == PROTOCOL_VERSION
