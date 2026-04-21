"""Unit tests for BrowserCapability — static contract + lifecycle.

These tests do not require a real CDP connection. Tests that exercise
the cache and lazy-batch resolution live in ``test_browser_lazy_batch.py``.
"""

from __future__ import annotations

from spectragent.core.capabilities.base import BaseCapability
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.schemas.action import ActionDescriptor


class TestStaticContract:
    def test_satisfies_base_capability_protocol(self) -> None:
        cap = BrowserCapability()
        assert isinstance(cap, BaseCapability)

    def test_capability_id(self) -> None:
        cap = BrowserCapability()
        assert cap.capability_id == "browser"

    def test_provided_fragments(self) -> None:
        cap = BrowserCapability()
        assert cap.provided_fragments == frozenset(
            {"dom", "screenshot", "browser_meta"}
        )

    def test_wiring_observation(self) -> None:
        # Wiring declares the three fragments under the "observation" registry
        wiring = BrowserCapability.__wiring__
        assert "observation" in wiring
        registry, frags = wiring["observation"]
        assert registry == "observation"
        assert set(frags) == {"dom", "screenshot", "browser_meta"}

    def test_wiring_backend(self) -> None:
        # Wiring declares browser_provider as the backend registry
        wiring = BrowserCapability.__wiring__
        assert wiring.get("backend") == "browser_provider"


class TestActionDescriptors:
    def test_returns_list_of_action_descriptors(self) -> None:
        cap = BrowserCapability()
        descriptors = cap.get_action_descriptors()
        assert isinstance(descriptors, list)
        assert all(isinstance(d, ActionDescriptor) for d in descriptors)
        assert len(descriptors) > 0

    def test_includes_core_actions(self) -> None:
        cap = BrowserCapability()
        names = {d.name for d in cap.get_action_descriptors()}
        # The agent must have at least these actions
        assert {"click", "type", "scroll", "navigate", "done"} <= names

    def test_done_action_has_extracted_content_param(self) -> None:
        cap = BrowserCapability()
        done = next(
            d for d in cap.get_action_descriptors() if d.name == "done"
        )
        assert "extracted_content" in done.params

    def test_descriptors_are_a_copy(self) -> None:
        # Mutating the returned list must not affect future calls.
        cap = BrowserCapability()
        first = cap.get_action_descriptors()
        first.clear()
        second = cap.get_action_descriptors()
        assert len(second) > 0


class TestPropertiesBeforeSetup:
    def test_is_alive_false_before_setup(self) -> None:
        cap = BrowserCapability()
        assert cap.is_alive is False

    def test_default_session_id_raises_before_setup(self) -> None:
        cap = BrowserCapability()
        try:
            _ = cap.default_session_id
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError before setup()")

    def test_cdp_property_raises_before_setup(self) -> None:
        cap = BrowserCapability()
        try:
            _ = cap.cdp
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError before setup()")


class TestInitialization:
    def test_default_config(self) -> None:
        cap = BrowserCapability()
        assert cap.profile == "standard"
        assert cap.viewport_width == 1280
        assert cap.viewport_height == 720
        assert cap.cdp_url is None

    def test_custom_config(self) -> None:
        cap = BrowserCapability(
            cdp_url="ws://localhost:9222/devtools/browser/abc",
            profile="standard",
            viewport_width=1920,
            viewport_height=1080,
        )
        assert cap.cdp_url == "ws://localhost:9222/devtools/browser/abc"
        assert cap.viewport_width == 1920
        assert cap.viewport_height == 1080

    def test_internal_state_starts_empty(self) -> None:
        cap = BrowserCapability()
        assert cap._scopes == {}
        assert cap._selector_maps == {}
        assert cap._cycle_cache == {}
        assert cap._default_session_id is None


# ─── M13-Rev additions ────────────────────────────────────────────────


class TestActionWiringAndProvidedActions:
    """``__wiring__["action"]`` feeds the symmetric ``provided_actions``."""

    def test_provided_actions_covers_full_default_set(self) -> None:
        cap = BrowserCapability()
        assert cap.provided_actions == frozenset(
            {
                "click", "click_coordinate", "type", "navigate", "scroll",
                "send_keys", "go_back", "go_forward", "switch_tab",
                "select_dropdown", "extract_content", "done",
            }
        )

    def test_wiring_action_tuple_matches_provided(self) -> None:
        wiring = BrowserCapability.__wiring__
        registry, provides = wiring["action"]
        assert registry == "action"
        assert set(provides) == BrowserCapability().provided_actions


class TestDefaultActionInstantiation:
    """``__post_init__`` materialises actions from wiring + overrides."""

    def test_default_actions_instantiated_when_no_overrides(self) -> None:
        cap = BrowserCapability()
        assert len(cap._actions_by_name) == 12
        assert set(cap._actions_by_name) == cap.provided_actions

    def test_action_overrides_apply_kwargs(self) -> None:
        cap = BrowserCapability(
            action_overrides={"scroll": {"default_amount": 777, "max_amount": 9999}}
        )
        scroll = cap._actions_by_name["scroll"]
        assert scroll.default_amount == 777  # type: ignore[attr-defined]
        assert scroll.max_amount == 9999  # type: ignore[attr-defined]
        # All 12 actions still materialised — overrides are additive.
        assert len(cap._actions_by_name) == 12

    def test_unknown_override_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="not in __wiring__"):
            BrowserCapability(action_overrides={"rogue_action": {"x": 1}})

    def test_provided_actions_matches_dispatch_map(self) -> None:
        """Core invariant: provided_actions == _actions_by_name.keys()."""
        cap = BrowserCapability()
        assert cap.provided_actions == frozenset(cap._actions_by_name)

    def test_provided_actions_matches_with_overrides(self) -> None:
        cap = BrowserCapability(
            action_overrides={"navigate": {"timeout_ms": 5000}}
        )
        assert cap.provided_actions == frozenset(cap._actions_by_name)

    def test_get_action_descriptors_derives_from_dispatch_map(self) -> None:
        cap = BrowserCapability()
        names = {d.name for d in cap.get_action_descriptors()}
        assert names == set(cap._actions_by_name)


class TestExecuteActionDispatch:
    """``execute_action`` routes through the registered action instance."""

    def test_unknown_action_returns_error(self) -> None:
        cap = BrowserCapability()
        cap._default_session_id = "sid_x"
        cap._scopes["sid_x"] = object()  # type: ignore[assignment]
        cap._selector_maps["sid_x"] = {}

        import asyncio

        from spectragent.schemas.action import Action

        result = asyncio.run(
            cap.execute_action(
                Action(name="does_not_exist"), session_id="sid_x",
            )
        )
        assert result.success is False
        assert "Unknown action" in (result.error or "")

    def test_done_flows_through_capability(self) -> None:
        cap = BrowserCapability()
        cap._default_session_id = "sid_x"
        cap._scopes["sid_x"] = object()  # type: ignore[assignment]
        cap._selector_maps["sid_x"] = {}

        import asyncio

        from spectragent.schemas.action import Action

        result = asyncio.run(
            cap.execute_action(
                Action(name="done", params={"extracted_content": "answer42"}),
                session_id="sid_x",
            )
        )
        assert result.is_done is True
        assert result.extracted_content == "answer42"
