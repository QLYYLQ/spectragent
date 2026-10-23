"""Provider capabilities NETWORK_CAPTURE / LAUNCH_ARGS and the launch-args seam.

``docs/architecture_design.md`` §4.9.x: the capability decides what it may
hand a provider by reading ``__capabilities__`` only — never ``provider_id``,
never the provider's fields. ``local_chrome`` is the one provider that
launches (and kills) its own browser, so it alone declares both; the six
cloud lite providers declare neither, which is what makes recording and
``replayability: pinned`` refuse them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.archive.fingerprint import flags_digest
from spectragent.core.capabilities.browser.backends import local_chrome
from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderRegistrar
from spectragent.core.capabilities.browser.backends.base import (
    LaunchArgsProvider,
    NetworkCaptureProvider,
    NetworkIsolatingProvider,
)
from spectragent.core.capabilities.browser.backends.capabilities import (
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
    NETWORK_ISOLATION,
)
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    NETWORK_ISOLATION_ARGS,
    NETWORK_ROUTING_SWITCHES,
    contradicting_switch,
    merge_chrome_switches,
    without_routing_switches,
)
from spectragent.core.capabilities.browser.backends.local_chrome import (
    CHROME_BACKGROUND_UNTHROTTLED_ARGS,
    CHROME_DEFAULT_ARGS,
    CHROME_DISABLE_SECURITY_ARGS,
    LocalChromeProvider,
)

#: The six cloud "lite" providers (AGENTS.md §4). None launches a browser the
#: capability owns.
CLOUD_PROVIDERS = ("lexmount", "browser_use", "hyperbrowser", "agentbay", "airtop", "skyvern")

#: A representative ``replayability: pinned`` switch set (its own module pins
#: the exact list; these tests only need owned switches to contradict).
PINNED_LIKE = (
    "--js-flags=--random-seed=42",
    "--lang=en-US",
    "--force-device-scale-factor=1",
    "--disable-back-forward-cache",
    "--disable-features=HttpsUpgrades",
)


def _shipped_providers() -> dict[str, type]:
    import spectragent.core.capabilities.browser.backends.agentbay_lite  # noqa: F401
    import spectragent.core.capabilities.browser.backends.airtop_lite  # noqa: F401
    import spectragent.core.capabilities.browser.backends.browser_use_lite  # noqa: F401
    import spectragent.core.capabilities.browser.backends.hyperbrowser_lite  # noqa: F401
    import spectragent.core.capabilities.browser.backends.lexmount  # noqa: F401
    import spectragent.core.capabilities.browser.backends.skyvern_lite  # noqa: F401

    return {
        key: cls
        for key, cls in BrowserProviderRegistrar.get_all().items()
        if cls.__module__.startswith("spectragent.")
    }


class TestDeclarations:
    def test_only_local_chrome_declares_network_capture_and_launch_args(self) -> None:
        providers = _shipped_providers()
        declaring = sorted(
            key
            for key, cls in providers.items()
            if {NETWORK_CAPTURE, LAUNCH_ARGS} & cls.__capabilities__
        )
        assert declaring == ["local_chrome"]
        assert {NETWORK_CAPTURE, LAUNCH_ARGS} <= LocalChromeProvider.__capabilities__

    @pytest.mark.parametrize("provider_id", CLOUD_PROVIDERS)
    def test_cloud_provider_declares_neither(self, provider_id: str) -> None:
        cls = _shipped_providers()[provider_id]
        assert NETWORK_CAPTURE not in cls.__capabilities__
        assert LAUNCH_ARGS not in cls.__capabilities__

    def test_local_chrome_satisfies_the_launch_args_protocol(self) -> None:
        assert isinstance(LocalChromeProvider(), LaunchArgsProvider)

    def test_local_chrome_reports_its_profile_as_network_capture_promises(self) -> None:
        assert isinstance(LocalChromeProvider(), NetworkCaptureProvider)

    def test_only_local_chrome_declares_network_isolation(self) -> None:
        """The static "supports" declaration of M30's isolation (§4.9.x)."""
        providers = _shipped_providers()
        declaring = sorted(
            key for key, cls in providers.items() if NETWORK_ISOLATION in cls.__capabilities__
        )
        assert declaring == ["local_chrome"]
        assert isinstance(LocalChromeProvider(), NetworkIsolatingProvider)
        for provider_id in CLOUD_PROVIDERS:
            assert NETWORK_ISOLATION not in providers[provider_id].__capabilities__


