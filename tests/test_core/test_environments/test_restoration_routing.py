"""Checkpoint restoration through the environment: routed like ``act``, read-only.

``docs/architecture_design.md`` §4.3 "检查点恢复的路由" and §4.9
``RestorationSupport``: ``retarget`` goes to the capability owning the
action's prefix when it implements the protocol and is the identity
otherwise; ``verify_restoration`` fans out, in capability order, to every
capability implementing it, and is empty when none does.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import RestorationAlignment, RestorationSupport
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.restoration import RestorationEvidence, Retarget
from tests._helpers.assembly import make_env


class _Plain(CapabilityBase):
    """A capability that knows nothing about restoration."""

    __registry_key__ = "restore_route_plain"

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="restore_route_plain.run", description="Run")]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("retarget and verify must never execute an action")


class _Restoring(CapabilityBase):
    """Remaps ``index`` by an offset and reports a fixed measurement.

    No ``__init__`` of its own (it would hide the inherited ``id`` knob from
    conscribe): build it with :func:`_restoring`.
    """

    __registry_key__ = "restore_route_cap"

    offset: int
    url_match: bool
    retargets: list[tuple[Action, Any, Any]]
    verifies: list[tuple[Any, Any]]

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="restore_route_cap.click", description="Click")]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("retarget and verify must never execute an action")

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        self.retargets.append((action, recorded, restored))
        params = dict(action.params)
        params["index"] = params.get("index", 0) + self.offset
        return Retarget(
            action=Action(name=action.name, params=params),
            method="geometry_signature",
            detail="offset",
        )

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        self.verifies.append((recorded, restored))
        return RestorationEvidence(capability_id=self.capability_id, url_match=self.url_match)


class _Second(_Restoring):
    __registry_key__ = "restore_route_second"


def _restoring(cls: type[_Restoring] = _Restoring, *, url_match: bool = True) -> _Restoring:
    cap = cls()
    cap.offset = 100
    cap.url_match = url_match
    cap.retargets = []
    cap.verifies = []
    return cap


def test_the_protocol_is_structural() -> None:
    assert isinstance(_restoring(), RestorationSupport)
    assert not isinstance(_Plain(), RestorationSupport)


class TestRetarget:
    async def test_routed_to_the_implementing_capability(self) -> None:
        cap = _restoring()
        env = make_env(EnvironmentBase, {"restore_route_cap": cap, "plain": _Plain()})
        action = Action(name="restore_route_cap.click", params={"index": 7})
        recorded, restored = object(), object()
        retarget = await env.retarget(action, recorded, restored)
        assert retarget.method == "geometry_signature"
        assert retarget.action == Action(name="restore_route_cap.click", params={"index": 107})
        assert cap.retargets == [(action, recorded, restored)]

    async def test_a_capability_without_the_protocol_gets_the_identity(self) -> None:
        env = make_env(EnvironmentBase, {"plain": _Plain(), "restore_route_cap": _restoring()})
        action = Action(name="restore_route_plain.run", params={"x": [1, 2]})
        retarget = await env.retarget(action, None, None)
        assert retarget.method == "identity"
        assert retarget.failure is None
        assert retarget.action == action
        # A copy, deep: mutating it cannot rewrite the recorded action.
        assert retarget.action is not action
        assert retarget.action is not None
        assert retarget.action.params["x"] is not action.params["x"]

    async def test_a_bare_name_gets_the_identity(self) -> None:
        cap = _restoring()
        env = make_env(EnvironmentBase, {"restore_route_cap": cap})
        retarget = await env.retarget(Action(name="click", params={"index": 1}), None, None)
        assert retarget.method == "identity"
        assert retarget.action == Action(name="click", params={"index": 1})
        assert cap.retargets == []

    async def test_an_unknown_prefix_gets_the_identity(self) -> None:
        cap = _restoring()
        env = make_env(EnvironmentBase, {"restore_route_cap": cap})
        action = Action(name="nowhere.click", params={"index": 1})
        retarget = await env.retarget(action, None, None)
        assert retarget.method == "identity"
        assert retarget.action == action
        assert cap.retargets == []


class TestVerifyRestoration:
    async def test_fans_out_in_capability_order(self) -> None:
        first, second = _restoring(_Second, url_match=False), _restoring()
        env = make_env(
            EnvironmentBase,
            {"second": first, "plain": _Plain(), "restore_route_cap": second},
        )
        recorded, restored = object(), object()
        evidence = await env.verify_restoration(recorded, restored)
        assert [(e.capability_id, e.url_match) for e in evidence] == [
            ("restore_route_second", False),
            ("restore_route_cap", True),
        ]
        assert first.verifies == [(recorded, restored)]
        assert second.verifies == [(recorded, restored)]

    async def test_nothing_to_check_without_an_implementer(self) -> None:
        env = make_env(EnvironmentBase, {"plain": _Plain()})
        assert await env.verify_restoration(object(), object()) == []


class _Aligning(CapabilityBase):
    """Keeps every alignment request it is handed; restores nothing else."""

    __registry_key__ = "restore_route_aligning"

    requests: list[tuple[Any, float]]

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("an alignment request must never execute an action")

    async def align_next_observation(self, recorded: Any, *, media_seek_timeout_s: float) -> None:
        self.requests.append((recorded, media_seek_timeout_s))


class _SecondAligning(_Aligning):
    __registry_key__ = "restore_route_aligning_second"


class TestAlignNextObservation:
    """``RestorationAlignment``: a separate optional face, fanned out in capability order."""

    def test_it_is_its_own_protocol(self) -> None:
        aligning = _Aligning()
        assert isinstance(aligning, RestorationAlignment)
        assert not isinstance(aligning, RestorationSupport)
        assert not isinstance(_restoring(), RestorationAlignment)

    async def test_only_implementers_are_asked(self) -> None:
        first, second = _Aligning(), _SecondAligning()
        first.requests, second.requests = [], []
        env = make_env(
            EnvironmentBase,
            {"a": first, "plain": _Plain(), "restore_route_cap": _restoring(), "b": second},
        )
        recorded = object()
        await env.align_next_observation(recorded, media_seek_timeout_s=1.5)
        assert first.requests == [(recorded, 1.5)]
        assert second.requests == [(recorded, 1.5)]

    async def test_nothing_happens_without_an_implementer(self) -> None:
        env = make_env(EnvironmentBase, {"plain": _Plain()})
        await env.align_next_observation(object(), media_seek_timeout_s=2.0)
