"""F10 — an agent declares what it needs, not who provides it.

``__registry_key__`` is fully qualified (``browser.click``) because
registration must be collision-free and ``EnvironmentBase.act`` routes on
the prefix. Both of those are kept. What moves is the *declaration*
surface: an agent that can click at a coordinate does not thereby know
whether it is clicking in a browser, a desktop VM or an Android emulator,
and making it write ``browser.click_coordinate`` welded every
computer-use agent to the one capability that happened to exist.

So negotiation runs in the agent's declaration space and resolves it
against whatever this environment holds:

- a relative key binds to the single capability offering it;
- a fully-qualified key pins that capability, ambiguity or not;
- two capabilities offering one relative key is refused, by name.

Everything past ``build_guarded_act`` is fully qualified again — the
tests at the bottom check the router never sees a short name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import pytest

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import (
    AmbiguousActionError,
    AmbiguousFragmentError,
    IncompatibleActionError,
    negotiate_action,
    negotiate_observation,
)
from spectragent.core.negotiation.action import _action_rules
from spectragent.core.negotiation.observation import _observation_rules
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

# Loop-sourced fragments (``loop.task`` & co) must be registered before
# observation negotiation can resolve a relative ``task``.
from spectragent.schemas.observation import fragments as _fragments  # noqa: F401
from spectragent.schemas.observation._registrar import FragmentBase
from tests._helpers.assembly import make_env

# ── A second capability, so "which click?" is a real question ─────────


@dataclass
class DesktopClickCoordinateAction(ActionBase):
    """The same domain verb UI-TARS emits, on a desktop VM."""

    __registry_key__ = "desktop.click_coordinate"

    executed: list[dict[str, Any]] = field(default_factory=list)

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Click at an absolute screen coordinate.",
            params={"coordinate_x": "int", "coordinate_y": "int"},
        )

    async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
        self.executed.append(params)
        return ActionResult(success=True, extracted_content="desktop click")


@dataclass
class DesktopDoneAction(ActionBase):
    __registry_key__ = "desktop.done"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(name=self.action_id, description="Finish.")

    async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
        return ActionResult(success=True, is_done=True)


@dataclass
class DesktopTypeAction(ActionBase):
    """The index-native ``type`` — present so ``type_coordinate`` has to
    win on a *trailing-segment* match rather than a prefix one."""

    __registry_key__ = "desktop.type"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Type into the focused field.",
            params={"text": "str"},
        )

    async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
        return ActionResult(success=True)


@dataclass
class DesktopTypeCoordinateAction(ActionBase):
    __registry_key__ = "desktop.type_coordinate"

    executed: list[dict[str, Any]] = field(default_factory=list)

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Type at an absolute screen coordinate.",
            params={"coordinate_x": "int", "coordinate_y": "int", "text": "str"},
        )

    async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
        self.executed.append(params)
        return ActionResult(success=True, extracted_content="desktop type")


@dataclass
class DesktopScrollAction(ActionBase):
    __registry_key__ = "desktop.scroll"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.action_id,
            description="Scroll the screen.",
            params={"direction": "up|down|left|right"},
        )

    async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
        return ActionResult(success=True)


class DesktopScreenshotObs(FragmentBase):
    """``screenshot``, provided by something that is not a browser."""

    __registry_key__ = "desktop.screenshot"
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_bytes: bytes | None = None
    viewport_width: int = 0
    viewport_height: int = 0


@dataclass
class MockDesktopCapability(CapabilityBase):
    """A desktop VM: the same domain verbs, a different provider.

    Deliberately offers ``type`` *and* ``type_coordinate``: UI-TARS needs
    the second, and a short name must bind on the whole trailing segment
    rather than on a prefix of one.
    """

    __registry_key__ = "desktop"

    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["desktop.screenshot"]),
        "action": (
            "action",
            [
                "desktop.click_coordinate",
                "desktop.type",
                "desktop.type_coordinate",
                "desktop.done",
                "desktop.scroll",
            ],
        ),
    }

    def __post_init__(self) -> None:
        self.click = DesktopClickCoordinateAction()
        self.type_coordinate = DesktopTypeCoordinateAction()
        self.done = DesktopDoneAction()
        self._by_name = {
            a.action_id: a
            for a in (
                self.click,
                DesktopTypeAction(),
                self.type_coordinate,
                self.done,
                DesktopScrollAction(),
            )
        }

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return [a.descriptor(**context) for a in self._by_name.values()]

    async def execute_action(self, action: Action) -> ActionResult:
        impl = self._by_name.get(action.name)
        if impl is None:
            return ActionResult(success=False, error=f"Unknown action: {action.name}")
        return await impl.execute(action.params, capability=self)

    async def resolve_fragment(self, fragment_key: str) -> Any:
        assert fragment_key == "desktop.screenshot"
        return DesktopScreenshotObs(
            image_bytes=b"\x89PNG", viewport_width=800, viewport_height=600
        )


# ── Agents whose wiring names no capability ───────────────────────────


class _RelativeAgent:
    """Declares domain verbs only — the F10 shape."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "action": ("action", ["click_coordinate", "done"], ["scroll"]),
        "observation": ("observation", ["screenshot"], []),
    }


