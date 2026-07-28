"""Tests for ActionRegistrar and ActionBase auto-registration.

These tests ensure the action registry is symmetric to the observation
registry: every concrete action class is auto-registered via conscribe
metaclass machinery, can produce a descriptor, and can be instantiated
with its capability-side config fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Import to trigger registration of all browser actions.
import spectragent.core.capabilities.browser.actions as _browser_actions  # noqa: F401
from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.actions.base import BaseAction
from spectragent.schemas.action import ActionDescriptor, ActionResult

_EXPECTED_BROWSER_ACTIONS = {
    "browser.click",
    "browser.click_coordinate",
    "browser.type",
    "browser.navigate",
    "browser.scroll",
    "browser.send_keys",
    "browser.go_back",
    "browser.go_forward",
    "browser.switch_tab",
    "browser.select_dropdown",
    "browser.extract_content",
    "browser.done",
}


class TestRegistryMembership:
    """All expected browser actions are registered and lookup-able."""

    def test_all_browser_actions_registered(self) -> None:
        keys = set(ActionRegistrar.keys())
        assert keys >= _EXPECTED_BROWSER_ACTIONS

    def test_each_action_class_satisfies_base_action_protocol(self) -> None:
        for name in _EXPECTED_BROWSER_ACTIONS:
            cls = ActionRegistrar.get(name)
            assert cls is not None, f"{name} not in registry"
            instance = cls()
            assert isinstance(instance, BaseAction), (
                f"{cls.__name__} does not satisfy BaseAction Protocol"
            )

    def test_action_id_matches_registry_key(self) -> None:
        for name in _EXPECTED_BROWSER_ACTIONS:
            cls = ActionRegistrar.get(name)
            # action_id returns the fully-qualified registry key.
            assert cls().action_id == name
            assert cls().source == name.split(".", 1)[0]


class TestDescriptorContract:
    """Every registered action returns a valid ActionDescriptor."""

    def test_descriptor_is_action_descriptor(self) -> None:
        for name in _EXPECTED_BROWSER_ACTIONS:
            cls = ActionRegistrar.get(name)
            desc = cls().descriptor()
            assert isinstance(desc, ActionDescriptor)
            assert desc.name == name


class TestDataclassFieldInjection:
    """Subclasses can declare Annotated capability-side config fields."""

    def test_scroll_default_amount_is_configurable(self) -> None:
        from spectragent.core.capabilities.browser.actions.scroll import (
            ScrollAction,
        )

        default = ScrollAction()
        custom = ScrollAction(default_amount=600, max_amount=5000)
        assert default.default_amount == 300
        assert custom.default_amount == 600
        assert custom.max_amount == 5000

    def test_navigate_wait_until_configurable(self) -> None:
        from spectragent.core.capabilities.browser.actions.navigate import (
            NavigateAction,
        )

        default = NavigateAction()
        custom = NavigateAction(wait_until="networkidle", timeout_ms=10000)
        assert default.wait_until == "load"
        assert custom.wait_until == "networkidle"
        assert custom.timeout_ms == 10000


class TestAdHocRegistration:
    """New action subclasses register themselves via the metaclass."""

    def test_new_subclass_is_registered(self) -> None:
        @dataclass
        class TestOnlyAction(ActionBase):
            __registry_key__ = "_test.only_action_registrar"

            def descriptor(self, **ctx: Any) -> ActionDescriptor:
                return ActionDescriptor(
                    name="_test.only_action_registrar",
                    description="unit test only",
                    params={},
                )

            async def execute(self, params, *, capability) -> ActionResult:
                return ActionResult(success=True)

        cls = ActionRegistrar.get("_test.only_action_registrar")
        assert cls is TestOnlyAction
        # Clean up to avoid polluting the shared registry for other tests
        ActionRegistrar.unregister("_test.only_action_registrar")
