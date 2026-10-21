"""Every action's ``Effect`` is an ``ActionEffect`` model — the output twin of ``Params``.

``docs/architecture_design.md`` §4.5, "动作效果": an action class may declare a
nested ``Effect`` model; the trace reader finds it by the action's key to read
a recorded effect back as its type, so a value that is not an ``ActionEffect``
subclass would make every recorded effect of that action unreadable. The
browser's effects cover pointer, text input, scroll, navigation, tab, select
and wait actions; ``done``, ``screenshot`` and ``extract_content`` report none.
"""

from __future__ import annotations

import inspect

import pytest
from conscribe import discover

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    NavigationEffect,
    PointerEffect,
    ScrollEffect,
    ScrollOffset,
    SelectionEffect,
    TabSwitchEffect,
    TextInputEffect,
    ViewportPoint,
    WaitEffect,
)
from spectragent.core.trace.effects import registered_effect
from spectragent.schemas.action import ActionEffect, write_viewport_point

BROWSER_EFFECTS: dict[str, type[ActionEffect] | None] = {
    "browser.click": PointerEffect,
    "browser.click_coordinate": PointerEffect,
    "browser.drag": PointerEffect,
    "browser.type": TextInputEffect,
    "browser.type_coordinate": TextInputEffect,
    "browser.send_keys": TextInputEffect,
    "browser.scroll": ScrollEffect,
    "browser.navigate": NavigationEffect,
    "browser.go_back": NavigationEffect,
    "browser.go_forward": NavigationEffect,
    "browser.switch_tab": TabSwitchEffect,
    "browser.select_dropdown": SelectionEffect,
    "browser.wait": WaitEffect,
    "browser.done": None,
    "browser.screenshot": None,
    "browser.extract_content": None,
}


def _shipped_actions() -> dict[str, type]:
    discover("spectragent.core.capabilities")
    return {
        key: cls
        for key, cls in ActionRegistrar.get_all().items()
        if isinstance(cls, type) and cls.__module__.startswith("spectragent.")
    }


def test_every_shipped_action_declares_an_effect_model_or_none() -> None:
    for key, cls in _shipped_actions().items():
        effect = cls.__dict__.get("Effect", getattr(cls, "Effect", None))
        assert effect is None or (isinstance(effect, type) and issubclass(effect, ActionEffect)), (
            f"{key}.Effect is {effect!r}, not an ActionEffect model"
        )


def test_the_browser_actions_report_the_designed_effects() -> None:
    shipped = _shipped_actions()
    browser = {key: cls for key, cls in shipped.items() if key.startswith("browser.")}
    assert set(browser) == set(BROWSER_EFFECTS)
    assert {key: cls.Effect for key, cls in browser.items()} == BROWSER_EFFECTS


@pytest.mark.parametrize(
    "key", sorted(key for key, effect in BROWSER_EFFECTS.items() if effect is not None)
)
def test_the_trace_reader_finds_each_effect_by_its_action_key(key: str) -> None:
    _shipped_actions()
    assert registered_effect(key) is BROWSER_EFFECTS[key]


def test_effect_models_are_frozen_and_closed() -> None:
    for effect in {e for e in BROWSER_EFFECTS.values() if e is not None}:
        assert effect.model_config.get("frozen") is True, effect
        assert effect.model_config.get("extra") == "forbid", effect


# ── Points are written by the renderer (§4.5, "点由渲染方决定写法") ──────────


def _on_a_grid(x: int, y: int) -> str:
    """A renderer's grid: half the viewport's units, written as a reply writes a point."""
    return f"<{x // 2}, {y // 2}>"


def test_every_shipped_effect_takes_the_point_writer() -> None:
    """Agents always pass ``write_point``, so every ``describe`` accepts it, keyword-only."""
    for key, cls in _shipped_actions().items():
        effect = getattr(cls, "Effect", None)
        if effect is None:
            continue
        parameter = inspect.signature(effect.describe).parameters.get("write_point")
        assert parameter is not None, f"{key}.Effect.describe takes no write_point"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, key
        assert parameter.default is write_viewport_point, key


def test_every_point_a_pointer_effect_states_goes_through_the_writer() -> None:
    drag = PointerEffect(
        effect_id="browser.drag",
        point=ViewportPoint(x=640, y=618),
        release_point=ViewportPoint(x=100, y=20),
        hit=ElementFacts(role="slider", name="Volume"),
        focus_after="nothing",
    )
    assert drag.describe() == (
        'Dragged from (640, 618) to (100, 20), pressing on slider "Volume"; nothing is focused now.'
    )
    assert drag.describe(write_point=_on_a_grid) == (
        'Dragged from <320, 309> to <50, 10>, pressing on slider "Volume"; nothing is focused now.'
    )
    click = drag.model_copy(update={"release_point": None, "effect_id": "browser.click"})
    assert click.describe(write_point=_on_a_grid).startswith("Clicked at <320, 309> on ")
    unknown = click.model_copy(update={"point": "unknown"})
    assert unknown.describe(write_point=_on_a_grid) == unknown.describe()


def test_a_scroll_distance_is_not_a_point() -> None:
    """Offsets are distances: they stay in the viewport's px whatever the writer."""
    effect = ScrollEffect(
        effect_id="browser.scroll",
        direction="down",
        container="page",
        before=ScrollOffset(x=0, y=0),
        after=ScrollOffset(x=0, y=600),
        at_boundary=False,
    )
    assert (
        effect.describe(write_point=_on_a_grid)
        == effect.describe()
        == ("Scroll down moved the page by 600 px (y 0 → 600); it can scroll further down.")
    )
