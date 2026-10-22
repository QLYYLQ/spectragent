"""The ``replayability`` receptor: its registry, both tiers, the shim, cooperation.

``docs/architecture_design.md`` §4.14. What a pinned shim *does* inside a
page and a worker is checked against real Chrome in
``test_browser_recorder_live.py::TestPinnedShims``; this module pins the
configuration surface, the exact switches and commands each tier hands out,
and the setup-time refusals.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, ClassVar

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.backends.capabilities import (
    CDP,
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
)
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cooperation import (
    BrowserCooperationError,
    cooperation_problems,
)
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import (
    SHIM_SHA256,
    NoneReplayability,
    PinnedReplayability,
    ReplayabilityProtocol,
    ReplayabilityRegistrar,
    default_replayability,
    render_shim,
)
from spectragent.core.capabilities.browser.replay.shims import SHIM_TEMPLATE
from spectragent.schemas.provider_session import ProviderSession

PINNED_DEFAULT_ARGS = (
    "--js-flags=--random-seed=1",
    "--lang=en-US",
    "--force-device-scale-factor=1",
    "--disable-back-forward-cache",
    "--disable-features=HttpsUpgrades",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-field-trial-config",
)


def _target(kind: str) -> RecorderTarget:
    return RecorderTarget(session_id=f"s-{kind}", target_id=f"t-{kind}", type=kind)


def _capability_config(replayability: Any) -> Any:
    from spectragent.generated._composed import BrowserCapabilityConfig

    return BrowserCapabilityConfig.model_validate(
        {
            "mouse_trajectory": {"trajectory_type": "instant"},
            "typing_rhythm": {"rhythm_type": "instant"},
            "replayability": replayability,
            "network_source": {"source_id": "live"},
            "dom_cleaner": {"cleaner_type": "browser_use"},
            "dom_serializer": {"serializer_type": "text"},
            "backend": {"provider_id": "local_chrome"},
        }
    )


class TestRegistryAndGeneratedConfig:
    def test_both_tiers_are_registered(self) -> None:
        assert sorted(ReplayabilityRegistrar.keys()) == ["none", "pinned"]
        assert ReplayabilityRegistrar.get("none") is NoneReplayability
        assert ReplayabilityRegistrar.get("pinned") is PinnedReplayability

    def test_every_tier_satisfies_the_protocol(self) -> None:
        for tier in (NoneReplayability(), PinnedReplayability()):
            assert isinstance(tier, ReplayabilityProtocol)

    def test_generated_union_carries_every_pinned_knob(self) -> None:
        cfg = _capability_config(
            {
                "replayability_id": "pinned",
                "base_time": "run_start",
                "random_seed": 9,
                "timezone": "Asia/Shanghai",
                "locale": "zh-CN",
                "device_scale_factor": 2.0,
                "user_agent": "Pinned/1",
                "reduced_motion": False,
            }
        )
        r = cfg.replayability
        assert (r.replayability_id, r.random_seed, r.timezone, r.locale) == (
            "pinned",
            9,
            "Asia/Shanghai",
            "zh-CN",
        )
        assert (r.device_scale_factor, r.user_agent, r.reduced_motion) == (2.0, "Pinned/1", False)

    def test_none_validates(self) -> None:
        assert _capability_config({"replayability_id": "none"}).replayability.replayability_id == (
            "none"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"replayability_id": "pinnned"},
            {"replayability_id": "pinned", "random_seed": 0},
            {"replayability_id": "pinned", "base_time": "now"},
            {"replayability_id": "pinned", "device_scale_factor": 0},
            {"replayability_id": "none", "random_seed": 3},
            {"replayability_id": "off"},
        ],
        ids=["typo", "seed-zero", "base-time", "dpr-zero", "knob-on-none", "old-off-name"],
    )
    def test_invalid_blocks_are_rejected(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            _capability_config(bad)

    def test_a_bare_yaml_none_is_a_plain_word(self) -> None:
        """YAML 1.1 (PyYAML) reads a bare ``off`` as ``False``; ``none`` stays a string."""
        block = yaml.safe_load("replayability: { replayability_id: none }")["replayability"]
        assert _capability_config(block).replayability.replayability_id == "none"
        bare_off = yaml.safe_load("replayability: { replayability_id: off }")["replayability"]
        assert bare_off == {"replayability_id": False}
        with pytest.raises(ValidationError):
            _capability_config(bare_off)

    def test_the_capability_defaults_to_none(self) -> None:
        cap = BrowserCapability()
        assert isinstance(cap.replayability, NoneReplayability)
        assert isinstance(default_replayability(), NoneReplayability)
        assert BrowserCapability.__wiring__["replayability"] == (
            "replayability",
            ["none", "pinned"],
        )


class TestNoneIsInert:
    def test_no_switches_platform_dpr_and_an_empty_plan(self) -> None:
        tier = NoneReplayability()
        assert tier.replayable is False
        assert tier.needs_archive_clock is False
        assert tier.launch_args() == ()
        assert tier.launch_env() == {}
        assert tier.device_scale_factor == 0.0
        plan = tier.plan(t0_ms=123, browser_user_agent="UA")
        assert plan.tier == "none"
        for kind in ("page", "iframe", "worker", "shared_worker", "service_worker", "browser_ui"):
            assert plan.target_commands(_target(kind)) == ()
            assert plan.fetch_patterns(_target(kind)) == ()
        assert plan.fingerprint() == {
            "tier": "none",
            "params": {},
            "t0_ms": None,
            "shim_sha256": None,
            "replayability_args": [],
            "replayability_env": {},
            "client_hints": None,
        }


class TestPinnedSwitches:
    def test_default_switches_are_exactly_the_documented_list(self) -> None:
        pinned = PinnedReplayability()
        assert pinned.replayable is True
        assert pinned.launch_args() == PINNED_DEFAULT_ARGS
        assert "--disable-site-isolation-trials" not in pinned.launch_args()

    def test_switches_follow_the_knobs(self) -> None:
        pinned = PinnedReplayability(
            random_seed=42, locale="fr-FR", device_scale_factor=2.5, user_agent="Pinned/1"
        )
        args = pinned.launch_args()
        assert args[:3] == (
            "--js-flags=--random-seed=42",
            "--lang=fr-FR",
            "--force-device-scale-factor=2.5",
        )
        # A pinned user agent also reaches workers and the network stack, and
        # makes a provider's own user_agent knob a reportable conflict.
        assert args[-1] == "--user-agent=Pinned/1"
        assert pinned.device_scale_factor == 2.5

    def test_the_launch_environment_pins_the_default_time_zone(self) -> None:
        """``TZ``: what a frame without an override owner falls back to (§4.14)."""
        assert PinnedReplayability().launch_env() == {"TZ": "UTC"}
        assert PinnedReplayability(timezone="Asia/Tokyo").launch_env() == {"TZ": "Asia/Tokyo"}
        plan = PinnedReplayability(timezone="Asia/Tokyo").plan(t0_ms=1, browser_user_agent="UA")
        assert plan.fingerprint()["replayability_env"] == {"TZ": "Asia/Tokyo"}
        assert "TZ" not in " ".join(PinnedReplayability().launch_args()), "never a switch"

    def test_base_time_from_archive_needs_an_archive_clock(self) -> None:
        assert PinnedReplayability().needs_archive_clock is False
        assert PinnedReplayability(base_time="from_archive").needs_archive_clock is True

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"timezone": "Mars/Olympus"}, "unknown time zone"),
            ({"locale": "en_US"}, "language tag"),
            ({"locale": "english please"}, "language tag"),
        ],
    )
    def test_bad_time_zone_or_locale_fails_at_construction(
        self, kwargs: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            PinnedReplayability(**kwargs)

    def test_accept_language_is_a_plain_list(self) -> None:
        assert PinnedReplayability(locale="en-US").accept_language() == "en-US,en"
        assert PinnedReplayability(locale="de").accept_language() == "de"


class TestPinnedPlan:
    @pytest.fixture
    def plan(self):
        return PinnedReplayability(
            random_seed=5, timezone="Asia/Tokyo", locale="fr-FR", user_agent=None
        ).plan(t0_ms=1_700_000_000_000, browser_user_agent="Browser/153")

    def test_frames_enable_page_before_the_init_script(self, plan) -> None:
        for kind in ("page", "iframe"):
            methods = [c.method for c in plan.target_commands(_target(kind))]
            assert methods.index("Page.enable") < methods.index(
                "Page.addScriptToEvaluateOnNewDocument"
            )
            assert methods == [
                "Page.enable",
                "Page.addScriptToEvaluateOnNewDocument",
                "Emulation.setTimezoneOverride",
                "Emulation.setLocaleOverride",
                "Emulation.setUserAgentOverride",
                "Emulation.setEmulatedMedia",
                "Network.enable",
                "Network.setCacheDisabled",
                "Network.setBypassServiceWorker",
            ]

    def test_frame_command_parameters(self, plan) -> None:
        commands = {c.method: dict(c.params) for c in plan.target_commands(_target("page"))}
        assert commands["Page.addScriptToEvaluateOnNewDocument"] == {
            # No start given: the run starts at T0 itself (base_time: run_start).
            "source": render_shim(
                t0_ms=1_700_000_000_000, run_started_ms=1_700_000_000_000, seed=5
            ),
            # The document already there when the target is prepared is pinned too.
            "runImmediately": True,
        }
        assert commands["Emulation.setTimezoneOverride"] == {"timezoneId": "Asia/Tokyo"}
        assert commands["Emulation.setLocaleOverride"] == {"locale": "fr-FR"}
        # user_agent None: the browser's own UA, Accept-Language pinned.
        assert commands["Emulation.setUserAgentOverride"] == {
            "userAgent": "Browser/153",
            "acceptLanguage": "fr-FR,fr",
        }
        assert commands["Emulation.setEmulatedMedia"] == {
            "features": [{"name": "prefers-reduced-motion", "value": "reduce"}]
        }
        assert commands["Network.setCacheDisabled"] == {"cacheDisabled": True}
        assert commands["Network.setBypassServiceWorker"] == {"bypass": True}

    def test_workers_get_the_shim_by_evaluation(self, plan) -> None:
        shim = render_shim(t0_ms=1_700_000_000_000, run_started_ms=1_700_000_000_000, seed=5)
        for kind in ("worker", "shared_worker", "service_worker"):
            commands = list(plan.target_commands(_target(kind)))
            assert commands[0].method == "Runtime.evaluate"
            assert dict(commands[0].params) == {"expression": shim}
            assert [c.method for c in commands[1:]] == [
                "Network.enable",
                "Network.setUserAgentOverride",
                "Network.setCacheDisabled",
            ]
            assert not any(c.method.startswith(("Page.", "Emulation.")) for c in commands)

    def test_other_targets_get_nothing(self, plan) -> None:
        assert plan.target_commands(_target("browser_ui")) == ()
        assert plan.fetch_patterns(_target("page")) == ()

    def test_no_per_run_cache_clear(self, plan) -> None:
        """Measured 3.9–9.3 s per setup on Chrome 153, against an empty cache.

        The profile is fresh for every pinned run (the cooperation check
        refuses any other) and the cache is disabled on every target.
        """
        everything = [
            c.method
            for kind in ("page", "iframe", "worker", "shared_worker", "service_worker")
            for c in plan.target_commands(_target(kind))
        ]
        assert "Network.clearBrowserCache" not in everything

    def test_reduced_motion_off_sends_no_media_override(self) -> None:
        plan = PinnedReplayability(reduced_motion=False).plan(t0_ms=1, browser_user_agent="UA")
        methods = [c.method for c in plan.target_commands(_target("page"))]
        assert "Emulation.setEmulatedMedia" not in methods

    def test_a_pinned_user_agent_wins_over_the_browsers(self) -> None:
        plan = PinnedReplayability(user_agent="Pinned/2").plan(t0_ms=1, browser_user_agent="B/1")
        frame = {c.method: dict(c.params) for c in plan.target_commands(_target("page"))}
        worker = {c.method: dict(c.params) for c in plan.target_commands(_target("worker"))}
        assert frame["Emulation.setUserAgentOverride"]["userAgent"] == "Pinned/2"
        assert worker["Network.setUserAgentOverride"]["userAgent"] == "Pinned/2"

    def test_fingerprint_fields(self, plan) -> None:
        print_ = plan.fingerprint()
        assert print_["tier"] == "pinned"
        assert print_["t0_ms"] == 1_700_000_000_000
        assert print_["shim_sha256"] == SHIM_SHA256
        assert print_["replayability_args"][0] == "--js-flags=--random-seed=5"
        assert print_["params"] == {
            "base_time": "run_start",
            "random_seed": 5,
            "timezone": "Asia/Tokyo",
            "locale": "fr-FR",
            "device_scale_factor": 1.0,
            "user_agent": None,
            "reduced_motion": True,
        }

    def test_a_replay_shifts_the_clock_to_the_recordings_t0(self) -> None:
        """T0 is the recording's, the start is the replay's own: the shim gets both.

        The page then reads the recording's T0 at the replay's start and advances
        in real time from there (``shims.py``: one clock for the whole run).
        """
        plan = PinnedReplayability(base_time="from_archive", random_seed=5).plan(
            t0_ms=1_760_000_000_000, run_started_ms=1_790_000_000_000, browser_user_agent="UA"
        )
        commands = {c.method: dict(c.params) for c in plan.target_commands(_target("page"))}
        assert commands["Page.addScriptToEvaluateOnNewDocument"]["source"] == render_shim(
            t0_ms=1_760_000_000_000, run_started_ms=1_790_000_000_000, seed=5
        )
        assert plan.fingerprint()["t0_ms"] == 1_760_000_000_000

    def test_the_configured_instance_keeps_no_run_state(self) -> None:
        pinned = PinnedReplayability()
        first = pinned.plan(t0_ms=1, browser_user_agent="A")
        second = pinned.plan(t0_ms=2, browser_user_agent="B")
        assert first.t0_ms == 1 and second.t0_ms == 2
        assert not [k for k in vars(pinned) if "t0" in k or "plan" in k]


class TestShim:
    def test_render_fills_t0_start_and_seed(self) -> None:
        shim = render_shim(t0_ms=1_700_000_000_123, run_started_ms=1_700_000_005_000, seed=77)
        assert "__AF_" not in shim
        assert "const T0_MS = 1700000000123;" in shim
        assert "const RUN_STARTED_MS = 1700000005000;" in shim
        assert "const SEED = 77 >>> 0;" in shim

    def test_the_clock_is_one_clock_for_the_whole_run(self) -> None:
        """Real time from the run's start, not a per-document counter.

        The per-document counter (WPR's: T0, then 50 ms every 25
        constructions) gave every tab its own time, and iqiyi.com's cross-tab
        storage sync never converged under it (``shims.py`` module docstring).
        The behaviour itself is checked in a real Chrome
        (``test_browser_recorder_live.py``, ``TestPinnedClock``).
        """
        body = SHIM_TEMPLATE.split("(function ()", 1)[1]
        assert "perf.timeOrigin" in body and "perf.now" in body
        assert "T0_MS + (wallMs() - RUN_STARTED_MS)" in body
        assert "constructions" not in body

    def test_digest_is_the_templates_whatever_the_run(self) -> None:
        assert hashlib.sha256(SHIM_TEMPLATE.encode()).hexdigest() == SHIM_SHA256
        assert re.fullmatch(r"[0-9a-f]{64}", SHIM_SHA256)
        plan_a = PinnedReplayability(random_seed=1).plan(t0_ms=1, browser_user_agent="x")
        plan_b = PinnedReplayability(random_seed=2).plan(t0_ms=2, browser_user_agent="x")
        assert plan_a.shim_sha256 == plan_b.shim_sha256 == SHIM_SHA256

    def test_the_wpr_notice_travels_with_the_source(self) -> None:
        assert "Copyright 2017 The Chromium Authors" in SHIM_TEMPLATE
        assert "BSD-style license" in SHIM_TEMPLATE
        assert "6a31bb0937dccd01bedae8bd6cdeb8c113937cfb" in SHIM_TEMPLATE
        assert "Modified by af" in SHIM_TEMPLATE

    def test_the_shim_is_guarded_for_workers_and_reinjection(self) -> None:
        assert "Symbol.for('af.replayability.pinned')" in SHIM_TEMPLATE
        assert "typeof g.ServiceWorkerContainer === 'function'" in SHIM_TEMPLATE
        assert "typeof g.crypto === 'object'" in SHIM_TEMPLATE
        # WPR's hard-coded time-zone offset is not ported: tz is a CDP override.
        assert "getTimezoneOffset" not in SHIM_TEMPLATE.split("(function ()", 1)[1]

    @pytest.mark.parametrize("bad", [1.5, "1", True])
    def test_render_refuses_non_integers(self, bad: Any) -> None:
        with pytest.raises(TypeError):
            render_shim(t0_ms=bad, run_started_ms=1, seed=1)
        with pytest.raises(TypeError):
            render_shim(t0_ms=1, run_started_ms=bad, seed=1)
        with pytest.raises(TypeError):
            render_shim(t0_ms=1, run_started_ms=1, seed=bad)


# ── Cooperation (setup-time refusals) ─────────────────────────────────


class _Provider:
    """A provider double; ``__capabilities__`` decides what it may be handed."""

    __capabilities__: ClassVar[set[str]] = {CDP, NETWORK_CAPTURE, LAUNCH_ARGS}
    provider_id = "fake_local"

    def __init__(self, *, conflicts: list[str] | None = None, profile: str | None = None):
        self._conflicts = conflicts or []
        self._profile = profile
        self.open_calls: list[dict[str, Any]] = []

    async def open(self, **kwargs: Any) -> ProviderSession:
        self.open_calls.append(kwargs)
        return ProviderSession(session_id="s", provider_id=self.provider_id, cdp_url="ws://x")

    async def close(self, session: ProviderSession | None = None) -> None:
        return None

    def launch_arg_conflicts(self, launch_args: Any) -> list[str]:
        return list(self._conflicts)

    def planned_launch_flags(self, launch_args: Any, launch_env: Any = None) -> list[str]:
        env = [f"{name}={value}" for name, value in sorted((launch_env or {}).items())]
        return [*env, "--headless=new", *launch_args]

    def planned_browser_defaults(self, launch_env: Any = None) -> BrowserDefaults:
        return BrowserDefaults(timezone=(launch_env or {}).get("TZ"), locale=None)

    def persistent_profile(self) -> str | None:
        return self._profile


class _CloudProvider(_Provider):
    __capabilities__: ClassVar[set[str]] = {CDP}
    provider_id = "fake_cloud"


def _pinned_capability(backend: Any, **pinned: Any) -> BrowserCapability:
    cap = BrowserCapability()
    cap.replayability = PinnedReplayability(**pinned)
    cap.backend = backend  # type: ignore[assignment]
    return cap


class TestCooperation:
    def test_off_needs_nothing_from_any_provider(self) -> None:
        cap = BrowserCapability()
        cap.backend = _CloudProvider()  # type: ignore[assignment]
        assert cooperation_problems(cap, cdp_url=None, injected_client=False) == []
        assert cooperation_problems(cap, cdp_url="ws://remote", injected_client=True) == []

    def test_pinned_on_its_own_browser_cooperates(self) -> None:
        cap = _pinned_capability(_Provider())
        assert cooperation_problems(cap, cdp_url=None, injected_client=False) == []

    def test_pinned_vs_a_provider_without_network_capture_or_launch_args(self) -> None:
        problems = cooperation_problems(
            _pinned_capability(_CloudProvider()), cdp_url=None, injected_client=False
        )
        assert len(problems) == 2
        assert all(
            p.startswith("replayability: pinned ↔ backend provider_id='fake_cloud'")
            for p in problems
        )
        assert "NETWORK_CAPTURE" in problems[0]
        assert "LAUNCH_ARGS" in problems[1]

    def test_pinned_vs_an_explicit_cdp_url(self) -> None:
        problems = cooperation_problems(
            _pinned_capability(_Provider()), cdp_url="ws://127.0.0.1:9/x", injected_client=False
        )
        assert problems == [
            "replayability: pinned ↔ cdp_url: an explicit CDP endpoint is a browser this "
            "capability did not launch, and the recorder connection may only run on one it owns"
        ]
        assert "127.0.0.1" not in problems[0]  # the endpoint may carry a token

    def test_pinned_vs_an_injected_control_client(self) -> None:
        problems = cooperation_problems(
            _pinned_capability(_Provider()), cdp_url=None, injected_client=True
        )
        assert len(problems) == 1
        assert problems[0].startswith("replayability: pinned ↔ injected control client")

    def test_pinned_vs_provider_knob_conflicts(self) -> None:
        cap = _pinned_capability(
            _Provider(
                conflicts=["deterministic_rendering=true contradicts --js-flags=--random-seed=1"]
            )
        )
        assert cooperation_problems(cap, cdp_url=None, injected_client=False) == [
            "replayability: pinned ↔ backend provider_id='fake_local' knob "
            "deterministic_rendering=true contradicts --js-flags=--random-seed=1"
        ]

    def test_pinned_vs_a_persistent_profile(self) -> None:
        cap = _pinned_capability(_Provider(profile="user_data_dir='/home/me/chrome'"))
        (problem,) = cooperation_problems(cap, cdp_url=None, injected_client=False)
        assert problem.startswith("replayability: pinned ↔ backend provider_id='fake_local'")
        assert "user_data_dir='/home/me/chrome'" in problem and "fresh profile" in problem

    def test_pinned_from_archive_vs_a_live_network_source(self) -> None:
        cap = _pinned_capability(_Provider(), base_time="from_archive")
        (problem,) = cooperation_problems(cap, cdp_url=None, injected_client=False)
        assert problem.startswith(
            "replayability: pinned base_time=from_archive ↔ network_source: live"
        )

    def test_pinned_from_archive_with_an_archive_source_is_not_this_rows_problem(self) -> None:
        """The clock row names the live source only; an archive source answers it (M30)."""
        from spectragent.core.capabilities.browser.replay import ArchiveNetworkSource

        cap = _pinned_capability(_Provider(), base_time="from_archive")
        cap.network_source = ArchiveNetworkSource(isolation="none_acknowledged")
        problems = cooperation_problems(
            cap, cdp_url=None, injected_client=False, replay_archive="/runs/src"
        )
        assert not any("base_time" in problem for problem in problems)

    def test_every_problem_is_reported_at_once(self) -> None:
        cap = _pinned_capability(
            _Provider(
                conflicts=["user_agent contradicts --user-agent=X"], profile="profile_directory='W'"
            ),
            base_time="from_archive",
        )
        problems = cooperation_problems(cap, cdp_url=None, injected_client=False)
        assert len(problems) == 3
        error = BrowserCooperationError(problems)
        assert error.problems == problems
        for problem in problems:
            assert problem in str(error)

    def test_the_real_local_chrome_reports_its_own_knobs(self) -> None:
        cap = _pinned_capability(
            LocalChromeProvider(deterministic_rendering=True, user_data_dir="/p"),
            device_scale_factor=2.0,
        )
        problems = cooperation_problems(cap, cdp_url=None, injected_client=False)
        assert [p.split(" knob ")[-1].split(" contradicts")[0] for p in problems[:1]] == [
            "deterministic_rendering=true"
        ]
        assert any("user_data_dir='/p'" in p for p in problems)

    def test_a_refused_setup_launches_nothing(self) -> None:
        backend = _CloudProvider()
        cap = _pinned_capability(backend)
        with pytest.raises(BrowserCooperationError) as info:
            asyncio.run(cap.setup())
        assert backend.open_calls == []
        assert len(info.value.problems) == 2
        assert cap._scope_group is None and cap._cdp is None

    def test_setup_refuses_an_explicit_url_before_connecting(self) -> None:
        cap = _pinned_capability(_Provider())
        with pytest.raises(BrowserCooperationError, match="cdp_url"):
            asyncio.run(cap.setup(cdp_url="ws://127.0.0.1:1/devtools/browser/x"))
        assert cap._cdp is None
