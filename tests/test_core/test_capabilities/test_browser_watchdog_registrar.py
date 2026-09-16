"""F9 — watchdog axis is a registrar, and observation cost is negotiated.

Three claims are under test here, all of which were false before:

1. **"Adding a fragment = adding one class"** (``capability.py`` header,
   CLAUDE.md §2.4). It used to take edits in nine places: two watchdog
   name sets, the fixed-field cache dataclass, ``__wiring__``, the
   ``resolve_fragment`` if/elif chain, the event's include flags, the
   result-harvest loop, an in-function factory dict, and every
   ``BUILTIN_PROFILES`` entry. ``TestNewFragmentIsOneClass`` adds a real
   fragment at runtime and touches none of them.

2. **Negotiation controls cost.** The ``BrowserStateRequestEvent``
   include flags were computed from the capability's *full* provides set,
   so a vision-only agent paid for a full DOM extraction every step and
   negotiation saved nothing. ``TestNegotiationDrivenCost`` measures the
   CDP calls on both paths.

3. **An unknown watchdog is a hard error.** It used to be a
   ``logger.warning``; the fragment then resolved to ``None`` and, since
   env fragments are required on the negotiated observation model,
   surfaced as a pydantic ``ValidationError`` naming the *fragment* —
   the wrong layer entirely.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from agent_cdp import ConnectionType, EventScope, ScopeGroup, WatchdogRegistrar

# Concrete DOM strategies register on import. Production gets these via the
# composer's recursive ``discover()``; these tests build watchdogs directly.
import spectragent.core.capabilities.browser.dom.cleaning.browser_use  # noqa: F401
import spectragent.core.capabilities.browser.dom.serializers.text_serializer  # noqa: F401
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.profiles import BUILTIN_PROFILES
from spectragent.core.capabilities.browser.watchdogs import (
    BrowserWatchdog,
    CrashWatchdog,
    DOMWatchdog,
    ScreenshotWatchdog,
    WatchdogContext,
    browser_watchdog_classes,
    validate_watchdog_names,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
)
from spectragent.schemas.observation._registrar import FragmentBase, ObservationRegistrar

# CDP methods that only a DOM extraction issues. Presence of any of these
# is the measurable signal that the expensive path ran.
_DOM_CDP_METHODS = frozenset(
    {
        "DOM.getDocument",
        "DOMSnapshot.captureSnapshot",
        "Accessibility.getFullAXTree",
    }
)


class _RecordingCDP:
    """CDP stub that records every method the watchdogs actually send."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent: list[str] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.sent.append(method)
        if method == "Page.captureScreenshot":
            return {"data": "aGk="}  # b"hi"
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": "u", "title": "t"}]}
        # Enough for setup() to reach the end without a real browser.
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "sid1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "f1"}}}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def dom_calls(self) -> list[str]:
        return [m for m in self.sent if m in _DOM_CDP_METHODS]


async def _observe_once(
    cap: BrowserCapability,
    cdp: _RecordingCDP,
    fragment_key: str,
) -> tuple[Any, ScopeGroup]:
    """Drive one real observe cycle through a real agent-cdp EventScope.

    Not a mock of the event bus: watchdogs are built by the capability
    from the registry, attached to a genuine ``EventScope``, and answer a
    genuine ``BrowserStateRequestEvent``. What is faked is only the CDP
    socket, which is what lets us count round trips.
    """
    cap._cdp = cdp  # type: ignore[assignment]
    group = ScopeGroup("test")
    scope: EventScope = await group.create_scope("tab1", target_id="tab1")
    for wd in cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid1"):
        wd.attach(scope)
    cap._scopes["sid1"] = scope
    cap._default_session_id = "sid1"
    fragment = await cap.resolve_fragment(fragment_key)
    return fragment, group


# ─── 1. adding a fragment is one class ────────────────────────────────


