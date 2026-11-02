"""Every registered action declares its parameters once, as a type.

``ActionDescriptor.params_schema`` existed as a field on the model and was
``None`` on every action in the tree; the machine-readable half of the
contract simply did not exist, so an agent that wanted a closed output
schema had nothing to compile. ``params`` prose was hand-written next to a
hand-read ``dict``, which is two descriptions of one contract with nothing
holding them together.

This file is the registry-wide scan of the replacement:

- every action has a ``Params`` model, flat and closed;
- the schema derived from it stays inside the subset all three provider
  dialects accept;
- the prose derived from it is **byte-identical** to what the tree shipped
  before, because two other agents (Skyvern, UI-TARS) print a descriptor's
  param text verbatim into their prompts;
- a param that needs an observation fragment says so, once, and both the
  action-level dependency and the descriptor-level drop follow from it.
"""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

import spectragent.core.capabilities.android.actions  # noqa: F401 — registers them
import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
import spectragent.core.loops.actions  # noqa: F401 — registers the Loop's own (loop.done)
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.action_union import params_model_from_schema
from spectragent.core.actions.params import (
    PointAxis,
    RequiresFragment,
    claim_params,
    element_params,
    param_fragments,
    point_params,
)
from tests._helpers.action_spaces import space_of

#: The shipped actions only. The registry is process-global and test helpers
#: register their own fakes into it on import (``tests/_helpers/archive_fakes.py``
#: registers ``contract_archive.*``), so the raw key set depends on which test
#: modules happened to be collected first — and this table is exact.
_KEYS = sorted(
    key
    for key, cls in ActionRegistrar.get_all().items()
    if cls.__module__.startswith("spectragent.")
)

#: An Android point param's prose: physical pixels of the screen.
_ANDROID_X = "int -- Physical px from the screen's left edge"
_ANDROID_Y = "int -- Physical px from the screen's top edge"

