"""No key event af sends carries a native key code; what pages read is kept.

On macOS, Chrome matched an unhandled key event that carried
``nativeVirtualKeyCode`` against its own menu and opened
``chrome://settings/help`` in front of the agent's tab, which then stayed a
background page (``input/keyboard.py``, "No native key code"; the real
browser is in ``test_browser_keys_live.py``). Every path that builds key
events is checked here, without a browser: ``browser.send_keys`` (named keys,
single characters, chords, a key name it does not know), and the keyboard's
typing and clearing. What a page reads — ``key``, ``code``,
``windowsVirtualKeyCode`` and the text — is unchanged.
"""

from __future__ import annotations

import string
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input import HumanKeyboard, InstantRhythm, Keystroke
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    _KEY_INFO,
    ActionWatchdog,
    _resolve_key_event,
)
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from tests.test_core.test_capabilities.test_browser_action_click_robustness import FakeCDP

from .conftest import RecordCDP

#: Every key name ``browser.send_keys`` knows, a letter of each case, a digit, punctuation.
SEND_KEYS = (*_KEY_INFO, "a", "Z", "5", "%", "[")
#: Chords as agents write them, and a key name ``send_keys`` does not know.
CHORDS = ("Enter", "ctrl+a", "Meta+A", "shift+Tab", "alt+a", "ctrl+Enter", "cmd+c", "F5")


class _Rhythm:
    """Humanized with fixed timings, so every character becomes key events."""

    humanized = True

    def generate(self, text: str) -> list[Keystroke]:
        return [Keystroke(ch, 1.0, 1.0) for ch in text]


def _key_events(cdp: FakeCDP) -> list[dict[str, Any]]:
    return [call.params for call in cdp.calls if call.method == "Input.dispatchKeyEvent"]


@pytest.mark.parametrize("raw", SEND_KEYS)
def test_a_resolved_key_has_no_native_code_and_keeps_what_pages_read(raw: str) -> None:
    payload, char = _resolve_key_event(raw)
    assert "nativeVirtualKeyCode" not in payload
    assert payload["key"] and payload["code"] and payload["windowsVirtualKeyCode"] > 0
    if raw in _KEY_INFO:
        key, code, vk, text = _KEY_INFO[raw]
        assert (payload["key"], payload["code"], payload["windowsVirtualKeyCode"], char) == (
            key,
            code,
            vk,
            text,
        )


