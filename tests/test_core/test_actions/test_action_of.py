"""``action_union.action_of``: one validated member of the closed union, as the Action it names.

The closed union gives every optional param a null branch, because a strict
provider dialect makes every property required and ``null`` is then the only
way to decline one. That null must reach the capability as an omission, so its
own knob decides. A *required* param's null is different: it is the value the
action's ``Params`` asks for, and dropping it turns a correct reply into a
missing-param refusal.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from spectragent.core.action_guard import params_refusal
from spectragent.core.actions.action_union import action_of, action_union
from spectragent.schemas.action import Action
from tests._helpers.action_spaces import browser_space


def _member(space_names: tuple[str, ...], item: dict[str, object]):
    space = browser_space(*space_names)
    union, _models = action_union(space.descriptors)
    return space, TypeAdapter(union).validate_python(item)


def test_a_declined_optional_param_is_left_out() -> None:
    space, member = _member(
        ("type",), {"browser.type": {"index": 3, "text": "hello", "clear": None}}
    )
    action = action_of(member)
    assert action == Action(name="browser.type", params={"index": 3, "text": "hello"})
    assert params_refusal(space, action) is None


def test_a_required_null_is_kept_and_passes_the_params_check() -> None:
    """``done``'s ``extracted_content`` is required and nullable: null is the answer."""
    space, member = _member(
        ("done",), {"browser.done": {"extracted_content": None, "success": None}}
    )
    action = action_of(member)
    assert action == Action(name="browser.done", params={"extracted_content": None})
    assert params_refusal(space, action) is None


def test_written_values_are_kept_as_written() -> None:
    _space, member = _member(
        ("scroll",), {"browser.scroll": {"direction": "up", "amount": 600, "index": None}}
    )
    assert action_of(member) == Action(
        name="browser.scroll", params={"direction": "up", "amount": 600}
    )