class TestNetworkIsolation:
    def test_isolation_args_have_no_shell_quotes(self) -> None:
        """Two argv elements exactly as Chrome reads them — never a quoted shell string."""
        assert NETWORK_ISOLATION_ARGS == (
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--no-proxy-server",
        )
        assert not any(quote in arg for arg in NETWORK_ISOLATION_ARGS for quote in "\"'")

    def test_isolation_leaves_no_host_out(self) -> None:
        """Not even ``localhost``: the blackhole has no exception (the user's ruling of 2026-10-13).

        ``test_browser_replay_live.py`` shows a real Chrome launched with these
        switches refusing a replayed page's connections to ``localhost`` while
        the CDP connection itself keeps working.
        """
        (rules,) = [
            arg for arg in NETWORK_ISOLATION_ARGS if arg.startswith("--host-resolver-rules")
        ]
        assert "EXCLUDE" not in rules

    def test_routing_switches_are_dropped_from_a_flag_list(self) -> None:
        flags = [
            "--headless=new",
            "--host-resolver-rules=MAP shop.test 127.0.0.1:9000",
            *NETWORK_ISOLATION_ARGS,
            "--proxy-server=http://p:1",
            "--proxy-pac-url=http://p/x.pac",
            "--proxy-auto-detect",
            "--proxy-bypass-list=*.local",
            "--lang=en-US",
        ]
        assert without_routing_switches(flags) == ["--headless=new", "--lang=en-US"]
        assert {"--host-resolver-rules", "--no-proxy-server", "--proxy-server"} <= (
            NETWORK_ROUTING_SWITCHES
        )

    def test_a_clean_provider_has_no_isolation_conflict(self) -> None:
        assert LocalChromeProvider().isolation_conflicts() == []
        repeat = LocalChromeProvider(extra_args=["--no-proxy-server", "--mute-audio"])
        assert repeat.isolation_conflicts() == [], "an exact repeat contradicts nothing"

    def test_isolation_conflicts_name_each_knob(self) -> None:
        provider = LocalChromeProvider(
            proxy_server="http://127.0.0.1:7897",
            proxy_bypass_list="localhost",
            extra_args=[
                "--host-resolver-rules=MAP shop.test 127.0.0.1:9000",
                "--proxy-pac-url=http://p/x.pac",
                "--lang=fr",
            ],
            startup_url="http://shop.test/",
        )
        problems = provider.isolation_conflicts()
        assert len(problems) == 5
        assert problems[0].startswith("proxy_server='http://127.0.0.1:7897'")
        assert problems[1].startswith("proxy_bypass_list='localhost'")
        assert problems[2].startswith(
            "extra_args entry '--host-resolver-rules=MAP shop.test 127.0.0.1:9000'"
        )
        assert problems[3].startswith("extra_args entry '--proxy-pac-url=http://p/x.pac'")
        assert problems[4].startswith("startup_url='http://shop.test/'")


class TestMergeChromeSwitches:
    def test_js_flags_are_space_joined_into_one_switch(self) -> None:
        merged = merge_chrome_switches(
            ["--a", "--js-flags=--expose-gc", "--b", "--js-flags=--random-seed=7"]
        )
        assert merged == ["--a", "--js-flags=--expose-gc --random-seed=7", "--b"]

    def test_feature_lists_are_comma_joined_and_deduplicated(self) -> None:
        merged = merge_chrome_switches(
            [
                "--disable-features=A,B",
                "--enable-features=X",
                "--disable-features=B,C",
                "--enable-features=Y,X",
            ]
        )
        assert merged == ["--disable-features=A,B,C", "--enable-features=X,Y"]

    def test_other_switches_are_kept_verbatim_and_in_order(self) -> None:
        args = ["--lang=en-US", "--mute-audio", "--lang=fr", "--window-size=1,2"]
        assert merge_chrome_switches(args) == args

    def test_disable_security_no_longer_erases_the_default_feature_list(self) -> None:
        """Before the merge, Chrome kept only the second ``--disable-features``."""
        merged = merge_chrome_switches([*CHROME_DEFAULT_ARGS, *CHROME_DISABLE_SECURITY_ARGS])
        disabled = [a for a in merged if a.startswith("--disable-features=")]
        assert len(disabled) == 1
        features = disabled[0].split("=", 1)[1].split(",")
        assert "Translate" in features  # from the defaults
        assert "IsolateOrigins" in features  # from disable_security

    def test_an_empty_list_switch_is_dropped(self) -> None:
        assert merge_chrome_switches(["--js-flags=", "--x"]) == ["--x"]


