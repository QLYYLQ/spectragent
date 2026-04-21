"""Tests for Environment prefix routing — single-cap and multi-cap scenarios."""

import asyncio

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_env


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
        self.env = make_env(EnvironmentBase, {"alpha": CapA()})

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
        self.env = make_env(EnvironmentBase, {"alpha": CapA(), "beta": CapB()})

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


class TestActiveActionsNarrowing:
    """``get_action_space(active_actions=...)`` — unprefixed filter.

    The environment owns its prefix convention, so narrowing must
    happen on the unprefixed name *before* the prefix is applied.
    This test pins down both single-cap and composite paths.
    """

    def test_single_cap_narrowing_unprefixed(self) -> None:
        env = make_env(EnvironmentBase, {"alpha": CapA()})
        descs = env.get_action_space(active_actions=frozenset({"click"}))
        assert [d.name for d in descs] == ["click"]

    def test_composite_narrowing_preserves_prefix(self) -> None:
        """Composite narrowing must keep prefixed names after filtering."""
        env = make_env(EnvironmentBase, {"alpha": CapA(), "beta": CapB()})
        descs = env.get_action_space(
            active_actions=frozenset({"click", "run"}),
        )
        names = {d.name for d in descs}
        # ``type`` is CapA-provided but NOT in the active set → dropped.
        assert names == {"alpha.click", "beta.run"}

    def test_composite_narrowing_drops_cap_with_no_active_actions(
        self,
    ) -> None:
        """A cap whose actions are all filtered out disappears entirely."""
        env = make_env(EnvironmentBase, {"alpha": CapA(), "beta": CapB()})
        descs = env.get_action_space(active_actions=frozenset({"click"}))
        assert [d.name for d in descs] == ["alpha.click"]

    def test_composite_narrowing_empty_set_yields_empty(self) -> None:
        env = make_env(EnvironmentBase, {"alpha": CapA(), "beta": CapB()})
        descs = env.get_action_space(active_actions=frozenset())
        assert descs == []

    def test_none_means_no_narrowing(self) -> None:
        """``active_actions=None`` is the backward-compatible default."""
        env = make_env(EnvironmentBase, {"alpha": CapA(), "beta": CapB()})
        assert len(env.get_action_space(active_actions=None)) == len(
            env.get_action_space()
        )

    def test_two_caps_providing_same_action_both_kept(self) -> None:
        """If two caps provide ``click`` and active_actions={click},
        both prefixed variants survive — narrowing is by unprefixed
        name, not by first-match."""

        class CapClickTwin(CapabilityBase):
            __registry_key__ = "prefix_cap_twin"

            def get_action_descriptors(self, **ctx):
                return [ActionDescriptor(name="click", description="Click twin")]

            async def execute_action(self, action, **kw):
                return ActionResult(success=True, extracted_content="twin:click")

        env = make_env(
            EnvironmentBase, {"alpha": CapA(), "twin": CapClickTwin()},
        )
        descs = env.get_action_space(active_actions=frozenset({"click"}))
        names = {d.name for d in descs}
        assert names == {"alpha.click", "twin.click"}