#: What every action's ``params`` prose rendered before this change, to the
#: byte. Not a restatement of the implementation: it is the *old*
#: hand-written text, so a renderer that formats "correctly" but differently
#: fails here rather than silently rewording two other agents' prompts.
_GOLDEN_PROSE: dict[str, dict[str, str]] = {
    "browser.click": {"index": "int -- element index from selector_map"},
    "browser.click_coordinate": {
        "coordinate_x": "int -- CSS px from the viewport's left edge",
        "coordinate_y": "int -- CSS px from the viewport's top edge",
    },
    "loop.done": {
        "extracted_content": "str | None -- final answer / result text",
        # Reworded on purpose: the done action's ``success`` is the agent's
        # claim about the task, and the bare type told no model what it meant.
        "success": "bool (optional) -- whether the task was accomplished; "
        "leave it unset if you cannot tell",
    },
    "browser.drag": {
        "start_x": "int -- CSS px from the viewport's left edge",
        "start_y": "int -- CSS px from the viewport's top edge",
        "end_x": "int -- CSS px from the viewport's left edge",
        "end_y": "int -- CSS px from the viewport's top edge",
    },
    # ``goal`` was here, and is deliberately gone: the descriptor advertised
    # it, the event carried it, and no handler read it. Deleting a param the
    # prompt shipped is exactly the drift this golden table exists to catch,
    # so the entry is removed on purpose, not adjusted to fit.
    "browser.extract_content": {
        "index": "int (optional) -- element index; omit to extract page text",
    },
    "browser.go_back": {},
    "browser.go_forward": {},
    "browser.navigate": {"url": "str"},
    "browser.screenshot": {
        "full_page": "bool (optional) -- capture beyond viewport",
        "format": "png|jpeg|webp (optional)",
        "quality": "int (optional) -- jpeg/webp quality 0-100",
    },
    # ``start_x`` / ``start_y`` added on purpose (user ruling 2026-10-31): an
    # optional start point, which a run whose agent declares no grid never sees.
    "browser.scroll": {
        "direction": "up|down|left|right",
        "amount": "int (optional)",
        "index": "int (optional) -- scroll inside specific element",
        "start_x": "int (optional, the pointer moves there, then the wheel turns; never "
        "with index) -- CSS px from the viewport's left edge",
        "start_y": "int (optional, the pointer moves there, then the wheel turns; never "
        "with index) -- CSS px from the viewport's top edge",
    },
    "browser.select_dropdown": {
        "index": "int -- element index of the <select>",
        "value": "str (optional)",
        "text": "str (optional) -- visible option text",
        "option_index": "int (optional) -- 0-based option index",
    },
    "browser.send_keys": {"keys": "str"},
    "browser.switch_tab": {
        "target_id": (
            "str | None -- the tab_id shown in the observation's tab list; "
            "omit or null for the most recently opened tab"
        )
    },
    "browser.type": {
        "index": "int -- element index",
        "text": "str",
        "clear": "bool (optional, default=true)",
    },
    # The point made optional on purpose (user ruling 2026-11-01): without one
    # the text goes into the field that has focus.
    "browser.type_coordinate": {
        "coordinate_x": "int (optional) -- CSS px from the viewport's left edge",
        "coordinate_y": "int (optional) -- CSS px from the viewport's top edge",
        "text": "str",
        "clear": "bool (optional, default=true)",
    },
    "browser.wait": {"seconds": "float (optional, default=1.0, max=30.0)"},
    "android.click": {"index": "int -- element index from the UI tree"},
    "android.click_coordinate": {"coordinate_x": _ANDROID_X, "coordinate_y": _ANDROID_Y},
    "android.drag": {
        "start_x": _ANDROID_X,
        "start_y": _ANDROID_Y,
        "end_x": _ANDROID_X,
        "end_y": _ANDROID_Y,
    },
    "android.long_press": {"coordinate_x": _ANDROID_X, "coordinate_y": _ANDROID_Y},
    "android.open_app": {"app_name": "str"},
    "android.press_key": {"key": "back|home|enter|delete|recent_apps"},
    "android.scroll": {
        "direction": "up|down|left|right",
        "amount": "int (optional)",
        "index": "int (optional) -- scroll inside specific element",
        "start_x": "int (optional, the swipe starts there; never with index) -- "
        "Physical px from the screen's left edge",
        "start_y": "int (optional, the swipe starts there; never with index) -- "
        "Physical px from the screen's top edge",
    },
    "android.type_coordinate": {
        "coordinate_x": "int (optional) -- Physical px from the screen's left edge",
        "coordinate_y": "int (optional) -- Physical px from the screen's top edge",
        "text": "str",
        "clear": "bool (optional, default=true)",
    },
    "android.wait": {"seconds": "float (optional, default=1.0, max=30.0)"},
}

#: Which fragment each action cannot run without. Written out rather than
#: derived, so adding a DOM-reading action to a capability is a decision
#: someone records here.
_GOLDEN_DEPENDENCIES: dict[str, set[str]] = {
    "browser.click": {"browser.dom"},
    "browser.type": {"browser.dom"},
    "browser.select_dropdown": {"browser.dom"},
    "android.click": {"android.ui_tree"},
}


def _action(key: str) -> Any:
    return ActionRegistrar.get(key)()


# ── Every action has a typed contract ─────────────────────────────────


@pytest.mark.parametrize("key", _KEYS)
def test_every_action_declares_a_params_model(key: str) -> None:
    params = _action(key).Params
    assert isinstance(params, type) and issubclass(params, BaseModel), key
    assert params.model_config.get("extra") == "forbid", (
        f"{key}.Params accepts unknown parameters, so an agent could send "
        f"one and the action would silently ignore it"
    )


@pytest.mark.parametrize("key", _KEYS)
def test_the_descriptor_carries_the_schema(key: str) -> None:
    descriptor = _action(key).descriptor()
    schema = descriptor.params_schema
    assert schema is not None, f"{key} still has params_schema=None"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(descriptor.params), (
        f"{key}: the prose and the schema describe different parameters"
    )


@pytest.mark.parametrize("key", _KEYS)
def test_the_schema_stays_inside_the_closed_union_subset(key: str) -> None:
    """Flat scalars only — what OpenAI strict, Gemini and Anthropic all accept."""
    descriptor = _action(key).descriptor()
    model = params_model_from_schema(descriptor)  # raises if outside the subset
    assert set(model.model_fields) == set(descriptor.params)