class TestContradictingSwitch:
    def test_same_value_is_not_a_contradiction(self) -> None:
        assert contradicting_switch("--force-device-scale-factor=1.0", PINNED_LIKE) is None
        assert contradicting_switch("--lang=en-US", PINNED_LIKE) is None

    def test_different_value_names_the_owned_switch(self) -> None:
        assert contradicting_switch("--lang=fr", PINNED_LIKE) == "--lang=en-US"

    def test_v8_flag_and_feature_clashes(self) -> None:
        assert contradicting_switch("--js-flags=--random-seed=1", PINNED_LIKE) == (
            "--js-flags=--random-seed=42"
        )
        assert contradicting_switch("--js-flags=--expose-gc", PINNED_LIKE) is None
        assert contradicting_switch("--enable-features=HttpsUpgrades,Foo", PINNED_LIKE) == (
            "--disable-features=HttpsUpgrades"
        )
        assert contradicting_switch("--disable-features=Foo", PINNED_LIKE) is None


class TestBackgroundThrottling:
    """``background_throttling``: the three switches that keep left tabs at full speed (§4.9.x)."""

    def test_the_switches_are_among_the_defaults(self) -> None:
        assert set(CHROME_BACKGROUND_UNTHROTTLED_ARGS) <= set(CHROME_DEFAULT_ARGS)

    def test_off_by_default_the_switches_stay_where_they_were(self) -> None:
        provider = LocalChromeProvider()
        assert provider.background_throttling is False
        args = provider._build_args(9222, "/tmp/p")
        assert args[: len(CHROME_DEFAULT_ARGS)] == list(CHROME_DEFAULT_ARGS)

    def test_on_leaves_exactly_the_three_out(self) -> None:
        default = LocalChromeProvider()._build_args(9222, "/tmp/p")
        throttled = LocalChromeProvider(background_throttling=True)._build_args(9222, "/tmp/p")
        assert not set(CHROME_BACKGROUND_UNTHROTTLED_ARGS) & set(throttled)
        assert throttled == [a for a in default if a not in CHROME_BACKGROUND_UNTHROTTLED_ARGS]

    def test_the_knob_decides_whatever_ignore_default_args_says(self) -> None:
        bare = LocalChromeProvider(ignore_default_args=True)._build_args(9222, "/tmp/p")
        assert set(CHROME_BACKGROUND_UNTHROTTLED_ARGS) <= set(bare)
        assert not set(CHROME_DEFAULT_ARGS) - set(CHROME_BACKGROUND_UNTHROTTLED_ARGS) & set(bare)
        throttled = LocalChromeProvider(
            ignore_default_args=True, background_throttling=True
        )._build_args(9222, "/tmp/p")
        assert not set(CHROME_BACKGROUND_UNTHROTTLED_ARGS) & set(throttled)

    def test_it_is_part_of_the_launch_flags_a_replay_compares(self) -> None:
        """A recording and a replay that differ in it are refused by the launch-flag digest."""
        default = LocalChromeProvider().planned_launch_flags(PINNED_LIKE)
        throttled = LocalChromeProvider(background_throttling=True).planned_launch_flags(
            PINNED_LIKE
        )
        assert flags_digest(default) != flags_digest(throttled)


#: Defaults with no knob of their own, each with the knob it once had. Nothing
#: read those knobs: ``false`` in YAML left the switch on.
FIXED_DEFAULT_SWITCHES = {
    "disable_background_networking": "--disable-background-networking",
    "disable_popup_blocking": "--disable-popup-blocking",
    "disable_translate": "--disable-translate",
    "disable_sync": "--disable-sync",
    "no_first_run": "--no-first-run",
    "disable_default_apps": "--disable-default-apps",
}


