"""``browser.send_keys`` takes a named key, a single character, or a ``+`` chord — nothing else.

A model wrote a whole account-page URL into ``send_keys`` and was told it
succeeded: the old executor sent an unknown multi-character string as one key
event carrying no text (``docs/architecture_design.md`` §4.13). Every other
string is now refused before any key event is dispatched, with an error naming
``type`` as the way to enter text. Legitimate named keys keep working.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.send_keys import SendKeysAction
from spectragent.core.capabilities.browser.input.chords import (
    InvalidKeysError,
    parse_key_chord,
)
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from tests.test_core.test_capabilities.test_browser_action_click_robustness import FakeCDP

#: Inputs that are not keys: what a model writes when it means to type text.
NOT_KEYS = (
    "https://space.bilibili.com/12345/favlist",
    "hello world",
    "Enter the password",
    "ctrl+hello",
    "Control+Shift+Space bar",
    "abc",
    "F13",
)


@pytest.mark.parametrize(
    "raw,text,mask",
    [
        ("Enter", "Enter", 0),
        ("enter", "Enter", 0),
        ("ESC", "Escape", 0),
        ("return", "Enter", 0),
        ("down", "ArrowDown", 0),
        ("PageDown", "PageDown", 0),
        ("F5", "F5", 0),
        ("f12", "F12", 0),
        ("Space", "Space", 0),
        ("a", "a", 0),
        ("%", "%", 0),
        ("Shift", "Shift", 0),
        ("ctrl+a", "Control+A", 2),
        ("Meta+A", "Meta+A", 4),
        ("cmd+shift+z", "Shift+Meta+Z", 4 | 8),
        ("shift+a", "Shift+a", 8),
        ("shift + Tab", "Shift+Tab", 8),
        ("Alt+F4", "Alt+F4", 1),
        # The plus key: alone, or the last key of a chord.
        ("+", "+", 0),
        (" + ", "+", 0),
        ("Control++", "Control++", 2),
        ("ctrl + +", "Control++", 2),
        ("ctrl+shift++", "Control+Shift++", 2 | 8),
    ],
)
def test_named_keys_characters_and_chords_are_accepted(raw: str, text: str, mask: int) -> None:
    chord = parse_key_chord(raw)
    assert chord.text == text
    assert chord.modifier_mask == mask
    assert "nativeVirtualKeyCode" not in chord.payload


@pytest.mark.parametrize("raw", NOT_KEYS)
def test_anything_else_is_refused_and_names_type(raw: str) -> None:
    with pytest.raises(InvalidKeysError) as caught:
        parse_key_chord(raw)
    message = str(caught.value)
    assert "send_keys accepts only a key or a key chord" in message
    assert "To enter text, use type." in message


@pytest.mark.parametrize("raw", ["++", "+++", "ctrl+", "+a", "ctrl++a", "hyper++"])
def test_a_plus_anywhere_but_alone_or_last_still_separates_parts(raw: str) -> None:
    with pytest.raises(InvalidKeysError, match="send_keys accepts only a key or a key chord"):
        parse_key_chord(raw)


async def test_the_plus_key_is_pressed_with_its_character() -> None:
    cdp = FakeCDP()
    watchdog = ActionWatchdog(cdp=cdp, input_platform="linux", selector_map_getter=dict)  # type: ignore[arg-type]
    result = await watchdog._on_send_keys(SendKeysEvent(keys="Control++"))
    assert result is not None and "error" not in result, result
    events = [call.params for call in cdp.calls if call.method == "Input.dispatchKeyEvent"]
    assert [event["type"] for event in events][0] == "keyDown"
    assert events[0]["key"] == "+" and events[0]["modifiers"] == 2
    assert result["effect_facts"]["keys"] == ("Control++",)


def test_a_long_refused_input_is_shortened_in_the_error() -> None:
    raw = "https://example.com/" + "x" * 200
    with pytest.raises(InvalidKeysError) as caught:
        parse_key_chord(raw)
    assert raw not in str(caught.value)
    assert "..." in str(caught.value)


@pytest.mark.parametrize("raw", NOT_KEYS)
async def test_the_executor_sends_no_key_event_for_a_non_key(raw: str) -> None:
    cdp = FakeCDP()
    watchdog = ActionWatchdog(cdp=cdp, input_platform="linux", selector_map_getter=dict)  # type: ignore[arg-type]
    result = await watchdog._on_send_keys(SendKeysEvent(keys=raw))
    assert result is not None and "To enter text, use type." in result["error"]
    assert [call for call in cdp.calls if call.method == "Input.dispatchKeyEvent"] == []


class _Untouchable:
    """A capability the refused action must not reach: any attribute read fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"send_keys touched the capability ({name}) for an input it refuses")


@pytest.mark.parametrize("raw", NOT_KEYS)
async def test_the_action_refuses_before_it_emits(raw: str) -> None:
    result = await SendKeysAction().execute({"keys": raw}, capability=_Untouchable())  # type: ignore[arg-type]
    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("send_keys accepts only a key or a key chord")
    assert result.error.endswith("To enter text, use type.")
