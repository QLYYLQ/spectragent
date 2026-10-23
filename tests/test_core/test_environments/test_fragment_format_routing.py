"""Fragment format descriptions, through the environment.

``docs/architecture_design.md`` §4.4 "观测格式由提供方描述": each capability
implementing ``FragmentFormats`` describes its own fragments; the environment
unions them in capability order, a capability without the protocol describes
nothing, and a description of another capability's fragment is refused.
Read-only: nothing is set up.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import FragmentFormats
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_env


class _Plain(CapabilityBase):
    """A capability that describes no fragment."""

    __abstract__ = True

    def __init__(self, capability_id: str = "plain") -> None:
        super().__init__()
        self._capability_id = capability_id

    @property
    def capability_id(self) -> str:
        return self._capability_id

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        raise AssertionError("describing a format never executes an action")

    async def setup(self, *, negotiation: Any = None) -> None:
        raise AssertionError("describing a format never sets a capability up")


class _Describing(_Plain):
    """Describes whatever it is handed, keyed as given."""

    __abstract__ = True

    def __init__(self, capability_id: str, formats: dict[str, str]) -> None:
        super().__init__(capability_id)
        self._formats = formats

    def fragment_formats(self) -> dict[str, str]:
        return dict(self._formats)


def test_the_protocol_is_structural() -> None:
    assert isinstance(_Describing("site", {}), FragmentFormats)
    assert not isinstance(_Plain(), FragmentFormats)


def test_descriptions_are_unioned_and_a_plain_capability_describes_nothing() -> None:
    env = make_env(
        EnvironmentBase,
        {
            "site#0": _Describing("site", {"site.dom": "lines with [N]"}),
            "plain#0": _Plain(),
            "shell#0": _Describing("shell", {"shell.terminal": "stdout, then stderr"}),
        },
    )
    assert env.fragment_formats() == {
        "site.dom": "lines with [N]",
        "shell.terminal": "stdout, then stderr",
    }


def test_an_environment_without_describing_capabilities_describes_nothing() -> None:
    assert make_env(EnvironmentBase, {"plain#0": _Plain()}).fragment_formats() == {}


@pytest.mark.parametrize("key", ["shell.terminal", "dom"])
def test_a_capability_speaks_only_for_its_own_fragments(key: str) -> None:
    env = make_env(EnvironmentBase, {"site#0": _Describing("site", {key: "not mine"})})
    with pytest.raises(ValueError, match="not one of its fragments"):
        env.fragment_formats()
