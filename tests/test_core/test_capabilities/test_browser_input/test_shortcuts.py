"""Which ``send_keys`` chords are the browser's own shortcuts, per measured platform (§4.13).

``input/shortcuts.py`` is a fixed, named table (user ruling 2026-10-24), read
for the measured input platform only: a chord of that platform's table runs
its command; another platform's chord is only reported, with the measured
platform's binding; anything else is no browser shortcut. The bindings are
Chrome's (the module docstring cites where they come from).
"""

from __future__ import annotations

from typing import get_args

import pytest

from spectragent.core.capabilities.browser.input.chords import parse_key_chord
from spectragent.core.capabilities.browser.input.shortcuts import (
    BROWSER_UI,
    COMMANDS,
    OtherPlatform,
    Runs,
    Shortcut,
    WithoutUi,
    binding,
    browser_shortcut,
)
from spectragent.core.capabilities.browser.platform import InputPlatform

PLATFORMS: tuple[InputPlatform, ...] = get_args(InputPlatform)


def match(keys: str, platform: InputPlatform) -> object:
    return browser_shortcut(parse_key_chord(keys), platform)


#: (platform, chord, the shortcut it runs there): Chrome's bindings, every one.
RUNS: list[tuple[InputPlatform, str, Shortcut]] = [
    ("macos", "Meta+[", Shortcut("back")),
    ("macos", "Meta+ArrowLeft", Shortcut("back", yields_to_text=True)),
    ("macos", "Meta+]", Shortcut("forward")),
    ("macos", "Meta+ArrowRight", Shortcut("forward", yields_to_text=True)),
    ("macos", "Meta+R", Shortcut("reload")),
    ("macos", "Meta+Shift+R", Shortcut("hard_reload")),
    ("macos", "Meta+W", Shortcut("close_tab")),
    ("macos", "Control+Tab", Shortcut("next_tab")),
    ("macos", "Meta+Shift+]", Shortcut("next_tab")),
    ("macos", "Control+PageDown", Shortcut("next_tab")),
    ("macos", "Meta+Alt+ArrowRight", Shortcut("next_tab")),
    ("macos", "Control+Shift+Tab", Shortcut("previous_tab")),
    ("macos", "Meta+Shift+[", Shortcut("previous_tab")),
    ("macos", "Control+PageUp", Shortcut("previous_tab")),
    ("macos", "Meta+Alt+ArrowLeft", Shortcut("previous_tab")),
    ("macos", "Meta+1", Shortcut("select_tab", tab=1)),
    ("macos", "Meta+8", Shortcut("select_tab", tab=8)),
    ("macos", "Meta+9", Shortcut("last_tab")),
    *(
        (platform, keys, shortcut)
        for platform in ("windows", "linux")
        for keys, shortcut in (
            ("Alt+ArrowLeft", Shortcut("back")),
            ("Alt+ArrowRight", Shortcut("forward")),
            ("Control+R", Shortcut("reload")),
            ("F5", Shortcut("reload")),
            ("Control+Shift+R", Shortcut("hard_reload")),
            ("Shift+F5", Shortcut("hard_reload")),
            ("Control+F5", Shortcut("hard_reload")),
            ("Control+W", Shortcut("close_tab")),
            ("Control+F4", Shortcut("close_tab")),
            ("Control+Tab", Shortcut("next_tab")),
            ("Control+PageDown", Shortcut("next_tab")),
            ("Control+Shift+Tab", Shortcut("previous_tab")),
            ("Control+PageUp", Shortcut("previous_tab")),
            ("Control+1", Shortcut("select_tab", tab=1)),
            ("Control+8", Shortcut("select_tab", tab=8)),
            ("Control+9", Shortcut("last_tab")),
        )
    ),
    ("linux", "Alt+1", Shortcut("select_tab", tab=1)),
    ("linux", "Alt+9", Shortcut("last_tab")),
]


class TestRuns:
    @pytest.mark.parametrize(("platform", "keys", "shortcut"), RUNS)
    def test_the_measured_platforms_binding_runs_the_command(
        self, platform: InputPlatform, keys: str, shortcut: Shortcut
    ) -> None:
        assert match(keys, platform) == Runs(shortcut)

    @pytest.mark.parametrize(
        ("keys", "command"),
        [
            ("meta+[", "back"),
            ("cmd+[", "back"),
            ("Command+[", "back"),
            ("meta+left", "back"),
            ("cmd+option+right", "next_tab"),
            ("ctrl+tab", "next_tab"),
            ("ctrl+shift+tab", "previous_tab"),
            ("meta+r", "reload"),
            ("META+w", "close_tab"),
            ("Meta+Shift+}", "next_tab"),
            ("Meta+Shift+{", "previous_tab"),
        ],
    )
    def test_spellings_agents_write_match(self, keys: str, command: str) -> None:
        found = match(keys, "macos")
        assert isinstance(found, Runs) and found.shortcut.command == command

    def test_a_bracket_without_shift_is_not_the_shifted_one(self) -> None:
        """``Meta+}`` cannot be typed without Shift: it is no shortcut, not Next tab."""
        assert match("Meta+}", "macos") is None