class TestFixedDefaultSwitches:
    def test_a_default_launch_passes_every_one(self) -> None:
        args = LocalChromeProvider()._build_args(9222, "/tmp/p")
        assert set(FIXED_DEFAULT_SWITCHES.values()) <= set(args)

    @pytest.mark.parametrize("knob", sorted(FIXED_DEFAULT_SWITCHES))
    def test_a_config_naming_the_old_knob_is_refused(self, knob: str) -> None:
        from spectragent.generated._composed import LocalChromeBrowser_ProviderConfig

        with pytest.raises(ValidationError, match=knob):
            LocalChromeBrowser_ProviderConfig.model_validate({knob: False})


class _FakeProcess:
    pid = 4321
    returncode = 0  # already exited: close() has nothing to kill


class TestLaunchArgsReachChrome:
    #: The environment the last ``_open`` handed the subprocess (``None``: inherited as it is).
    env: dict[str, str] | None = None

    async def _open(
        self, monkeypatch: pytest.MonkeyPatch, provider: LocalChromeProvider, **kwargs: Any
    ) -> tuple[list[str], dict[str, Any]]:
        record: dict[str, Any] = {}

        async def _fake_exec(*cmd: str, **kw: Any) -> _FakeProcess:
            record["cmd"] = list(cmd)
            record["env"] = kw.get("env")
            return _FakeProcess()

        async def _fake_wait(_self: Any, _port: int, _process: Any, _profile: str) -> str:
            return "ws://127.0.0.1:9222/devtools/browser/x"

        monkeypatch.setattr(local_chrome.asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(LocalChromeProvider, "_wait_for_cdp", _fake_wait)
        provider.executable_path = "/fake/chrome"
        session = await provider.open(**kwargs)
        await provider.close(session)
        self.env = record["env"]
        return record["cmd"], session.metadata

    async def test_launch_args_follow_extra_args_and_are_merged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        provider = LocalChromeProvider(
            user_data_dir=str(tmp_path),
            extra_args=["--js-flags=--expose-gc", "--disable-features=Foo"],
        )
        cmd, _meta = await self._open(monkeypatch, provider, launch_args=PINNED_LIKE)
        argv = cmd[1:-1]
        assert argv.count("--js-flags=--expose-gc --random-seed=42") == 1
        assert not [a for a in argv if a.startswith("--js-flags=") and "expose" not in a]
        disabled = [a for a in argv if a.startswith("--disable-features=")]
        assert len(disabled) == 1 and disabled[0].endswith(",Foo,HttpsUpgrades")
        # The owned switches that are not list switches come last, in order:
        # after every provider switch, ``extra_args`` included.
        assert argv[-3:] == [
            "--lang=en-US",
            "--force-device-scale-factor=1",
            "--disable-back-forward-cache",
        ]

    async def test_metadata_records_the_command_line_and_placeholdered_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = LocalChromeProvider(remote_debugging_port=9555)
        cmd, meta = await self._open(monkeypatch, provider, launch_args=PINNED_LIKE)
        assert meta["command_line"] == cmd
        flags = meta["launch_flags"]
        assert flags == [
            "--user-data-dir=<user-data-dir>"
            if a.startswith("--user-data-dir=")
            else "--remote-debugging-port=<port>"
            if a.startswith("--remote-debugging-port=")
            else a
            for a in cmd[1:-1]
        ]
        assert "/fake/chrome" not in flags and provider.startup_url not in flags
        assert "--remote-debugging-port=<port>" in flags
        assert "--user-data-dir=<user-data-dir>" in flags

    async def test_flags_are_stable_across_launches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = await self._open(monkeypatch, LocalChromeProvider(), launch_args=PINNED_LIKE)
        second = await self._open(monkeypatch, LocalChromeProvider(), launch_args=PINNED_LIKE)
        assert first[0] != second[0]  # fresh profile dir and port each launch
        assert first[1]["launch_flags"] == second[1]["launch_flags"]

    async def test_no_launch_args_is_the_old_command_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        provider = LocalChromeProvider(user_data_dir=str(tmp_path), remote_debugging_port=9666)
        cmd, _ = await self._open(monkeypatch, provider)
        assert cmd[1:-1] == merge_chrome_switches(provider._build_args(9666, str(tmp_path)))

    @pytest.mark.parametrize(
        "launch_args", [(), PINNED_LIKE, (*PINNED_LIKE, *NETWORK_ISOLATION_ARGS)]
    )
    @pytest.mark.parametrize("launch_env", [None, {"TZ": "Asia/Tokyo"}])
    async def test_planned_flags_are_what_open_records(
        self,
        monkeypatch: pytest.MonkeyPatch,
        launch_args: tuple[str, ...],
        launch_env: dict[str, str] | None,
    ) -> None:
        """Known before launch (a replay's consistency check) and equal to the launch's own."""
        provider = LocalChromeProvider(extra_args=["--mute-audio"])
        planned = provider.planned_launch_flags(launch_args, launch_env)
        _cmd, meta = await self._open(
            monkeypatch, provider, launch_args=launch_args, launch_env=launch_env
        )
        assert planned == meta["launch_flags"]

    async def test_the_launch_environment_reaches_chrome_and_heads_the_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TZ`` (pinned's default time zone) is added to what Chrome inherits, and digested."""
        monkeypatch.setenv("AF_INHERITED_PROBE", "kept")
        monkeypatch.setenv("TZ", "Europe/Paris")
        cmd, meta = await self._open(
            monkeypatch, LocalChromeProvider(), launch_args=PINNED_LIKE, launch_env={"TZ": "UTC"}
        )
        assert self.env is not None
        assert self.env["TZ"] == "UTC" and self.env["AF_INHERITED_PROBE"] == "kept"
        assert meta["launch_env"] == {"TZ": "UTC"}
        assert meta["launch_flags"][0] == "TZ=UTC"
        assert not any(arg.startswith("TZ=") for arg in cmd), "never a switch on the command line"

    async def test_no_launch_environment_inherits_this_process_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cmd, meta = await self._open(monkeypatch, LocalChromeProvider(), launch_args=PINNED_LIKE)
        assert self.env is None  # create_subprocess_exec inherits os.environ as it is
        assert meta["launch_env"] == {}
        assert not any(flag.startswith("TZ=") for flag in meta["launch_flags"])

    def test_planned_browser_defaults_follow_the_launch_environment(self) -> None:
        provider = LocalChromeProvider(executable_path="/fake/chrome")
        assert provider.planned_browser_defaults({"TZ": "Asia/Tokyo"}).timezone == "Asia/Tokyo"
        assert provider.planned_browser_defaults({"TZ": "JST-9"}).timezone is None

    async def test_the_isolation_switches_reach_chrome_as_two_elements(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd, _ = await self._open(
            monkeypatch, LocalChromeProvider(), launch_args=NETWORK_ISOLATION_ARGS
        )
        assert cmd[-3:-1] == list(NETWORK_ISOLATION_ARGS)


class TestLaunchArgConflicts:
    def test_clean_provider_reports_nothing(self) -> None:
        assert LocalChromeProvider().launch_arg_conflicts(PINNED_LIKE) == []
        assert LocalChromeProvider(deterministic_rendering=True).launch_arg_conflicts(()) == []

    def test_deterministic_rendering_names_its_knob(self) -> None:
        problems = LocalChromeProvider(deterministic_rendering=True).launch_arg_conflicts(
            PINNED_LIKE
        )
        assert len(problems) == 1
        assert problems[0].startswith("deterministic_rendering=true")
        assert "--random-seed=42" in problems[0]

    def test_deterministic_rendering_passes_with_an_equal_seed(self) -> None:
        """Like every other row: equal values agree (the user's ruling of 2026-10-13)."""
        same_seed = ("--js-flags=--random-seed=1157259159",)
        assert (
            LocalChromeProvider(deterministic_rendering=True).launch_arg_conflicts(same_seed) == []
        )

    def test_force_device_scale_factor_only_when_it_differs(self) -> None:
        same = LocalChromeProvider(force_device_scale_factor=1.0)
        assert same.launch_arg_conflicts(PINNED_LIKE) == []
        other = LocalChromeProvider(force_device_scale_factor=2.0)
        problems = other.launch_arg_conflicts(PINNED_LIKE)
        assert problems == ["force_device_scale_factor=2 contradicts --force-device-scale-factor=1"]

    def test_user_agent_contradicts_a_pinned_user_agent(self) -> None:
        provider = LocalChromeProvider(user_agent="Agent/1")
        assert provider.launch_arg_conflicts(PINNED_LIKE) == []
        problems = provider.launch_arg_conflicts([*PINNED_LIKE, "--user-agent=Pinned/2"])
        assert problems == ["user_agent contradicts --user-agent=Pinned/2"]

    def test_extra_args_entries_are_named_one_by_one(self) -> None:
        provider = LocalChromeProvider(
            extra_args=["--lang=fr", "--mute-audio", "--enable-features=HttpsUpgrades"]
        )
        assert provider.launch_arg_conflicts(PINNED_LIKE) == [
            "extra_args entry '--lang=fr' contradicts --lang=en-US",
            "extra_args entry '--enable-features=HttpsUpgrades' contradicts "
            "--disable-features=HttpsUpgrades",
        ]


class TestPersistentProfile:
    def test_fresh_by_default(self) -> None:
        assert LocalChromeProvider().persistent_profile() is None

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"user_data_dir": "/p"}, "user_data_dir='/p'"),
            ({"profile_directory": "Profile 2"}, "profile_directory='Profile 2'"),
            ({"extra_args": ["--user-data-dir=/x"]}, "extra_args entry '--user-data-dir=/x'"),
        ],
    )
    def test_each_source_is_named(self, kwargs: dict[str, Any], expected: str) -> None:
        assert LocalChromeProvider(**kwargs).persistent_profile() == expected

    async def test_the_old_environment_variable_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The profile directory is YAML only: config.json must show it (ruling of 2026-10-13)."""
        monkeypatch.setenv("CHROME_USER_DATA_DIR", "/home/me/chrome")
        provider = LocalChromeProvider()
        assert provider.persistent_profile() is None
        cmd, meta = await TestLaunchArgsReachChrome()._open(monkeypatch, provider)
        assert meta["user_data_dir"] != "/home/me/chrome"
        assert "--user-data-dir=/home/me/chrome" not in cmd

    def test_several_sources_are_all_named(self) -> None:
        found = LocalChromeProvider(
            user_data_dir="/p", profile_directory="Work"
        ).persistent_profile()
        assert found == "user_data_dir='/p'; profile_directory='Work'"


def test_open_signature_accepts_launch_args_keyword_only() -> None:
    import inspect

    params = inspect.signature(LocalChromeProvider.open).parameters
    assert params["launch_args"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["launch_args"].default == ()
    assert asyncio.iscoroutinefunction(LocalChromeProvider.open)


@pytest.mark.parametrize("provider_id", CLOUD_PROVIDERS)
def test_cloud_providers_refuse(provider_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``replayability: pinned`` on a cloud lite provider fails at setup, naming both sides.

    The provider does not launch a browser the capability owns
    (``NETWORK_CAPTURE``) and cannot take launch switches (``LAUNCH_ARGS``);
    both are reported at once, and nothing is opened — the refusal comes
    before ``backend.open()``, so no cloud session is ever allocated.
    """
    from spectragent.core.capabilities.browser import BrowserCapability
    from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
    from spectragent.core.capabilities.browser.replay import PinnedReplayability

    cls = _shipped_providers()[provider_id]
    opened: list[dict[str, Any]] = []

    async def _never_open(_self: Any, **kwargs: Any) -> Any:
        opened.append(kwargs)
        raise AssertionError("a refused configuration must not open a session")

    monkeypatch.setattr(cls, "open", _never_open)
    cap = BrowserCapability()
    cap.replayability = PinnedReplayability()
    cap.backend = cls()  # type: ignore[assignment]

    with pytest.raises(BrowserCooperationError) as info:
        asyncio.run(cap.setup())
    assert opened == []
    problems = info.value.problems
    assert len(problems) == 2
    for problem in problems:
        assert problem.startswith(f"replayability: pinned ↔ backend provider_id={provider_id!r}")
    assert "NETWORK_CAPTURE" in problems[0] and "LAUNCH_ARGS" in problems[1]