def test_enter_is_a_real_enter() -> None:
    """What a React-style handler checks: ``key`` Enter, ``keyCode`` 13, and a char that fires keypress."""
    payload, char = _resolve_key_event("enter")
    assert payload == {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
    assert char == "\r"


@pytest.mark.parametrize("keys", [*SEND_KEYS, *CHORDS])
async def test_send_keys_sends_no_native_code(keys: str) -> None:
    cdp = FakeCDP()
    watchdog = ActionWatchdog(input_platform="linux", cdp=cdp, selector_map_getter=dict)  # type: ignore[arg-type]
    await watchdog._on_send_keys(SendKeysEvent(keys=keys))
    events = _key_events(cdp)
    assert [event["type"] for event in events if event["type"] != "char"] == ["keyDown", "keyUp"]
    assert all("nativeVirtualKeyCode" not in event for event in events)


async def test_a_chord_keeps_its_modifiers() -> None:
    cdp = FakeCDP()
    watchdog = ActionWatchdog(input_platform="linux", cdp=cdp, selector_map_getter=dict)  # type: ignore[arg-type]
    await watchdog._on_send_keys(SendKeysEvent(keys="ctrl+shift+a"))
    down, char, up = _key_events(cdp)
    assert (down["key"], down["code"], down["windowsVirtualKeyCode"]) == ("a", "KeyA", 65)
    assert down["modifiers"] == char["modifiers"] == up["modifiers"] == 2 | 8
    assert char == {"type": "char", "text": "a", "key": "a", "modifiers": 10}


@pytest.mark.parametrize(
    "chord,expected",
    [
        ("ctrl+a", 2),
        ("Control+a", 2),
        ("alt+a", 1),
        ("Option+a", 1),
        ("meta+a", 4),
        ("cmd+a", 4),
        ("Command+a", 4),
        ("shift+a", 8),
    ],
)
async def test_named_chords_use_the_cdp_modifier_bits(chord, expected):
    cdp = FakeCDP()
    watchdog = ActionWatchdog(input_platform="linux", cdp=cdp, selector_map_getter=dict)  # type: ignore[arg-type]
    await watchdog._on_send_keys(SendKeysEvent(keys=chord))
    assert all(event["modifiers"] == expected for event in _key_events(cdp))


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
@pytest.mark.parametrize(
    "chord",
    [
        "ctrl+a",
        "Control+A",
        "meta+a",
        "cmd+a",
        "Command+A",
        "ctrl+shift+a",
        "meta+shift+a",
        "alt+ctrl+a",
        "ctrl+meta+a",
        "ctrl+c",
        "cmd+x",
    ],
)
async def test_only_the_measured_platforms_native_select_all_gets_an_editing_command(
    platform: InputPlatform,
    chord: str,
):
    cdp = FakeCDP()
    watchdog = ActionWatchdog(cdp=cdp, input_platform=platform, selector_map_getter=dict)  # type: ignore[arg-type]
    await watchdog._on_send_keys(SendKeysEvent(keys=chord))
    events = _key_events(cdp)
    native = chord.lower() in (
        ("meta+a", "cmd+a", "command+a") if platform == "macos" else ("ctrl+a", "control+a")
    )
    assert events[0].get("commands", []) == (["selectAll"] if native else [])
    assert all("commands" not in event for event in events[1:])
    if native:
        assert [event["type"] for event in events] == ["keyDown", "keyUp"]


@pytest.mark.parametrize("chord", ["", " ", "ctrl+", "+a", "ctrl++a", "hyper+a", "commnad+a"])
async def test_empty_keys_and_unknown_modifiers_are_refused_before_dispatch(chord):
    cdp = FakeCDP()
    watchdog = ActionWatchdog(cdp=cdp, input_platform="macos", selector_map_getter=dict)  # type: ignore[arg-type]
    result = await watchdog._on_send_keys(SendKeysEvent(keys=chord))
    assert result is not None and result["error"]
    assert _key_events(cdp) == []


@pytest.mark.parametrize("platform", [None, "unknown"])
def test_keyboard_requires_a_measured_platform(platform):
    with pytest.raises(ValueError, match="measured browser input_platform"):
        HumanKeyboard(RecordCDP(), input_platform=platform)


async def test_typing_sends_no_native_code(no_sleep: list[float]) -> None:
    cdp = RecordCDP()
    keyboard = HumanKeyboard(cdp, input_platform="linux", rhythm=_Rhythm())
    text = string.printable  # letters, digits, punctuation, space, \t\n\r\x0b\x0c
    await keyboard.type_text(text)
    events = cdp.key_events()
    assert events, "the humanized rhythm types with key events"
    assert all("nativeVirtualKeyCode" not in event for event in events)
    downs = [event for event in events if event["type"] == "keyDown"]
    assert all(event["text"] and event["windowsVirtualKeyCode"] > 0 for event in downs)
    enter = next(event for event in downs if event["key"] == "Enter")
    assert (enter["code"], enter["windowsVirtualKeyCode"], enter["text"]) == ("Enter", 13, "\r")


async def test_clearing_sends_no_native_code(no_sleep: list[float]) -> None:
    for rhythm in (InstantRhythm(), _Rhythm()):
        cdp = RecordCDP()
        await HumanKeyboard(cdp, input_platform="linux", rhythm=rhythm).clear_focused()  # type: ignore[arg-type]
        events = cdp.key_events()
        assert [event["key"] for event in events if event["type"] == "keyDown"][1:] == [
            "a",
            "Backspace",
        ]
        assert all("nativeVirtualKeyCode" not in event for event in events)
