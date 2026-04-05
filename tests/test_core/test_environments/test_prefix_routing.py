"""Tests for Environment prefix routing — single-cap and multi-cap scenarios."""

import asyncio

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


class CapA(CapabilityBase):
    __registry_key__ = "prefix_cap_a"

    def get_action_descriptors(self, **ctx):
        return [
            ActionDescriptor(name="click", description="Click"),
            ActionDescriptor(name="type", description="Type"),
        ]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, extracted_content=f"a:{action.name}")


class CapB(CapabilityBase):
    __registry_key__ = "prefix_cap_b"

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="run", description="Run")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, extracted_content=f"b:{action.name}")


class TestSingleCapability:
    def setup_method(self):
        self.env = EnvironmentBase({"alpha": CapA()})

    def test_action_space_no_prefix(self) -> None:
        descs = self.env.get_action_space()
        names = [d.name for d in descs]
        assert "click" in names
        assert not any("." in n for n in names)

    def test_act_direct_delegation(self) -> None:
        r = asyncio.run(self.env.act(Action(name="click")))
        assert r.success
        assert r.extracted_content == "a:click"

    def test_capabilities_set(self) -> None:
        assert self.env.capabilities() == frozenset({"alpha"})


class TestMultiCapability:
    def setup_method(self):
        self.env = EnvironmentBase({"alpha": CapA(), "beta": CapB()})

    def test_action_space_prefixed(self) -> None:
        names = [d.name for d in self.env.get_action_space()]
        assert "alpha.click" in names
        assert "beta.run" in names

    def test_act_routes_by_prefix(self) -> None:
        r = asyncio.run(self.env.act(Action(name="alpha.click")))
        assert r.success and r.extracted_content == "a:click"

        r2 = asyncio.run(self.env.act(Action(name="beta.run")))
        assert r2.success and r2.extracted_content == "b:run"

    def test_act_unknown_prefix_error(self) -> None:
        r = asyncio.run(self.env.act(Action(name="unknown.click")))
        assert not r.success

    def test_act_missing_prefix_error(self) -> None:
        r = asyncio.run(self.env.act(Action(name="click")))
        assert not r.success