class _TypeCoordinateAgent:
    """Wants the coordinate flavour and only the coordinate flavour."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "action": ("action", ["type_coordinate"], []),
    }


class _PinnedAgent:
    """The escape hatch: one provider named outright."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "action": ("action", ["desktop.click_coordinate", "done"], []),
        "observation": ("observation", ["desktop.screenshot"], []),
    }


class _ProvidesActions:
    """Environment stand-in: an action key set and nothing else."""

    def __init__(self, *keys: str) -> None:
        self.provided_actions = frozenset(keys)


_BROWSER_ACTIONS = _ProvidesActions(
    "browser.click_coordinate", "browser.done", "browser.scroll"
)
_DESKTOP_ACTIONS = _ProvidesActions(
    "desktop.click_coordinate", "desktop.done"
)
_COMPOSITE_ACTIONS = _ProvidesActions(
    *_BROWSER_ACTIONS.provided_actions, *_DESKTOP_ACTIONS.provided_actions
)
#: A composite where only ``click_coordinate`` collides, so a single pin
#: is enough to make the whole wiring resolve.
_ONE_COLLISION_ACTIONS = _ProvidesActions(
    "browser.click_coordinate",
    "desktop.click_coordinate",
    "desktop.done",
)


# ── 1. One wiring, two capabilities ───────────────────────────────────


class TestRelativeKeysResolveAgainstWhoeverIsPresent:
    def test_same_wiring_negotiates_against_browser(self) -> None:
        result = negotiate_action(_RelativeAgent(), _BROWSER_ACTIONS)
        assert result.active_actions == frozenset(
            {"browser.click_coordinate", "browser.done", "browser.scroll"}
        )

    def test_same_wiring_negotiates_against_desktop(self) -> None:
        """The acceptance case: byte-identical ``__wiring__``, other provider.

        This is what fails before F10 — ``browser.click_coordinate`` and
        ``desktop.click_coordinate`` are two registry keys and set
        subtraction cannot see that they mean the same verb.
        """
        result = negotiate_action(_RelativeAgent(), _DESKTOP_ACTIONS)
        assert result.active_actions == frozenset(
            {"desktop.click_coordinate", "desktop.done"}
        )

    def test_unprovided_optional_is_dropped_not_missing(self) -> None:
        result = negotiate_action(_RelativeAgent(), _DESKTOP_ACTIONS)
        assert not any(a.endswith(".scroll") for a in result.active_actions)

    def test_a_short_name_binds_on_the_whole_trailing_segment(self) -> None:
        """``type`` must not match ``type_coordinate`` or vice versa.

        Both exist on the mock desktop, and prefix matching here would
        silently hand the model the wrong parameter contract — the exact
        class of failure ``type`` shipped with.
        """
        provides = _ProvidesActions("desktop.type", "desktop.type_coordinate")
        assert negotiate_action(
            _TypeCoordinateAgent(), provides
        ).active_actions == frozenset({"desktop.type_coordinate"})

    def test_a_relative_key_nobody_provides_is_reported_as_written(self) -> None:
        """The diagnosis must name the string in the wiring line."""
        with pytest.raises(IncompatibleActionError) as exc:
            _action_rules(
                agent=({"click_coordinate", "type"}, set()),
                env_provides=set(_DESKTOP_ACTIONS.provided_actions),
            )
        assert exc.value.missing == {"type"}
        assert "'type'" in str(exc.value)

    def test_bindings_are_the_run_s_short_name_answer(self) -> None:
        result = negotiate_action(_RelativeAgent(), _DESKTOP_ACTIONS)
        assert result.action_bindings == {
            "click_coordinate": "desktop.click_coordinate",
            "done": "desktop.done",
        }


