"""The browser's defaults: what a frame no override reaches falls back to (§4.14).

``backends/browser_defaults.py`` reads them two ways: from the OS before
launch (the time zone by ICU's rule — ``TZ`` the launch sets, else the one
inherited, else the zone ``/etc/localtime`` links to — and, on macOS, the
locale Cocoa picks for Chrome's bundle), and from Chrome right after launch,
in a frame no override reached. The live check that the two agree is
``test_browser_new_tab_live.py::test_the_os_facts_are_what_chrome_falls_back_to``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.backends.browser_defaults import (
    DEFAULTS_WORLD,
    BrowserDefaults,
    expected_browser_defaults,
    measure_browser_defaults,
)
from spectragent.core.capabilities.browser.backends.local_chrome import find_chrome
from tests._helpers.fake_cdp import FakeBrowser


def _localtime(tmp_path: Path, target: str) -> str:
    link = tmp_path / "localtime"
    os.symlink(target, link)
    return str(link)


class TestTimeZoneBeforeLaunch:
    def test_the_launch_environment_wins(self, tmp_path: Path) -> None:
        localtime = _localtime(tmp_path, "/var/db/timezone/zoneinfo/Asia/Shanghai")
        defaults = expected_browser_defaults(
            executable=None,
            launch_env={"TZ": "UTC"},
            environ={"TZ": "Europe/Paris"},
            localtime=localtime,
        )
        assert defaults.timezone == "UTC"

    def test_an_inherited_tz_comes_next(self, tmp_path: Path) -> None:
        localtime = _localtime(tmp_path, "/usr/share/zoneinfo/Asia/Shanghai")
        defaults = expected_browser_defaults(
            executable=None, launch_env={}, environ={"TZ": ":Europe/Paris"}, localtime=localtime
        )
        assert defaults.timezone == "Europe/Paris"

    def test_without_tz_the_zone_localtime_links_to(self, tmp_path: Path) -> None:
        mac = _localtime(tmp_path, "/var/db/timezone/zoneinfo/Asia/Shanghai")
        assert (
            expected_browser_defaults(executable=None, launch_env={}, environ={}, localtime=mac)
        ).timezone == "Asia/Shanghai"

    @pytest.mark.parametrize(
        ("launch_env", "link"),
        [
            ({"TZ": "JST-9"}, "/usr/share/zoneinfo/UTC"),  # a POSIX rule names no zone
            ({}, "/etc/somewhere/else"),  # a link outside any zoneinfo directory
            ({}, None),  # no /etc/localtime at all
        ],
    )
    def test_what_names_no_zone_is_unknown(
        self, tmp_path: Path, launch_env: dict[str, str], link: str | None
    ) -> None:
        localtime = _localtime(tmp_path, link) if link else str(tmp_path / "missing")
        defaults = expected_browser_defaults(
            executable=None, launch_env=launch_env, environ={}, localtime=localtime
        )
        assert defaults.timezone is None


class TestLocaleBeforeLaunch:
    def test_off_macos_the_locale_is_not_read_before_launch(self) -> None:
        defaults = expected_browser_defaults(
            executable="/usr/bin/chromium", launch_env={}, platform="linux"
        )
        assert defaults.locale is None

    def test_an_executable_outside_an_app_bundle_has_no_cocoa_locale(self) -> None:
        defaults = expected_browser_defaults(
            executable="/opt/chromium/chrome", launch_env={}, platform="darwin"
        )
        assert defaults.locale is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="Cocoa's choice exists on macOS only")
    def test_on_macos_cocoa_names_a_chrome_locale(self) -> None:
        try:
            executable = find_chrome()
        except FileNotFoundError:
            pytest.skip("no Chrome installed")
        locale = expected_browser_defaults(executable=executable, launch_env={}).locale
        assert locale is not None and "_" not in locale and locale != "en"


class TestRecord:
    def test_as_and_from_record(self) -> None:
        defaults = BrowserDefaults(timezone="UTC", locale="zh-CN")
        assert defaults.as_record() == {"timezone": "UTC", "locale": "zh-CN"}
        assert BrowserDefaults.from_record(defaults.as_record()) == defaults

    @pytest.mark.parametrize("record", [None, "UTC", ["UTC"]])
    def test_no_record_is_none(self, record: object) -> None:
        assert BrowserDefaults.from_record(record) is None

    def test_empty_or_odd_values_are_unknown(self) -> None:
        assert BrowserDefaults.from_record({"timezone": "", "locale": 3}) == BrowserDefaults()


class TestMeasurement:
    async def test_measured_in_an_isolated_world_of_the_first_tab_then_detached(self) -> None:
        browser = FakeBrowser()
        browser.default_timezone, browser.default_locale = "Asia/Tokyo", "zh-CN"
        client = browser.client("ws://fake/devtools/browser")
        await client.start()
        measured = await measure_browser_defaults(client, timeout=1.0)
        assert measured == BrowserDefaults(timezone="Asia/Tokyo", locale="zh-CN")
        assert client.methods() == [
            "Target.getTargets",
            "Target.attachToTarget",
            "Page.getFrameTree",
            "Page.createIsolatedWorld",
            "Runtime.evaluate",
            "Target.detachFromTarget",
        ]
        world = next(s for s in client.sent if s.method == "Page.createIsolatedWorld")
        assert world.params["worldName"] == DEFAULTS_WORLD
        assert browser.targets["page-1"].sessions == {}, "the measuring session is gone"
        await client.stop()

    async def test_no_page_to_measure_in_raises(self) -> None:
        browser = FakeBrowser()
        browser.targets.clear()
        client = browser.client("ws://fake/devtools/browser")
        await client.start()
        with pytest.raises(RuntimeError, match="no page target"):
            await measure_browser_defaults(client, timeout=1.0)
        await client.stop()
