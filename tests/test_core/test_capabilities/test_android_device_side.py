"""The device side for one run: install, the helper, enable, confirm by ping, stop, and §11.2's constraints.

``spectragent/core/capabilities/android/device/lifecycle.py`` drives a
:class:`~tests._helpers.fake_device_side.FakeDeviceSide` behind the fake ADB
server, which answers as the APK and Android do. The four hard constraints of
``docs/architecture_design.md`` §11.2 ("设备端") are tested twice: as what the
lifecycle sends, and in the sources of the APK and the Android package.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import spectragent
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.device import lifecycle
from spectragent.core.capabilities.android.device.apk import (
    TEST_KEY_CERTIFICATE_SHA256,
    ServiceApk,
    build_directory,
)
from spectragent.core.capabilities.android.device.channel import (
    PROTOCOL_VERSION,
    JsonObject,
    RemoteError,
)
from spectragent.core.capabilities.android.device.lifecycle import DeviceSide, DeviceSideError
from tests._helpers.fake_adb import FakeAdbServer, ShellIO, StreamingCommand
from tests._helpers.fake_device_side import (
    PROVIDER_ERROR,
    SERVICE_COMPONENT,
    Answer,
    Failure,
    FakeDeviceSide,
)

SERIAL = "emulator-5554"
TALKBACK = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
APK_BYTES = b"PK\x03\x04 the device-side APK " + bytes(range(256)) * 300
#: Long enough for a loaded machine; a protocol deadlock fails instead of hanging.
_WAIT_S = 10
REPOSITORY = Path(spectragent.__file__).resolve().parent.parent


@pytest.fixture
def apk(tmp_path: Path) -> ServiceApk:
    build = build_directory(hashlib.sha256(APK_BYTES).hexdigest())
    path = tmp_path / build / "spectragent-device.apk"
    path.parent.mkdir()
    path.write_bytes(APK_BYTES)
    sidecar = {
        "format": "spectragent-device-apk-2",
        "package": "org.spectragent.device",
        "version_code": 1,
        "version_name": "1",
        "protocol": PROTOCOL_VERSION,
        "accessibility_tool": False,
        "sha256": hashlib.sha256(APK_BYTES).hexdigest(),
        "signing_certificate_sha256": TEST_KEY_CERTIFICATE_SHA256,
    }
    path.with_suffix(".json").write_text(json.dumps(sidecar))
    return ServiceApk.load(path)


@asynccontextmanager
async def _phone(side: FakeDeviceSide) -> AsyncIterator[tuple[FakeAdbServer, AdbClient]]:
    """The phone behind a fake ADB server; the test's body may take at most :data:`_WAIT_S`."""
    async with (
        FakeAdbServer(
            {SERIAL: side.device}, streams={SERIAL: side.streams}, sockets={SERIAL: side.sockets}
        ) as adb,
        asyncio.timeout(_WAIT_S),
    ):
        yield adb, AdbClient(adb.host, adb.port)


def _services(adb: FakeAdbServer) -> list[str]:
    return [request.service for request in adb.requests]


async def _helper_exited(side: FakeDeviceSide) -> None:
    """Until the fake helper has seen its stdin end and exited.

    ``stop()`` closes the helper's shell session and waits for its own reader,
    never for the program on the device, which exits on its own schedule. Called
    inside :func:`_phone`, whose time limit bounds the wait: the fake ADB server
    cancels a helper that is still running when it shuts down.
    """
    while side.helper_exits == 0:
        await asyncio.sleep(0.01)