class TestNotRun:
    @pytest.mark.parametrize(
        ("platform", "keys", "command"),
        [
            ("macos", "Meta+L", "address_bar"),
            ("macos", "Meta+T", "new_tab"),
            ("macos", "Meta+Shift+T", "reopen_closed_tab"),
            ("macos", "Meta+N", "new_window"),
            ("macos", "Meta+Shift+N", "new_window"),
            ("macos", "Meta+Y", "history"),
            ("macos", "Meta+Shift+J", "downloads"),
            ("macos", "Meta+Alt+B", "bookmarks"),
            ("macos", "Meta+F", "find"),
            ("windows", "Control+L", "address_bar"),
            ("windows", "Alt+D", "address_bar"),
            ("windows", "F6", "address_bar"),
            ("windows", "Control+K", "address_bar"),
            ("linux", "Control+T", "new_tab"),
            ("linux", "Control+Shift+T", "reopen_closed_tab"),
            ("linux", "Control+N", "new_window"),
            ("linux", "Control+H", "history"),
            ("linux", "Control+J", "downloads"),
            ("linux", "Control+Shift+O", "bookmarks"),
            ("linux", "Control+F", "find"),
            ("linux", "F3", "find"),
        ],
    )
    def test_shortcuts_for_browser_ui_this_browser_does_not_show(
        self, platform: InputPlatform, keys: str, command: str
    ) -> None:
        assert match(keys, platform) == WithoutUi(command)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("platform", "keys", "expected"),
        [
            ("macos", "Alt+ArrowLeft", OtherPlatform("back", None, "Meta+[")),
            ("macos", "Alt+ArrowRight", OtherPlatform("forward", None, "Meta+]")),
            ("macos", "F5", OtherPlatform("reload", None, "Meta+R")),
            ("macos", "Control+W", OtherPlatform("close_tab", None, "Meta+W")),
            ("macos", "Control+3", OtherPlatform("select_tab", 3, "Meta+3")),
            ("macos", "Control+L", OtherPlatform("address_bar", None, "Meta+L")),
            ("windows", "Meta+[", OtherPlatform("back", None, "Alt+ArrowLeft")),
            ("windows", "Meta+ArrowLeft", OtherPlatform("back", None, "Alt+ArrowLeft")),
            ("windows", "Meta+R", OtherPlatform("reload", None, "Control+R")),
            ("windows", "Meta+Shift+R", OtherPlatform("hard_reload", None, "Control+Shift+R")),
            ("windows", "Meta+Shift+]", OtherPlatform("next_tab", None, "Control+Tab")),
            ("windows", "Alt+2", OtherPlatform("select_tab", 2, "Control+2")),
            ("windows", "Meta+T", OtherPlatform("new_tab", None, "Control+T")),
            ("linux", "Meta+W", OtherPlatform("close_tab", None, "Control+W")),
            ("linux", "Meta+9", OtherPlatform("last_tab", None, "Control+9")),
        ],
    )
    def test_another_platforms_shortcut_names_the_measured_binding(
        self, platform: InputPlatform, keys: str, expected: OtherPlatform
    ) -> None:
        assert match(keys, platform) == expected

    @pytest.mark.parametrize(
        "keys",
        ["Enter", "Escape", "PageDown", "Meta+A", "Control+A", "a", "Tab", "Meta+Shift+W", "+"],
    )
    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_other_keys_are_no_browser_shortcut(self, keys: str, platform: InputPlatform) -> None:
        assert match(keys, platform) is None


class TestTheTable:
    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_no_chord_both_runs_and_needs_browser_ui(self, platform: InputPlatform) -> None:
        assert not set(COMMANDS[platform]) & set(BROWSER_UI[platform])

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_every_command_names_a_binding_that_runs_it(self, platform: InputPlatform) -> None:
        for shortcut in COMMANDS[platform].values():
            chord = binding(platform, shortcut.command, shortcut.tab)
            ran = match(chord, platform)
            assert isinstance(ran, Runs) and ran.shortcut.command == shortcut.command, chord
            assert not ran.shortcut.yields_to_text, f"{chord}: the named binding always runs"
        for command in BROWSER_UI[platform].values():
            assert match(binding(platform, command), platform) == WithoutUi(command)

    def test_the_effect_names_the_bindings_the_descriptor_teaches(self) -> None:
        assert [binding("macos", c) for c in ("back", "forward", "reload", "close_tab")] == [
            "Meta+[",
            "Meta+]",
            "Meta+R",
            "Meta+W",
        ]
        assert [binding("windows", c) for c in ("back", "forward", "reload", "close_tab")] == [
            "Alt+ArrowLeft",
            "Alt+ArrowRight",
            "Control+R",
            "Control+W",
        ]
        assert binding("linux", "select_tab", 4) == "Control+4"