# ── 2. The escape hatch ───────────────────────────────────────────────


class TestFullyQualifiedKeysPinTheirProvider:
    def test_pinned_key_selects_its_capability_in_a_composite(self) -> None:
        """Writing the full key is exactly what the ambiguity error asks for."""
        with pytest.raises(AmbiguousActionError):
            negotiate_action(_RelativeAgent(), _ONE_COLLISION_ACTIONS)

        result = negotiate_action(_PinnedAgent(), _ONE_COLLISION_ACTIONS)
        assert "desktop.click_coordinate" in result.active_actions
        assert "browser.click_coordinate" not in result.active_actions
        # The un-pinned key stayed relative and resolved on its own.
        assert "desktop.done" in result.active_actions

    def test_pinned_key_does_not_fall_back_to_another_provider(self) -> None:
        """Pinning means pinning: browser-only cannot satisfy a desktop pin."""
        with pytest.raises(IncompatibleActionError) as exc:
            negotiate_action(_PinnedAgent(), _BROWSER_ACTIONS)
        assert exc.value.missing == {"desktop.click_coordinate"}

    def test_pinning_one_key_leaves_the_others_relative(self) -> None:
        result = negotiate_action(_PinnedAgent(), _DESKTOP_ACTIONS)
        assert result.active_actions == frozenset(
            {"desktop.click_coordinate", "desktop.done"}
        )


# ── 3. Ambiguity is refused, with both candidates named ───────────────


class TestAmbiguityIsRefused:
    def test_two_providers_of_one_relative_action_raise(self) -> None:
        with pytest.raises(AmbiguousActionError) as exc:
            negotiate_action(_RelativeAgent(), _COMPOSITE_ACTIONS)
        assert exc.value.declared == "click_coordinate"
        assert exc.value.candidates == (
            "browser.click_coordinate",
            "desktop.click_coordinate",
        )

    def test_the_message_names_both_candidates_and_the_remedy(self) -> None:
        with pytest.raises(AmbiguousActionError) as exc:
            negotiate_action(_RelativeAgent(), _COMPOSITE_ACTIONS)
        message = str(exc.value)
        assert "browser.click_coordinate" in message
        assert "desktop.click_coordinate" in message
        assert "fully-qualified" in message

    def test_an_ambiguous_optional_action_also_raises(self) -> None:
        """Dropping it would be the silent degradation this rule exists for."""
        with pytest.raises(AmbiguousActionError) as exc:
            _action_rules(
                agent=(set(), {"done"}),
                env_provides={"browser.done", "desktop.done"},
            )
        assert exc.value.declared == "done"

    def test_observation_refuses_the_same_way(self) -> None:
        with pytest.raises(AmbiguousFragmentError) as exc:
            _observation_rules(
                agent=({"screenshot"}, set()),
                loop=(set(), set()),
                env_provides={"browser.screenshot", "desktop.screenshot"},
            )
        assert exc.value.candidates == (
            "browser.screenshot",
            "desktop.screenshot",
        )


# ── 4. Observation: same rule, and loop fragments still resolve ───────


class TestObservationNegotiatesRelatively:
    def test_relative_fragment_binds_to_the_only_provider(self) -> None:
        result = _observation_rules(
            agent=({"screenshot"}, set()),
            loop=(set(), set()),
            env_provides={"desktop.screenshot"},
        )
        assert result.active_fragments == frozenset({"desktop.screenshot"})
        assert result.env_sourced == frozenset({"desktop.screenshot"})
        assert result.fragment_bindings == {"screenshot": "desktop.screenshot"}

    def test_a_loop_fragment_resolves_without_the_environment(self) -> None:
        """``task`` has no capability behind it, and still binds."""
        result = _observation_rules(
            agent=(set(), set()),
            loop=({"task"}, set()),
            env_provides=set(),
        )
        assert result.loop_sourced == frozenset({"loop.task"})
        assert result.fragment_bindings == {"task": "loop.task"}

    def test_pinned_fragment_wins_over_a_second_provider(self) -> None:
        result = _observation_rules(
            agent=({"browser.screenshot"}, set()),
            loop=(set(), set()),
            env_provides={"browser.screenshot", "desktop.screenshot"},
        )
        assert result.active_fragments == frozenset({"browser.screenshot"})