async def test_start_installs_starts_the_helper_and_confirms_the_service_with_a_ping(
    apk: ServiceApk,
) -> None:
    side = FakeDeviceSide(enabled=TALKBACK)
    async with _phone(side) as (adb, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        try:
            assert (await device.service.ping()).uptime_ms == 1000
            enabled_while_up = side.enabled
        finally:
            await device.stop()
        await _helper_exited(side)

    assert side.installed == APK_BYTES and device.installed_now
    assert enabled_while_up == f"{TALKBACK}:{SERVICE_COMPONENT}", "appended, TalkBack kept"
    assert side.enabled == TALKBACK, "stop() takes only the service out"
    assert (device.faketouch, device.gestures) == (True, True)
    assert side.helper_exits == 1, "the helper's stdin ended with the session, and it exited"


async def test_each_program_has_its_own_token_and_neither_is_ever_on_a_command_line(
    apk: ServiceApk,
) -> None:
    """``ps`` shows a command line to the shell and root users; the tokens go on stdin."""
    side = FakeDeviceSide()
    async with _phone(side) as (adb, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        service_token = side.service.token
        await device.stop()

    (line,) = side.helper_stdin
    socket, helper_token = line.decode().split()
    assert socket.startswith("spectragent_helper_")
    assert len(helper_token) >= 16 and len(service_token) >= 16
    assert helper_token != service_token
    for service in _services(adb):
        assert helper_token not in service and service_token not in service, service


async def test_a_device_that_has_this_build_is_not_installed_again(apk: ServiceApk) -> None:
    side = FakeDeviceSide(installed=APK_BYTES)
    async with _phone(side) as (adb, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()

    assert side.installs == 0 and not device.installed_now
    assert not any(service.startswith("shell,v2:cmd package install") for service in _services(adb))


async def test_another_build_on_the_device_is_replaced(apk: ServiceApk) -> None:
    side = FakeDeviceSide(installed=b"an older build")
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()

    assert side.installed == APK_BYTES and side.installs == 1


async def test_binding_is_confirmed_by_a_ping_and_never_by_reading_the_setting(
    apk: ServiceApk,
) -> None:
    """Constraint 4: the setting is written, the system refuses the service and drops it from the
    setting without a word (Android 14 and later); only a ping can tell."""
    side = FakeDeviceSide(binds=False)
    async with _phone(side) as (adb, client):
        with pytest.raises(TimeoutError, match="waiting for the service to answer a ping"):
            await DeviceSide.start(client, SERIAL, apk, timeout_s=1.0)
        await _helper_exited(side)

    assert not any("settings" in service for service in _services(adb))
    assert side.helper_exits == 1, "the failed start stopped the helper"


async def test_a_service_process_that_lost_the_token_is_handed_it_again(apk: ServiceApk) -> None:
    side = FakeDeviceSide(lose_token_once=True)
    async with _phone(side) as (adb, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()

    assert side.tokens_written == 2


async def test_a_provider_that_throws_on_the_token_fails_the_start_with_its_error(
    apk: ServiceApk,
) -> None:
    """``content`` exits 0 when the provider throws; its error on stderr is what tells. Taken for
    a success, the start would wait for a ping the service refuses, until its time ran out."""
    side = FakeDeviceSide(token_write_throws=True)
    async with _phone(side) as (_, client):
        with pytest.raises(DeviceSideError, match=f"did not take the token: {PROVIDER_ERROR}"):
            await DeviceSide.start(client, SERIAL, apk, timeout_s=2.0)
        await _helper_exited(side)

    assert side.helper_exits == 1, "the failed start stopped the helper"


async def test_stop_never_force_stops_our_package(apk: ServiceApk) -> None:
    """Constraint 3: a force-stop takes the service out of the setting for good; stop() unbinds it
    through the setting and leaves the package alone."""
    side = FakeDeviceSide()
    async with _phone(side) as (adb, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()
        await device.stop()  # safe to repeat

    assert side.enabled is None and not side.bound
    for service in _services(adb):
        assert not re.search(r"force-stop|pm clear|uninstall|am kill", service), service


async def test_stop_returns_once_the_service_is_unbound_so_a_next_start_reaches_a_new_one(
    apk: ServiceApk,
) -> None:
    """The system unbinds a moment after the setting changed. Had stop returned before, a device
    side started next on the same device would ping the instance going away, whose socket then
    closes under it (a device kept for a sweep's next task, 2026-11-04)."""
    side = FakeDeviceSide(unbinds_after_reads=3)
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()
        assert not side.bound, "stop waited for the unbind"

        nxt = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        try:
            assert (await nxt.service.ping()).uptime_ms == 1000
        finally:
            await nxt.stop()

    assert side.enabled is None and not side.bound


async def test_a_service_enabled_before_the_run_stays_enabled(apk: ServiceApk) -> None:
    side = FakeDeviceSide(enabled=SERVICE_COMPONENT)
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()

    assert side.enabled == SERVICE_COMPONENT


async def test_ensure_bound_enables_the_service_again_after_a_force_stop(apk: ServiceApk) -> None:
    side = FakeDeviceSide()
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        try:
            assert await device.ensure_bound(timeout_s=_WAIT_S) is False
            side.force_stop()  # what a task may do to every app
            assert await device.ensure_bound(timeout_s=_WAIT_S) is True
            assert side.bound and side.enabled == SERVICE_COMPONENT
            assert (await device.service.ping()).uptime_ms == 1000
        finally:
            await device.stop()

    assert side.enabled is None


async def test_a_service_held_as_crashed_is_taken_out_of_the_setting_and_put_back(
    apk: ServiceApk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a force-stop on API 30 the system holds the service as crashed: enabling it again
    binds nothing until the setting has been seen without it (measured)."""
    monkeypatch.setattr(lifecycle, "_REBIND_S", 0.2)
    side = FakeDeviceSide(enabled=TALKBACK, crashes_on_force_stop=True)
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        try:
            side.force_stop()
            assert await device.ensure_bound(timeout_s=_WAIT_S) is True
            assert (await device.service.ping()).uptime_ms == 1000
            assert side.enabled == f"{TALKBACK}:{SERVICE_COMPONENT}"
        finally:
            await device.stop()

    assert side.enabled == TALKBACK


async def test_a_second_recovery_works_after_one_that_ran_out_of_time(apk: ServiceApk) -> None:
    side = FakeDeviceSide()
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        try:
            side.force_stop()
            side.binds = False  # the system does not bind it this time
            with pytest.raises(TimeoutError, match="did not answer within 0.5 s"):
                await device.ensure_bound(timeout_s=0.5)
            side.binds = True
            assert await device.ensure_bound(timeout_s=_WAIT_S) is True
            assert (await device.service.ping()).uptime_ms == 1000
        finally:
            await device.stop()

    assert side.enabled is None


class _SlowToAnswerEnable(FakeDeviceSide):
    """The helper writes the setting, and its answer comes back late (a loaded machine)."""

    async def _enable_method(self, params: JsonObject) -> Answer:
        answer = await super()._enable_method(params)
        await asyncio.sleep(2)
        return answer


class _EnableFailsAfterItsWrite(FakeDeviceSide):
    """The helper writes the setting, and then the enable fails."""

    async def _enable_method(self, params: JsonObject) -> Answer:
        await super()._enable_method(params)
        raise Failure("settings_failed", "settings put secure accessibility_enabled exited 1")


@pytest.mark.parametrize("side_class", [_SlowToAnswerEnable, _EnableFailsAfterItsWrite])
async def test_a_start_that_fails_while_enabling_leaves_the_setting_as_it_was(
    apk: ServiceApk, side_class: type[FakeDeviceSide]
) -> None:
    side = side_class(enabled=TALKBACK)
    async with _phone(side) as (_, client):
        with pytest.raises((TimeoutError, RemoteError)):
            await DeviceSide.start(client, SERIAL, apk, timeout_s=1.0)
        await _helper_exited(side)

    assert side.enabled == TALKBACK, "our entry was taken out again, TalkBack's kept"
    assert side.helper_exits == 1


async def test_a_device_without_faketouch_reports_that_it_cancels_gestures(
    apk: ServiceApk,
) -> None:
    side = FakeDeviceSide(faketouch=False)
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        await device.stop()

    assert (device.faketouch, device.gestures) == (False, False)
    assert device.gesture_detail is not None and "gesture_cancelled" in device.gesture_detail


async def test_a_helper_that_exits_before_it_is_ready_fails_the_start_with_its_words(
    apk: ServiceApk,
) -> None:
    side = FakeDeviceSide()

    async def crashes(shell: ShellIO) -> int:
        await shell.write(
            b"java.lang.NoSuchMethodException: getInternalDisplayToken\n", stderr=True
        )
        return 1

    def streams(command: str) -> StreamingCommand | None:
        return crashes if "app_process" in command else side.streams(command)

    async with FakeAdbServer({SERIAL: side.device}, streams={SERIAL: streams}) as adb:
        with pytest.raises(
            DeviceSideError, match="exited with code 1 before it was ready"
        ) as failed:
            await DeviceSide.start(AdbClient(adb.host, adb.port), SERIAL, apk, timeout_s=_WAIT_S)

    assert "NoSuchMethodException" in str(failed.value)


async def test_health_answers_from_the_provider_while_the_socket_is_down(apk: ServiceApk) -> None:
    side = FakeDeviceSide()
    async with _phone(side) as (_, client):
        device = await DeviceSide.start(client, SERIAL, apk, timeout_s=_WAIT_S)
        side.force_stop()
        health = await device.health()
        await device.stop()

    assert (health.socket, health.service_connected, health.protocol) == (
        "spectragent_device",
        False,
        PROTOCOL_VERSION,
    )


async def test_a_start_that_runs_out_of_time_names_its_step(apk: ServiceApk) -> None:
    side = FakeDeviceSide()

    async def hangs(shell: ShellIO) -> int:
        await asyncio.Event().wait()
        return 0

    def streams(command: str) -> StreamingCommand | None:
        return hangs if "app_process" in command else side.streams(command)

    async with FakeAdbServer({SERIAL: side.device}, streams={SERIAL: streams}) as adb:
        with pytest.raises(TimeoutError, match="starting the shell helper"):
            await DeviceSide.start(AdbClient(adb.host, adb.port), SERIAL, apk, timeout_s=0.5)


# ── the constraints, in the sources ───────────────────────────────────


def _java_without_comments(path: Path) -> str:
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _code_strings(path: Path) -> list[str]:
    """Every string literal of a Python module that is not a docstring."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_device_side_code_opens_a_ui_automation_or_runs_uiautomator() -> None:
    """Constraints 1 and 2: a UiAutomation in its default mode unbinds every accessibility
    service while it is connected, and ``uiautomator dump`` uses one."""
    sources = sorted((REPOSITORY / "android" / "service" / "src").rglob("*.java"))
    assert sources, "the APK's sources are where the test looks"
    for source in sources:
        code = _java_without_comments(source)
        assert "UiAutomation" not in code and "uiautomator" not in code, source


def test_no_host_code_runs_uiautomator_or_force_stops_a_package() -> None:
    """Constraints 2 and 3, in the Android package: no command it builds runs uiautomator, and
    none force-stops (a reset stops only the app under test, and that is not this package's)."""
    package = REPOSITORY / "spectragent" / "core" / "capabilities" / "android"
    for module in sorted(package.rglob("*.py")):
        for text in _code_strings(module):
            assert "uiautomator" not in text and "force-stop" not in text, (module, text)
