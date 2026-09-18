"""Tests for Environment prefix routing.

Under the nested-registry design, action names are always fully
qualified (``source.action_id``) — capabilities emit descriptors with
the prefix already attached, and the environment routes on the leading
segment without any runtime re-prefixing.
"""

import asyncio
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_env


class CapA(CapabilityBase):
    __registry_key__ = "prefix_cap_a"

    def get_action_descriptors(self, **ctx):
        return [
            ActionDescriptor(name="prefix_cap_a.click", description="Click"),
            ActionDescriptor(name="prefix_cap_a.type", description="Type"),
        ]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, extracted_content=f"a:{action.name}")


class CapB(CapabilityBase):
    __registry_key__ = "prefix_cap_b"

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="prefix_cap_b.run", description="Run")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, extracted_content=f"b:{action.name}")


class TestSingleCapability:
    def setup_method(self):
        self.env = make_env(EnvironmentBase, {"prefix_cap_a": CapA()})

    def test_action_space_keeps_registry_prefix(self) -> None:
        descs = self.env.get_action_space()
        names = [d.name for d in descs]
        assert "prefix_cap_a.click" in names
        assert all("." in n for n in names)

    def test_act_routes_by_prefix(self) -> None:
        r = asyncio.run(self.env.act(Action(name="prefix_cap_a.click")))
        assert r.success
        assert r.extracted_content == "a:prefix_cap_a.click"

    def test_act_missing_prefix_error(self) -> None:
        r = asyncio.run(self.env.act(Action(name="click")))
        assert not r.success

    def test_capabilities_set(self) -> None:
        assert self.env.capability_ids() == frozenset({"prefix_cap_a"})


class TestMultiCapability:
    def setup_method(self):
        self.env = make_env(
            EnvironmentBase,
            {"prefix_cap_a": CapA(), "prefix_cap_b": CapB()},
        )

    def test_action_space_prefixed(self) -> None:
        names = [d.name for d in self.env.get_action_space()]
        assert "prefix_cap_a.click" in names
        assert "prefix_cap_b.run" in names

    def test_act_routes_by_prefix(self) -> None:
        r = asyncio.run(self.env.act(Action(name="prefix_cap_a.click")))
        assert r.success and r.extracted_content == "a:prefix_cap_a.click"

        r2 = asyncio.run(self.env.act(Action(name="prefix_cap_b.run")))
        assert r2.success and r2.extracted_content == "b:prefix_cap_b.run"

    def test_act_unknown_prefix_error(self) -> None:
        r = asyncio.run(self.env.act(Action(name="unknown.click")))
        assert not r.success

    def test_act_missing_prefix_error(self) -> None:
        r = asyncio.run(self.env.act(Action(name="click")))
        assert not r.success


class TestActiveActionsNarrowing:
    """``get_action_space(active_actions=...)`` filters by fully-qualified name."""

    def test_single_cap_narrowing(self) -> None:
        env = make_env(EnvironmentBase, {"prefix_cap_a": CapA()})
        descs = env.get_action_space(
            active_actions=frozenset({"prefix_cap_a.click"}),
        )
        assert [d.name for d in descs] == ["prefix_cap_a.click"]

    def test_composite_narrowing_preserves_prefix(self) -> None:
        env = make_env(
            EnvironmentBase,
            {"prefix_cap_a": CapA(), "prefix_cap_b": CapB()},
        )
        descs = env.get_action_space(
            active_actions=frozenset(
                {"prefix_cap_a.click", "prefix_cap_b.run"},
            ),
        )
        names = {d.name for d in descs}
        # ``prefix_cap_a.type`` is CapA-provided but NOT in the active
        # set → dropped.
        assert names == {"prefix_cap_a.click", "prefix_cap_b.run"}

    def test_composite_narrowing_drops_cap_with_no_active_actions(
        self,
    ) -> None:
        env = make_env(
            EnvironmentBase,
            {"prefix_cap_a": CapA(), "prefix_cap_b": CapB()},
        )
        descs = env.get_action_space(
            active_actions=frozenset({"prefix_cap_a.click"}),
        )
        assert [d.name for d in descs] == ["prefix_cap_a.click"]

    def test_composite_narrowing_empty_set_yields_empty(self) -> None:
        env = make_env(
            EnvironmentBase,
            {"prefix_cap_a": CapA(), "prefix_cap_b": CapB()},
        )
        descs = env.get_action_space(active_actions=frozenset())
        assert descs == []

    def test_none_means_no_narrowing(self) -> None:
        env = make_env(
            EnvironmentBase,
            {"prefix_cap_a": CapA(), "prefix_cap_b": CapB()},
        )
        assert len(env.get_action_space(active_actions=None)) == len(env.get_action_space())


class TestDuplicateCapabilityInstances:
    """Two instances of one capability class cannot be routed apart.

    ``capabilities`` is keyed by capability *instance* id, but every
    routing key the environment exposes — action prefixes
    (``prefix_cap_a.click``) and observation fragment keys — is scoped to
    the capability *class*. The reverse index used to be a last-wins
    comprehension, so a second instance composed and started (in
    production: a whole second Chrome) and was then unreachable for every
    action and every fragment, silently, for the entire run.
    """

    def test_colliding_capability_ids_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            make_env(EnvironmentBase, {"a#0": CapA(), "a#1": CapA()})

        message = str(exc_info.value)
        assert "prefix_cap_a" in message
        assert "a#0" in message and "a#1" in message

    def test_distinct_classes_still_compose(self) -> None:
        env = make_env(EnvironmentBase, {"a#0": CapA(), "b#0": CapB()})
        assert env.capability_ids() == frozenset({"prefix_cap_a", "prefix_cap_b"})

    def test_collision_is_caught_before_any_capability_is_set_up(self) -> None:
        """The deferred-injection path: capabilities arrive by setattr.

        ``ConfigComposer`` setattrs ``capabilities`` when the YAML omits
        the block, which bypasses ``__init__`` entirely. ``setup()``
        re-checks before it starts anything, so no capability is acquired
        and then stranded.
        """
        started: list[str] = []

        class _RecordingCap(CapA):
            __registry_key__ = "prefix_dup_recording"

            async def setup(self, *, negotiation=None):
                started.append(self.capability_id)

        caps: dict[str, Any] = {"x#0": _RecordingCap(), "x#1": _RecordingCap()}
        env = EnvironmentBase()
        env.capabilities = caps

        with pytest.raises(ValueError, match="prefix_dup_recording"):
            asyncio.run(env.setup())
        assert started == []