# ── 5. Downstream stays fully qualified ───────────────────────────────


def _desktop_env() -> EnvironmentBase:
    return make_env(EnvironmentBase, {"desktop#0": MockDesktopCapability()})


class TestRoutingStillSpeaksFullyQualifiedNames:
    async def test_guard_rewrites_a_relative_name_before_the_router(self) -> None:
        env = _desktop_env()
        agent = _RelativeAgent()
        negotiated = negotiate_action(agent, env)
        descriptors = env.get_action_space(
            active_actions=negotiated.active_actions
        )
        _kept, guard = build_guarded_act(
            env.act, descriptors, bindings=negotiated.action_bindings
        )

        result = await guard(
            Action(
                name="click_coordinate",
                params={"coordinate_x": 3, "coordinate_y": 4},
            )
        )

        assert result.success
        assert result.extracted_content == "desktop click"
        cap = env.capabilities["desktop#0"]
        assert isinstance(cap, MockDesktopCapability)
        assert cap.click.executed == [{"coordinate_x": 3, "coordinate_y": 4}]

    async def test_the_environment_itself_still_rejects_a_relative_name(
        self,
    ) -> None:
        """Proof the prefix routing the guard feeds is untouched."""
        env = _desktop_env()
        result = await env.act(Action(name="click_coordinate", params={}))
        assert not result.success
        assert "fully qualified" in (result.error or "")

    async def test_descriptors_advertise_the_fully_qualified_name(self) -> None:
        env = _desktop_env()
        negotiated = negotiate_action(_RelativeAgent(), env)
        names = {
            d.name
            for d in env.get_action_space(active_actions=negotiated.active_actions)
        }
        assert names == {
            "desktop.click_coordinate",
            "desktop.done",
            "desktop.scroll",
        }

    async def test_a_relative_name_outside_the_active_set_is_still_rejected(
        self,
    ) -> None:
        env = _desktop_env()
        negotiated = negotiate_action(_RelativeAgent(), env)
        descriptors = env.get_action_space(
            active_actions=negotiated.active_actions
        )
        _kept, guard = build_guarded_act(
            env.act, descriptors, bindings=negotiated.action_bindings
        )

        result = await guard(Action(name="type", params={"text": "hi"}))

        assert not result.success
        assert "not in the active action set" in (result.error or "")

    async def test_an_ablation_revokes_the_relative_alias_too(self) -> None:
        """A binding whose target the filter dropped must not resolve."""
        env = _desktop_env()
        negotiated = negotiate_action(_RelativeAgent(), env)
        descriptors = env.get_action_space(
            active_actions=negotiated.active_actions
        )
        _kept, guard = build_guarded_act(
            env.act,
            descriptors,
            ["desktop.done"],
            bindings=negotiated.action_bindings,
        )

        result = await guard(Action(name="click_coordinate", params={}))

        assert not result.success
        assert "not in the active action set" in (result.error or "")


# ── 6. The acceptance case: a real agent, two capabilities ────────────


class _BrowserActionsAndFragments:
    """The browser half of the acceptance pair, as key sets."""

    provided_actions = frozenset(
        {
            "browser.click_coordinate",
            "browser.type_coordinate",
            "browser.done",
            "browser.scroll",
            "browser.send_keys",
            "browser.drag",
            "browser.wait",
        }
    )
    provided_fragments = frozenset({"browser.screenshot", "browser.dom"})


class _DesktopActionsAndFragments:
    provided_actions = MockDesktopCapability().provided_actions
    provided_fragments = frozenset({"desktop.screenshot"})


