"""Every action's ``Effect`` is an ``ActionEffect`` model — the output twin of ``Params``.

``docs/architecture_design.md`` §4.5, "动作效果": an action class may declare a
nested ``Effect`` model; the trace reader finds it by the action's key to read
a recorded effect back as its type, so a value that is not an ``ActionEffect``
subclass would make every recorded effect of that action unreadable. The
browser's effects cover pointer, text input, scroll, navigation, tab, select
and wait actions; ``done``, ``screenshot`` and ``extract_content`` report none.
"""

from __future__ import annotations

import pytest
from conscribe import discover

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.effects import (
    NavigationEffect,
    PointerEffect,
    ScrollEffect,
    SelectionEffect,
    TabSwitchEffect,
    TextInputEffect,
    WaitEffect,
)
from spectragent.core.trace.effects import registered_effect
from spectragent.schemas.action import ActionEffect

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