@pytest.fixture
def network_watchdog() -> Iterator[type[BrowserWatchdog]]:
    """Register a new fragment + its watchdog, then clean up.

    This is the *whole* diff a fragment author writes: one fragment class
    and one watchdog class. Nothing in ``capability.py``, ``profiles.py``,
    ``events.py`` or ``__wiring__`` is touched — if any of them still
    needed an edit, the assertions below would fail.
    """

    class NetworkObs(FragmentBase):
        """A genuinely new env fragment, defined entirely in this test."""

        __registry_key__ = "browser.network"
        __source__: ClassVar[str] = "env"

        request_count: int = 0

    class NetworkWatchdog(BrowserWatchdog):
        level: ClassVar[str] = "tab"
        provides: ClassVar[frozenset[str]] = frozenset({"browser.network"})

        def __init__(self, cdp: Any) -> None:
            self._cdp = cdp

        @classmethod
        def build(cls, ctx: WatchdogContext) -> Any:
            return cls(ctx.cdp)

        def attach(self, scope: EventScope, session_id: str | None = None) -> None:
            scope.connect(
                BrowserStateRequestEvent,
                self._on_state_request,
                mode=ConnectionType.QUEUED,
            )

        async def _on_state_request(
            self, event: BrowserStateRequestEvent
        ) -> BrowserStateSummary | None:
            if not (event.fragments & self.provides):
                return None
            await self._cdp.send("Network.getRequestPostData", {})
            return BrowserStateSummary(
                fragments={"browser.network": NetworkObs(request_count=7)}
            )

    NetworkWatchdog.fragment_cls = NetworkObs  # type: ignore[attr-defined]
    yield NetworkWatchdog

    WatchdogRegistrar.unregister("network")
    ObservationRegistrar.unregister("browser.network")


class TestNewFragmentIsOneClass:
    def test_registration_is_automatic(
        self, network_watchdog: type[BrowserWatchdog]
    ) -> None:
        # Subclassing BrowserWatchdog registered it under agent_cdp's
        # registrar with the suffix stripped. No table to update.
        assert browser_watchdog_classes()["network"] is network_watchdog

    def test_capability_advertises_it_without_code_changes(
        self, network_watchdog: type[BrowserWatchdog]
    ) -> None:
        cap = BrowserCapability()
        assert "browser.network" in cap.declared_fragments
        assert "browser.network" in cap.provided_fragments

    def test_every_builtin_profile_installs_it(
        self, network_watchdog: type[BrowserWatchdog]
    ) -> None:
        # Profiles list exclusions, so a new watchdog joins all of them.
        # Previously each profile enumerated watchdog names and a new one
        # was silently absent from every single entry.
        for name in BUILTIN_PROFILES:
            cap = BrowserCapability(profile=name)
            assert "network" in cap._watchdog_classes(), name

    async def test_capability_resolves_it_end_to_end(
        self, network_watchdog: type[BrowserWatchdog]
    ) -> None:
        cap = BrowserCapability()
        cap.apply_negotiation({"browser.network"})
        cdp = _RecordingCDP()
        fragment, group = await _observe_once(cap, cdp, "browser.network")
        try:
            assert isinstance(fragment, network_watchdog.fragment_cls)
            assert fragment.request_count == 7
            # ...and only the new watchdog did any work.
            assert cdp.sent == ["Network.getRequestPostData"]
        finally:
            await group.close_all()


# ─── 2. negotiation drives per-step cost ──────────────────────────────