@pytest.mark.parametrize("key", _KEYS)
def test_no_schema_publishes_a_default(key: str) -> None:
    """The default belongs to the capability, applied in ``execute``.

    Publishing one invites a provider to fill the param in, which is how
    ``clear: null`` came to override ``clear_by_default``.
    """
    schema = _action(key).descriptor().params_schema or {}
    for name, node in (schema.get("properties") or {}).items():
        assert "default" not in node, f"{key}.{name}"


# ── The prose two other agents print verbatim ─────────────────────────


@pytest.mark.parametrize("key", _KEYS)
def test_the_derived_prose_is_what_the_tree_shipped(key: str) -> None:
    assert key in _GOLDEN_PROSE, (
        f"{key} is a new action; add its param prose to _GOLDEN_PROSE so the "
        f"renderer cannot silently reword Skyvern's and UI-TARS's prompts"
    )
    assert _action(key).descriptor().params == _GOLDEN_PROSE[key]


def test_the_golden_table_covers_the_registry_and_nothing_else() -> None:
    assert set(_GOLDEN_PROSE) == set(_KEYS)


@pytest.mark.parametrize("key", _KEYS)
def test_the_schema_description_is_the_prose_the_prompt_shows(key: str) -> None:
    """One rendering, two channels.

    A model given only the schema — a strict-mode provider passes
    ``description`` and nothing else through — learns the same thing the
    prompt says, including the current value of a capability knob, which
    exists nowhere in the schema's structure.
    """
    descriptor = _action(key).descriptor()
    schema = descriptor.params_schema or {}
    for name, node in (schema.get("properties") or {}).items():
        assert node["description"] == descriptor.params[name], f"{key}.{name}"


# ── Knob values reach the model ───────────────────────────────────────


def test_a_knob_value_shows_up_in_the_param_text() -> None:
    from spectragent.core.capabilities.browser.actions.type_action import TypeAction

    assert (
        TypeAction(clear_by_default=False).descriptor().params["clear"]
        == "bool (optional, default=false)"
    )


def test_a_numeric_knob_pair_shows_up_too() -> None:
    from spectragent.core.capabilities.browser.actions.wait import WaitAction

    assert (
        WaitAction(default_seconds=2.5, max_seconds=9.0).descriptor().params["seconds"]
        == "float (optional, default=2.5, max=9.0)"
    )


# ── Fragment dependencies: one declaration, two levels ────────────────


@pytest.mark.parametrize("key", _KEYS)
def test_action_level_dependencies_are_the_declared_ones(key: str) -> None:
    assert _action(key).requires_fragments() == frozenset(_GOLDEN_DEPENDENCIES.get(key, set()))


def test_a_dependency_is_declared_with_a_fully_qualified_key() -> None:
    """The capability knows which ``dom`` it means; an agent does not.

    This is the deliberate asymmetry with ``__wiring__``, where a key is
    relative because the agent cannot know its provider.
    """
    from spectragent.core.capabilities.browser.actions.click import ClickAction

    (keys,) = param_fragments(ClickAction.Params).values()
    assert keys == frozenset({"browser.dom"})
    assert all("." in key for key in keys)


def test_an_optional_dependent_param_is_dropped_when_its_fragment_is_absent() -> None:
    """``scroll`` survives without a DOM; its ``index`` does not.

    A required dependent param would instead make the whole action depend
    on the fragment — same declaration, the level follows from whether the
    param can be omitted.
    """
    scroll = _action("browser.scroll")
    with_dom = scroll.descriptor(fragments=frozenset({"browser.dom"}))
    without = scroll.descriptor(fragments=frozenset({"browser.screenshot"}))

    assert "index" in with_dom.params
    assert "index" not in without.params
    assert "index" not in (without.params_schema or {})["properties"]
    # The action itself is untouched.
    assert "direction" in without.params


