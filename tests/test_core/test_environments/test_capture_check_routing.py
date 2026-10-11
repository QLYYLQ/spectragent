"""A recorded capture against this configuration, through the environment (M32).

``docs/architecture_design.md`` §4.3 "录下的采集与本配置是否一致" and §4.9
``CaptureCheck``: each recorded key goes to the capability its prefix names;
capabilities implementing the protocol compare their own group, in capability
order, and the results are concatenated; a capability without the protocol or
a key no capability owns contributes nothing. Read-only: nothing is set up.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import CaptureCheck, CaptureComparison
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_env


class _Plain(CapabilityBase):
    """A capability that cannot compare captures."""

    __registry_key__ = "capture_route_plain"

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("a capture comparison never executes an action")

    async def setup(self, *, negotiation: Any = None) -> None:
        raise AssertionError("a capture comparison never sets a capability up")


class _Checking(_Plain):
    """Reports one mismatch per key it is handed, and remembers what it saw."""

    __registry_key__ = "capture_route_cap"

    seen: list[dict[str, Any]]

    def compare_capture(self, recorded: Mapping[str, Any]) -> CaptureComparison:
        self.seen.append(dict(recorded))
        return CaptureComparison(
            mismatches=tuple(f"{self.capability_id}: {key}" for key in sorted(recorded)),
            unchecked=(f"{self.capability_id}: dpr",),
        )


class _Second(_Checking):
    __registry_key__ = "capture_route_second"


def _checking(cls: type[_Checking] = _Checking) -> _Checking:
    cap = cls()
    cap.seen = []
    return cap


def test_the_protocol_is_structural() -> None:
    assert isinstance(_checking(), CaptureCheck)
    assert not isinstance(_Plain(), CaptureCheck)


def test_each_capability_compares_only_its_own_keys_in_capability_order() -> None:
    first, second = _checking(_Second), _checking()
    env = make_env(
        EnvironmentBase,
        {"b": first, "plain": _Plain(), "a": second},
    )
    recorded = {
        "capture_route_cap.screenshot": "s",
        "capture_route_second.dom": "d",
        "capture_route_cap.meta": "m",
        "capture_route_plain.thing": "t",
        "nowhere.screenshot": "x",
        "bare": "y",
    }
    comparison = env.compare_capture(recorded)
    assert comparison.mismatches == (
        "capture_route_second: capture_route_second.dom",
        "capture_route_cap: capture_route_cap.meta",
        "capture_route_cap: capture_route_cap.screenshot",
    )
    assert comparison.unchecked == ("capture_route_second: dpr", "capture_route_cap: dpr")
    assert first.seen == [{"capture_route_second.dom": "d"}]
    assert second.seen == [
        {"capture_route_cap.screenshot": "s", "capture_route_cap.meta": "m"},
    ]


def test_a_capability_with_nothing_recorded_is_not_asked() -> None:
    cap = _checking()
    env = make_env(EnvironmentBase, {"cap": cap})
    assert env.compare_capture({"nowhere.x": 1}) == CaptureComparison()
    assert cap.seen == []


def test_nothing_to_compare_without_an_implementer() -> None:
    env = make_env(EnvironmentBase, {"plain": _Plain()})
    assert env.compare_capture({"capture_route_plain.thing": 1}) == CaptureComparison()