class TestUITarsRunsWhereverItsVerbsExist:
    """UI-TARS is a computer-use model. Its wiring must not say ``browser``.

    Before F10 it declared ``browser.click_coordinate`` /
    ``browser.screenshot``, so the model whose own config offers
    COMPUTER_USE / MOBILE_USE / GROUNDING could only ever negotiate
    against a browser. These four assertions are the whole point of the
    change: one ``__wiring__``, both axes, two capabilities.
    """

    def test_the_wiring_names_no_capability_at_all(self) -> None:
        for entry in UITarsAgent.__wiring__.values():
            if isinstance(entry, str):
                continue
            for key_list in entry[1:]:
                assert all("." not in key for key in key_list), key_list

    def test_actions_negotiate_against_browser(self) -> None:
        result = negotiate_action(UITarsAgent(), _BrowserActionsAndFragments())
        assert result.active_actions == _BrowserActionsAndFragments.provided_actions
        assert all(a.startswith("browser.") for a in result.active_actions)

    def test_actions_negotiate_against_desktop_unchanged(self) -> None:
        result = negotiate_action(UITarsAgent(), _DesktopActionsAndFragments())
        # ``desktop.type`` exists too and is *not* picked: the agent asked
        # for ``type_coordinate``, and a short name binds on the whole
        # trailing segment.
        assert result.active_actions == frozenset(
            {
                "desktop.click_coordinate",
                "desktop.type_coordinate",
                "desktop.done",
                "desktop.scroll",
            }
        )
        assert all(a.startswith("desktop.") for a in result.active_actions)

    def test_observation_negotiates_against_both(self) -> None:
        for env, expected in (
            (_BrowserActionsAndFragments(), "browser.screenshot"),
            (_DesktopActionsAndFragments(), "desktop.screenshot"),
        ):
            result = negotiate_observation(UITarsAgent(), ReactLoop(), env)
            assert result.env_sourced == frozenset({expected})
            assert result.fragment_bindings["screenshot"] == expected
            # The loop-source half resolves the same relative way.
            assert "loop.task" in result.loop_sourced

    def test_a_composite_holding_both_refuses_and_names_the_candidates(
        self,
    ) -> None:
        composite = _ProvidesActions(
            *_BrowserActionsAndFragments.provided_actions,
            *_DesktopActionsAndFragments.provided_actions,
        )
        with pytest.raises(AmbiguousActionError) as exc:
            negotiate_action(UITarsAgent(), composite)
        assert exc.value.declared in {"click_coordinate", "done", "type_coordinate"}
        assert len(exc.value.candidates) == 2
        message = str(exc.value)
        assert all(candidate in message for candidate in exc.value.candidates)

    def test_the_observation_axis_refuses_the_same_way(self) -> None:
        class _Composite:
            provided_fragments = frozenset(
                {"browser.screenshot", "desktop.screenshot"}
            )

        with pytest.raises(AmbiguousFragmentError) as exc:
            negotiate_observation(UITarsAgent(), ReactLoop(), _Composite())
        assert exc.value.candidates == (
            "browser.screenshot",
            "desktop.screenshot",
        )

    def test_the_escape_hatch_pins_the_choice(self) -> None:
        """Writing the full key is exactly what the ambiguity error asks for."""

        class _PinnedUITars(UITarsAgent):
            __wiring__: ClassVar[dict[str, Any]] = {
                **UITarsAgent.__wiring__,
                "action": (
                    "action",
                    [
                        "desktop.click_coordinate",
                        "desktop.type_coordinate",
                        "desktop.done",
                    ],
                    [],
                ),
                "observation": (
                    "observation",
                    ["desktop.screenshot", "task"],
                    [],
                ),
            }

        composite_actions = _ProvidesActions(
            *_BrowserActionsAndFragments.provided_actions,
            *_DesktopActionsAndFragments.provided_actions,
        )
        result = negotiate_action(_PinnedUITars(), composite_actions)
        assert result.active_actions == frozenset(
            {
                "desktop.click_coordinate",
                "desktop.type_coordinate",
                "desktop.done",
            }
        )

        class _CompositeFragments:
            provided_fragments = frozenset(
                {"browser.screenshot", "desktop.screenshot"}
            )

        observed = negotiate_observation(
            _PinnedUITars(), ReactLoop(), _CompositeFragments()
        )
        assert observed.env_sourced == frozenset({"desktop.screenshot"})