def test_extract_content_loses_its_index_the_same_way() -> None:
    """Same drop, down to an empty parameter list.

    ``index`` is now this action's *only* param, so a run without
    ``browser.dom`` advertises ``extract_content()`` with nothing at all —
    which is correct, and is what the second assertion pins: the drop must
    empty the list, not fall back to teaching a param the run cannot honour.
    """
    action = _action("browser.extract_content")
    assert "index" in action.descriptor(fragments=frozenset({"browser.dom"})).params
    assert action.descriptor(fragments=frozenset()).params == {}


def test_no_context_means_no_filtering() -> None:
    """Introspection asks what an action *can* take, not what this run offers."""
    assert "index" in _action("browser.scroll").descriptor().params


# ── Every agent's action set is consistent with its observation set ───


def _bound(declared: str, capability: str, registered: list[str]) -> str | None:
    """The fully-qualified key *declared* binds to in a run of *capability*, or ``None``.

    A relative name binds to the one provider of that run that offers it: the
    capability, or the Loop (``loop.done``); a qualified one pins its provider.
    """
    if "." in declared:
        return declared if declared.split(".", 1)[0] in (capability, "loop") else None
    found = [
        key
        for key in registered
        if key.rsplit(".", 1)[-1] == declared and key.split(".", 1)[0] in (capability, "loop")
    ]
    return found[0] if len(found) == 1 else None


def test_every_agent_can_satisfy_its_actions_dependencies() -> None:
    """The static scan §4.5 asks for, over every registered agent, by the names its actions bind to.

    A family member is a class of its own (``browser_use.dom``,
    ``browser_use.dom_vision``), so the registry lists every demand there is.

    An agent whose required actions read a fragment it never requires would
    fail at the first step with ``Element N not found in selector_map`` — a
    runtime symptom of a wiring mistake that is visible from the declarations
    alone. A relative name binds per run (``click`` is ``browser.click`` with
    the browser and ``android.click`` with Android), so each agent is scanned
    on every capability it can run on, the ones that offer each action it
    requires, with its declarations bound there; matching names by their last
    segment alone would hold a browser agent's ``click`` to the UI tree
    Android's reads.
    """
    from spectragent.agents._registrar import AgentRegistrar
    from spectragent.agents.prompts import agent_family
    from spectragent.core.registries import (
        AGENTS_PACKAGE,
        agent_families,
        discover_implementations,
    )

    # A run imports only the families its configuration names, so import them
    # all here: scanning whatever happens to be registered scanned nothing
    # when this test ran alone.
    discover_implementations(*(f"{AGENTS_PACKAGE}.{family}" for family in agent_families()))
    capabilities = sorted({key.split(".", 1)[0] for key in _KEYS} - {"loop"})

    scanned: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for agent_id, cls in sorted(AgentRegistrar.get_all().items()):
        if not cls.__module__.startswith(f"{AGENTS_PACKAGE}."):
            continue  # another test's fake, not a shipped agent
        agent = cls()
        scanned.add(agent_family(agent_id))
        actions, _optional = agent.negotiation_demand("action")
        fragments, _ = agent.negotiation_demand("observation")
        for capability in capabilities:
            bound = {declared: _bound(declared, capability, _KEYS) for declared in actions}
            if None in bound.values():
                continue  # the agent cannot run here: an action it requires is not offered
            pairs.add((agent_id, capability))
            have = {key if "." in key else f"{capability}.{key}" for key in fragments}
            for declared, full in sorted(bound.items()):
                assert full is not None
                missing = sorted(_action(full).requires_fragments() - have)
                assert not missing, (
                    f"{agent_id} requires action {declared!r} ({full} on {capability}), which "
                    f"reads {missing} — a fragment it does not require"
                )
    assert sorted(scanned) == list(agent_families())
    assert {("browser_use.dom", "browser"), ("ui_tars", "browser"), ("ui_tars", "android")} <= pairs