class TestNegotiationDrivenCost:
    """The measurable payoff: a vision-only agent stops paying for DOM."""

    async def test_vision_only_agent_never_extracts_dom(self) -> None:
        cap = BrowserCapability(profile="standard")
        # Exactly what negotiation produces for a screenshot-only agent
        # (UI-TARS) running against a DOM-capable profile.
        cap.apply_negotiation({"browser.screenshot"})
        cdp = _RecordingCDP()

        fragment, group = await _observe_once(cap, cdp, "browser.screenshot")
        try:
            assert fragment.image_bytes == b"hi"
            assert cdp.dom_calls() == [], (
                "a vision-only agent must not trigger DOM extraction"
            )
            # One round trip for the whole step.
            assert cdp.sent == ["Page.captureScreenshot"]
        finally:
            await group.close_all()

    async def test_dom_agent_still_extracts_dom(self) -> None:
        cap = BrowserCapability(profile="standard")
        cap.apply_negotiation({"browser.dom", "browser.screenshot"})
        cdp = _RecordingCDP()

        fragment, group = await _observe_once(cap, cdp, "browser.dom")
        try:
            assert fragment is not None
            assert sorted(cdp.dom_calls()) == sorted(_DOM_CDP_METHODS)
        finally:
            await group.close_all()

    async def test_without_negotiation_the_full_set_is_paid_for(self) -> None:
        """Pins the regression: no negotiation == the old behaviour.

        ``provided_fragments`` falls back to everything the profile can
        resolve, which is precisely what the include flags used to be
        computed from unconditionally.
        """
        cap = BrowserCapability(profile="standard")
        assert cap._negotiated_fragments is None
        cdp = _RecordingCDP()

        _, group = await _observe_once(cap, cdp, "browser.screenshot")
        try:
            assert cdp.dom_calls(), (
                "un-negotiated capability resolves everything it can — this "
                "is the cost the negotiated path avoids"
            )
        finally:
            await group.close_all()

    def test_event_carries_exactly_the_negotiated_set(self) -> None:
        cap = BrowserCapability(profile="standard")
        cap.apply_negotiation({"browser.screenshot"})
        assert cap.provided_fragments == frozenset({"browser.screenshot"})

        event = BrowserStateRequestEvent(fragments=cap.provided_fragments)
        assert not (event.fragments & DOMWatchdog.provides)
        assert event.fragments & ScreenshotWatchdog.provides

    def test_negotiation_narrows_but_cannot_widen(self) -> None:
        """A fragment the profile cannot serve stays unavailable."""
        cap = BrowserCapability(profile="vision_only")
        cap.apply_negotiation({"browser.dom", "browser.screenshot"})
        assert cap.provided_fragments == frozenset({"browser.screenshot"})

    async def test_setup_rejects_a_fragment_no_watchdog_resolves(self) -> None:
        cap = BrowserCapability(profile="vision_only", cdp_url="ws://x/y")
        cap.apply_negotiation({"browser.dom"})
        with pytest.raises(RuntimeError, match=r"browser\.dom"):
            await cap.setup(negotiation={"browser.dom"})

    async def test_environment_setup_threads_negotiation_to_capabilities(
        self,
    ) -> None:
        """The wiring that makes any of this reachable from a real run.

        ``AgentRuntime.setup_environment`` passes the negotiation result
        to ``EnvironmentBase.setup``, which broadcasts it to every
        capability. Without that hop the capability never learns the
        active set and falls back to resolving everything.
        """
        from spectragent.core.environments._registrar import EnvironmentBase

        class _ProbeEnv(EnvironmentBase):
            __registry_key__ = "probe_env_negotiation"

        cap = BrowserCapability(profile="standard")
        cap._inject_cdp_client(_RecordingCDP())  # type: ignore[arg-type]
        env = _ProbeEnv(capabilities={"browser#0": cap})

        assert env.provided_fragments == frozenset(
            {"browser.dom", "browser.screenshot", "browser.browser_meta"}
        )

        class _Negotiated:
            env_sourced = frozenset({"browser.screenshot"})
            active_fragments = frozenset({"browser.screenshot", "loop.task"})

        await env.setup(negotiation=_Negotiated())
        try:
            assert env.provided_fragments == frozenset({"browser.screenshot"})
        finally:
            await env.teardown()


# ─── 3. unknown watchdog is a hard error ──────────────────────────────