@pytest.mark.parametrize(
    ("click", "fragment"), [("browser.click", "dom"), ("android.click", "ui_tree")]
)
def test_a_required_dependent_param_would_be_caught(click: str, fragment: str) -> None:
    """The scan is falsifiable: give ``click`` to an agent with no DOM, or no UI tree."""
    from spectragent.agents.ui_tars import UITarsAgent

    ui_tars = UITarsAgent()
    fragments, _ = ui_tars.negotiation_demand("observation")
    assert fragment not in fragments
    needed = _action(click).requires_fragments()
    assert needed and not {k.rsplit(".", 1)[-1] for k in needed} <= fragments


def test_requires_fragment_is_a_value_not_a_string() -> None:
    """So a typo is a marker nobody reads, not a silent no-op string."""
    assert RequiresFragment("browser.dom") == RequiresFragment("browser.dom")
    assert RequiresFragment("browser.dom") != RequiresFragment("desktop.dom")


# ── What a param means: points and element addresses, declared once ───

#: Which params are one point, and which name an element, as the shipped
#: actions declare them on their ``Params`` (``PointAxis``, ``AddressesElement``).
_GOLDEN_POINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "browser.click_coordinate": (("coordinate_x", "coordinate_y"),),
    "browser.type_coordinate": (("coordinate_x", "coordinate_y"),),
    "browser.drag": (("start_x", "start_y"), ("end_x", "end_y")),
    "browser.scroll": (("start_x", "start_y"),),
    "android.click_coordinate": (("coordinate_x", "coordinate_y"),),
    "android.long_press": (("coordinate_x", "coordinate_y"),),
    "android.type_coordinate": (("coordinate_x", "coordinate_y"),),
    "android.drag": (("start_x", "start_y"), ("end_x", "end_y")),
    "android.scroll": (("start_x", "start_y"),),
}
_GOLDEN_ELEMENTS: dict[str, tuple[str, ...]] = {
    "browser.click": ("index",),
    "browser.type": ("index",),
    "browser.select_dropdown": ("index",),
    "browser.scroll": ("index",),
    "browser.extract_content": ("index",),
    "android.click": ("index",),
    "android.scroll": ("index",),
}


@pytest.mark.parametrize("key", _KEYS)
def test_what_the_params_mean_reaches_the_descriptor(key: str) -> None:
    """An Agent reads it off its run's descriptors; nothing restates it per verb."""
    descriptor = _action(key).descriptor()
    assert descriptor.point_params == _GOLDEN_POINTS.get(key, ())
    assert descriptor.element_params == _GOLDEN_ELEMENTS.get(key, ())


@pytest.mark.parametrize("key", _KEYS)
def test_a_point_is_a_pair_of_ints_and_an_address_requires_its_fragment(key: str) -> None:
    params = ActionRegistrar.get(key).Params
    hints = get_type_hints(params)
    for x, y in point_params(params):
        # An optional point (``scroll``'s start) is two optional ints.
        assert hints[x] == hints[y] and hints[x] in (int, int | None), (key, x, y)
    fragments = param_fragments(params)
    for name in element_params(params):
        assert fragments.get(name), (key, name)


def test_a_param_this_run_cannot_honour_names_no_element() -> None:
    """Narrowed with the params: a run without a DOM has no index for scroll to name an element with."""
    without = _action("browser.scroll").descriptor(fragments=frozenset({"browser.screenshot"}))
    assert without.element_params == ()


def test_a_point_is_never_inferred_from_a_name() -> None:
    class Zoom(BaseModel):
        scale_x: int
        scale_y: int

    assert point_params(Zoom) == []


def test_a_point_declares_both_of_its_axes_once() -> None:
    from typing import Annotated

    class OneAxis(BaseModel):
        at_x: Annotated[int, PointAxis("at", "x")]

    class Twice(BaseModel):
        at_x: Annotated[int, PointAxis("at", "x")]
        at_y: Annotated[int, PointAxis("at", "x")]

    with pytest.raises(TypeError, match="only the x of point 'at'"):
        point_params(OneAxis)
    with pytest.raises(TypeError, match="declares the x of point 'at' twice"):
        point_params(Twice)


# ── A scroll's optional start point ───────────────────────────────────


#: The scrolls that share one param contract, and so one validator's rules.
_SCROLLS = ("browser.scroll", "android.scroll")


@pytest.mark.parametrize("scroll", _SCROLLS)
@pytest.mark.parametrize(
    "params",
    [
        {"direction": "down"},
        {"direction": "down", "index": 3},
        {"direction": "down", "start_x": 10, "start_y": 20},
        {"direction": "down", "amount": 600, "start_x": 0, "start_y": 0},
    ],
)
def test_a_scroll_takes_the_pointer_an_element_or_a_start_point(
    scroll: str, params: dict[str, Any]
) -> None:
    assert ActionRegistrar.get(scroll).Params.model_validate(params)


@pytest.mark.parametrize("scroll", _SCROLLS)
@pytest.mark.parametrize(
    ("params", "said"),
    [
        ({"direction": "down", "start_x": 10}, "a scroll start needs both start_x and start_y"),
        ({"direction": "down", "start_y": 20}, "a scroll start needs both start_x and start_y"),
        (
            {"direction": "down", "index": 3, "start_x": 10, "start_y": 20},
            "scroll takes an element index or a start point, not both",
        ),
    ],
)
def test_a_scroll_start_is_both_axes_and_never_beside_an_index(
    scroll: str, params: dict[str, Any], said: str
) -> None:
    with pytest.raises(ValidationError, match=said):
        ActionRegistrar.get(scroll).Params.model_validate(params)


def test_the_start_is_the_scrolls_one_optional_point() -> None:
    """Optional both ways: it is dropped when its agent declares no grid, never by fragment."""
    scroll = _action("browser.scroll")
    descriptor = scroll.descriptor(fragments=frozenset({"browser.screenshot"}))
    assert descriptor.optional_points == (("start_x", "start_y"),)
    assert "start_x" in descriptor.params and "index" not in descriptor.params
    assert scroll.requires_fragments() == frozenset()


# ── A typing action's optional point ──────────────────────────────────


#: The typing actions that share one param contract (user ruling 2026-11-01).
_TYPINGS = ("browser.type_coordinate", "android.type_coordinate")


@pytest.mark.parametrize("typing", _TYPINGS)
@pytest.mark.parametrize(
    "params",
    [
        {"text": "a"},
        {"text": "a", "clear": False},
        {"text": "a", "coordinate_x": 10, "coordinate_y": 20},
        {"text": "a", "coordinate_x": 0, "coordinate_y": 0},
    ],
)
def test_typing_takes_a_point_or_none(typing: str, params: dict[str, Any]) -> None:
    assert ActionRegistrar.get(typing).Params.model_validate(params)


@pytest.mark.parametrize("typing", _TYPINGS)
@pytest.mark.parametrize(
    "params", [{"text": "a", "coordinate_x": 10}, {"text": "a", "coordinate_y": 20}]
)
def test_a_typing_point_is_both_axes(typing: str, params: dict[str, Any]) -> None:
    with pytest.raises(
        ValidationError, match="a typing point needs both coordinate_x and coordinate_y"
    ):
        ActionRegistrar.get(typing).Params.model_validate(params)


@pytest.mark.parametrize("typing", _TYPINGS)
def test_the_typing_point_is_dropped_when_its_agent_declares_no_grid(typing: str) -> None:
    """As ``scroll``'s start: the action stays, and types into the field that has focus."""
    descriptor = _action(typing).descriptor()
    assert descriptor.optional_points == (("coordinate_x", "coordinate_y"),)
    (plain,) = space_of([descriptor]).descriptors
    assert plain.point_params == () and set(plain.params) == {"text", "clear"}
    assert set((plain.params_schema or {})["properties"]) == {"text", "clear"}
    (framed,) = space_of([descriptor], coordinate_space="normalized_1000").descriptors
    assert framed.point_params == (("coordinate_x", "coordinate_y"),)


@pytest.mark.parametrize("key", _KEYS)
def test_only_done_carries_the_agents_claim_about_the_task(key: str) -> None:
    """``done``'s ``success`` is the agent's statement, not an instruction to the page."""
    expected = ("success",) if key == "loop.done" else ()
    assert claim_params(ActionRegistrar.get(key).Params) == expected