class TestUnknownWatchdogIsHardError:
    def test_validate_names_rejects_and_names_the_watchdog(self) -> None:
        with pytest.raises(ValueError, match="netwrok"):
            validate_watchdog_names(["dom", "netwrok"])

    def test_error_lists_the_registered_alternatives(self) -> None:
        with pytest.raises(ValueError, match="Registered watchdogs"):
            validate_watchdog_names(["nope"])

    def test_capability_fails_on_a_typo_in_the_profile(self) -> None:
        cap = BrowserCapability()
        cap.profile_config.watchdogs_exclude.append("scrensht")
        # Not a downstream fragment ValidationError — it names the
        # watchdog, at the layer that owns the mistake.
        with pytest.raises(ValueError, match="scrensht"):
            cap._watchdog_classes()

    def test_known_names_pass(self) -> None:
        assert validate_watchdog_names(["dom", "screenshot"]) == frozenset(
            {"dom", "screenshot"}
        )


# ─── watchdog contract ────────────────────────────────────────────────


class TestWatchdogContract:
    def test_every_registered_watchdog_declares_a_level(self) -> None:
        for name, cls in browser_watchdog_classes().items():
            assert cls.level in ("tab", "browser"), name

    def test_every_registered_watchdog_is_buildable_from_context(self) -> None:
        cap = BrowserCapability()
        cap._cdp = _RecordingCDP()  # type: ignore[assignment]
        ctx = cap._watchdog_context("sid1")
        for name, cls in browser_watchdog_classes().items():
            assert isinstance(cls.build(ctx), cls), name

    def test_crash_watchdog_is_browser_level(self) -> None:
        # The only browser-level watchdog; used to live in a
        # ``_BROWSER_WATCHDOGS`` string set in capability.py.
        assert CrashWatchdog.level == "browser"
        cap = BrowserCapability()
        cap._cdp = _RecordingCDP()  # type: ignore[assignment]
        built = cap._create_watchdogs_for_scope(level="browser")
        assert [type(w) for w in built] == [CrashWatchdog]

    def test_detach_is_on_the_contract_not_probed_for(self) -> None:
        # agent-cdp's WatchdogProtocol only requires ``attach``; the
        # capability used to probe with getattr before every detach.
        for name, cls in browser_watchdog_classes().items():
            assert callable(getattr(cls, "detach", None)), name

    def test_fragment_watchdogs_are_disjoint(self) -> None:
        """Two watchdogs claiming one fragment would race in the harvest."""
        seen: set[str] = set()
        for name, cls in browser_watchdog_classes().items():
            overlap = seen & cls.provides
            assert not overlap, f"{name} re-provides {sorted(overlap)}"
            seen |= cls.provides


# ─── mouse_trajectory is a wired receptor, not a bare str ────────────


class TestMouseTrajectoryIsAWiredReceptor:
    """The strategy is chosen *and configured* from YAML.

    It used to be a ``Literal`` naming the strategy plus an untyped
    ``mouse_trajectory_overrides`` dict for its knobs. ``__config_annotated_only__``
    dropped that dict from the generated model and ``extra="forbid"``
    then rejected it, so every per-strategy knob (windmouse gravity /
    wind / seed, bezier control-point spread) was declared in Python and
    unreachable from a config file.
    """

    @staticmethod
    def _member_configs() -> list[type]:
        from typing import get_args

        from spectragent.generated._composed import BrowserCapabilityConfig

        annotation = BrowserCapabilityConfig.model_fields[
            "mouse_trajectory"
        ].annotation
        return list(get_args(annotation))

    def test_generated_config_is_a_union_of_strategy_configs(self) -> None:
        names = sorted(c.__name__ for c in self._member_configs())
        assert names == [
            "BezierMouse_TrajectoryConfig",
            "GanMouse_TrajectoryConfig",
            "InstantMouse_TrajectoryConfig",
            "WindmouseMouse_TrajectoryConfig",
        ]

    def test_union_matches_the_registry(self) -> None:
        """Guards the union against registry drift."""
        from spectragent.core.capabilities.browser.input import (
            MouseTrajectoryRegistrar,
        )

        keys = sorted(
            c.model_fields["trajectory_type"].default for c in self._member_configs()
        )
        assert keys == sorted(MouseTrajectoryRegistrar.keys())

    def test_per_strategy_knobs_are_in_the_generated_config(self) -> None:
        """The whole point: the knobs are reachable and typed."""
        from spectragent.generated._composed import WindmouseMouse_TrajectoryConfig

        fields = WindmouseMouse_TrajectoryConfig.model_fields
        assert {"gravity", "wind", "seed"} <= set(fields)

    def test_generated_config_rejects_a_typo(self) -> None:
        from pydantic import ValidationError

        from spectragent.generated._composed import BrowserCapabilityConfig

        ok = BrowserCapabilityConfig.model_validate(
            {
                "mouse_trajectory": {"trajectory_type": "windmouse", "gravity": 12.0},
                "dom_cleaner": {"cleaner_type": "browser_use"},
                "backend": {"provider_id": "local_chrome"},
            }
        )
        assert ok.mouse_trajectory.gravity == 12.0  # type: ignore[union-attr]
        with pytest.raises(ValidationError):
            BrowserCapabilityConfig.model_validate(
                {
                    "mouse_trajectory": {"trajectory_type": "windmoose"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "backend": {"provider_id": "local_chrome"},
                }
            )

    def test_there_is_no_humanize_flag_to_override_the_strategy(self) -> None:
        """``humanize_mouse: false`` used to silently discard a configured strategy.

        The strategy is now the only switch (``instant`` == not humanized), so
        the old key is an unknown field and fails validation loudly.
        """
        from pydantic import ValidationError

        from spectragent.generated._composed import BrowserCapabilityConfig

        assert "humanize_mouse" not in BrowserCapabilityConfig.model_fields
        with pytest.raises(ValidationError, match="humanize_mouse"):
            BrowserCapabilityConfig.model_validate(
                {
                    "humanize_mouse": False,
                    "mouse_trajectory": {"trajectory_type": "windmouse"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "backend": {"provider_id": "local_chrome"},
                }
            )

    def test_unset_receptor_defaults_to_instant_in_python(self) -> None:
        """Direct construction (no Composer) still has a working cursor."""
        from spectragent.core.capabilities.browser.input import InstantTrajectory

        assert isinstance(BrowserCapability().mouse_trajectory, InstantTrajectory)


# ─── dom_cleaner is a wired receptor, not a name on the profile ──────


#: A control that paints its own face: an opaque child exactly covering an
#: interactive parent, which is the shape ``descendants_occlude`` decides on
#: (reCAPTCHA's ``#recaptcha-anchor`` under its checkbox border).
def _self_painting_control() -> Any:
    from tests._helpers.dom_payloads import Doc, El, compile_target

    return compile_target(
        Doc(
            "MAIN",
            html_id=1,
            body=[
                El(
                    "div",
                    20,
                    bounds=(490, 265, 50, 50),
                    attrs={"id": "holder"},
                    children=[
                        El(
                            "div",
                            21,
                            bounds=(501, 275, 28, 28),
                            attrs={"id": "anchor", "role": "checkbox", "tabindex": "0"},
                            paint_order=4,
                            children=[
                                El(
                                    "div",
                                    22,
                                    bounds=(501, 275, 28, 28),
                                    attrs={"id": "face"},
                                    paint_order=10,
                                    styles={"background-color": "rgb(255, 255, 255)"},
                                )
                            ],
                        )
                    ],
                )
            ],
        ),
        session_id="sid1",
    )


class _PageCDP(_RecordingCDP):
    """``_RecordingCDP`` that also answers the three DOM captures."""

    def __init__(self, capture: Any) -> None:
        super().__init__()
        self._capture = capture

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        answer = await super().send(method, params, session_id)
        if method == "DOM.getDocument":
            return self._capture.raw_dom
        if method == "DOMSnapshot.captureSnapshot":
            return self._capture.snapshot
        if method == "Accessibility.getFullAXTree":
            return self._capture.ax_tree
        return answer


class TestDomCleanerIsAWiredReceptor:
    """The cleaning strategy is chosen *and configured* from YAML.

    It used to be a name on ``BrowserProfileConfig``: ``DOMWatchdog`` looked
    the class up and default-constructed it, so every knob the generated
    ``BrowserUseDom_CleanerConfig`` declared — ``descendants_occlude``,
    ``max_occlusion_nodes``, ``include_shadow_dom`` — validated and then
    went nowhere. Same fault, same fix, same shape as ``mouse_trajectory``.
    """

    @staticmethod
    def _compose(cleaner_block: dict[str, Any]) -> BrowserCapability:
        from spectragent.core.composer import ConfigComposer
        from spectragent.scripts.generate_all import _collect_registrars

        run = ConfigComposer(registrars=_collect_registrars()).compose(
            {
                "capability": [
                    {
                        "capability_id": "browser",
                        "mouse_trajectory": {"trajectory_type": "instant"},
                        "dom_cleaner": cleaner_block,
                        "backend": {"provider_id": "local_chrome"},
                    }
                ]
            }
        )
        return run.single("capability")

    @staticmethod
    async def _indexed_ids(cap: BrowserCapability) -> set[str | None]:
        fragment, group = await _observe_once(cap, _PageCDP(_self_painting_control()), "browser.dom")
        try:
            return {
                node.attributes.get("id") for node in fragment.element_tree.selector_map.values()
            }
        finally:
            await group.close_all()

    def test_the_knob_reaches_the_instance(self) -> None:
        from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
            BrowserUseCleaner,
        )

        cap = self._compose({"cleaner_type": "browser_use", "descendants_occlude": True})
        assert isinstance(cap.dom_cleaner, BrowserUseCleaner)
        assert cap.dom_cleaner.descendants_occlude is True
        assert cap.dom_cleaner.cleaner_type == "browser_use"

    def test_the_strategy_itself_is_chosen_there(self) -> None:
        from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner

        cap = self._compose({"cleaner_type": "skyvern", "max_text_length": 42})
        assert isinstance(cap.dom_cleaner, SkyvernCleaner)
        assert cap.dom_cleaner.max_text_length == 42

    def test_the_configured_instance_is_the_one_that_cleans(self) -> None:
        cap = self._compose({"cleaner_type": "browser_use", "descendants_occlude": True})
        cap._cdp = _RecordingCDP()  # type: ignore[assignment]
        watchdog = next(
            wd
            for wd in cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid1")
            if isinstance(wd, DOMWatchdog)
        )
        assert watchdog._cleaner is cap.dom_cleaner

    @pytest.mark.parametrize(
        ("descendants_occlude", "indexed"),
        [(False, True), (True, False)],
        ids=["default", "upstream-parity"],
    )
    async def test_the_yaml_value_changes_what_is_observed(
        self, descendants_occlude: bool, indexed: bool
    ) -> None:
        """End to end: one boolean in the config decides what the agent can click.

        Everything between is real — the Composer builds the cleaner, the
        capability carries it, the watchdog cleans with it, and the answer
        comes back on the negotiated ``browser.dom`` fragment.
        """
        cap = self._compose(
            {"cleaner_type": "browser_use", "descendants_occlude": descendants_occlude}
        )
        assert ("anchor" in await self._indexed_ids(cap)) is indexed

    async def test_a_hand_built_capability_still_cleans(self) -> None:
        """No Composer: the default strategy with its defaults, as before."""
        from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
            BrowserUseCleaner,
        )

        cap = BrowserCapability()
        assert isinstance(cap.dom_cleaner, BrowserUseCleaner)
        assert "anchor" in await self._indexed_ids(cap)

    def test_the_profile_no_longer_names_a_cleaner(self) -> None:
        """Two places to pick one is what made the config field inert."""
        from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig

        assert "cleaner" not in BrowserProfileConfig.model_fields
