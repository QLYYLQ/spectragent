"""``browser.send_keys`` runs the browser's own shortcuts on the measured platform (§4.13).

User ruling 2026-10-24: a chord that is the browser's shortcut for Back,
Forward, Reload (hard reload included) on the measured input platform runs the
command through the code ``go_back`` / ``go_forward`` use, and no key reaches
the page; its ``KeyPressEffect`` says the command, the URL before and after and
whether the document loaded. A shortcut for browser UI this browser does not
show, or another platform's shortcut, is pressed on the page as before, and
the effect adds one sentence about the browser. macOS's Meta+ArrowLeft /
Meta+ArrowRight go to a focused text field, where they move the caret.

Every test runs the real action against a real ``ActionWatchdog`` on a
scripted CDP (``_effect_tab.py``) that also keeps a navigation history and a
document loader. The tab commands are ``test_browser_shortcut_tabs.py``; the
real-Chrome half is ``test_browser_shortcuts_live.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.send_keys import SendKeysAction
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    KeyPressEffect,
    Location,
    ShortcutOfAnotherPlatform,
    ShortcutRan,
    ShortcutWithoutUi,
    TabFacts,
)
from spectragent.core.capabilities.browser.input.chords import parse_key_chord
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.trace.effects import registered_effect
from spectragent.schemas.action import ActionResult
from tests.test_core.test_capabilities.test_browser_actions._effect_tab import (
    Call,
    ProbeCDP,
    effect_tab,
    facts,
    reading,
)

A, B, C = "https://a.example/", "https://b.example/", "https://c.example/"


class HistoryProbeCDP(ProbeCDP):
    """:class:`ProbeCDP` whose tab also has a navigation history and a document loader.

    The history holds ``urls`` with ``current`` the current entry; ``Page.navigateToHistoryEntry``
    commits at once. ``Page.reload`` commits a new document (a new loader)
    unless ``reload_commits`` is false.
    """

    def __init__(
        self,
        urls: list[str],
        index: int,
        *,
        reload_commits: bool = True,
        **probe: Any,
    ) -> None:
        super().__init__(**probe)
        self.urls = list(urls)
        self.current = index
        self.reload_commits = reload_commits
        self.loaders = 1

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        params = params or {}
        if method == "Page.getNavigationHistory":
            self.calls.append(Call(method, params, session_id))
            entries = [{"id": 10 + i, "url": url} for i, url in enumerate(self.urls)]
            return {"currentIndex": self.current, "entries": entries}
        if method == "Page.navigateToHistoryEntry":
            self.calls.append(Call(method, params, session_id))
            self.current = params["entryId"] - 10
            return {}
        if method == "Page.reload":
            self.calls.append(Call(method, params, session_id))
            if self.reload_commits:
                self.loaders += 1
            return {}
        if method == "Page.getFrameTree":
            self.calls.append(Call(method, params, session_id))
            frame = {"id": "MAIN", "loaderId": f"L{self.loaders}", "url": self.urls[self.current]}
            return {"frameTree": {"frame": frame}}
        if method == "Runtime.evaluate" and params.get("expression") == "document.readyState":
            self.calls.append(Call(method, params, session_id))
            return {"result": {"type": "string", "value": "complete"}}
        return await super().send(method, params, session_id, timeout)

    def sent(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]


async def press(
    cdp: ProbeCDP, keys: str, platform: InputPlatform, **watchdog_kwargs: Any
) -> ActionResult:
    async with effect_tab(cdp, platform=platform, **watchdog_kwargs) as tab:
        return await SendKeysAction(post_delay_ms=0).execute({"keys": keys}, capability=tab)  # type: ignore[arg-type]


def key_press(result: ActionResult) -> KeyPressEffect:
    assert result.success, result.error
    assert isinstance(result.effect, KeyPressEffect)
    return result.effect


def ran(result: ActionResult) -> ShortcutRan:
    shortcut = key_press(result).shortcut
    assert isinstance(shortcut, ShortcutRan), shortcut
    return shortcut


# ── the navigation commands run ──────────────────────────────────────


BACK = [("macos", "Meta+["), ("macos", "Meta+ArrowLeft"), ("windows", "Alt+ArrowLeft")]
BACK += [("linux", "Alt+ArrowLeft")]
FORWARD = [("macos", "Meta+]"), ("macos", "Meta+ArrowRight"), ("windows", "Alt+ArrowRight")]
FORWARD += [("linux", "Alt+ArrowRight")]
RELOAD = [("macos", "Meta+R"), ("windows", "Control+R"), ("windows", "F5"), ("linux", "F5")]
RELOAD += [("linux", "Control+R")]
HARD_RELOAD = [("macos", "Meta+Shift+R"), ("windows", "Control+Shift+R")]
HARD_RELOAD += [("windows", "Shift+F5"), ("linux", "Control+F5"), ("linux", "Control+Shift+R")]


class TestNavigationShortcuts:
    @pytest.mark.parametrize(("platform", "keys"), BACK)
    async def test_back_goes_to_the_earlier_entry(self, platform: InputPlatform, keys: str) -> None:
        cdp = HistoryProbeCDP([A, B], 1)
        result = await press(cdp, keys, platform)
        shortcut = ran(result)
        assert (shortcut.command, shortcut.chord) == ("back", keys)
        assert (shortcut.before, shortcut.after, shortcut.document_loaded) == (
            Location(url=B),
            Location(url=A),
            True,
        )
        assert cdp.current == 0
        assert [c.params for c in cdp.sent("Page.navigateToHistoryEntry")] == [{"entryId": 10}]
        assert not cdp.inputs(), "the key does not reach the page"
        effect = key_press(result)
        assert (effect.keys, effect.text_entered, effect.value_changed) == (
            (),
            False,
            "unavailable",
        )
        assert effect.describe() == (
            f"{keys} ran the browser's Back; the URL changed from {B} to {A}; "
            "the document has finished loading."
        )

    @pytest.mark.parametrize(("platform", "keys"), FORWARD)
    async def test_forward_goes_to_the_later_entry(
        self, platform: InputPlatform, keys: str
    ) -> None:
        cdp = HistoryProbeCDP([A, B], 0)
        shortcut = ran(await press(cdp, keys, platform))
        assert shortcut.command == "forward"
        assert (shortcut.before, shortcut.after) == (Location(url=A), Location(url=B))
        assert not cdp.inputs()

    @pytest.mark.parametrize(("platform", "keys"), RELOAD)
    async def test_reload_loads_the_document_again(
        self, platform: InputPlatform, keys: str
    ) -> None:
        cdp = HistoryProbeCDP([A, B], 1)
        result = await press(cdp, keys, platform)
        shortcut = ran(result)
        assert shortcut.command == "reload"
        assert (shortcut.before, shortcut.after, shortcut.document_loaded) == (
            Location(url=B),
            Location(url=B),
            True,
        )
        assert [c.params for c in cdp.sent("Page.reload")] == [{"ignoreCache": False}]
        assert not cdp.inputs()
        assert key_press(result).describe() == (
            f"{keys} ran the browser's Reload; the URL stayed {B}; "
            "the document has finished loading."
        )

    @pytest.mark.parametrize(("platform", "keys"), HARD_RELOAD)
    async def test_a_hard_reload_ignores_the_cache(
        self, platform: InputPlatform, keys: str
    ) -> None:
        cdp = HistoryProbeCDP([A], 0)
        result = await press(cdp, keys, platform)
        assert ran(result).command == "hard_reload"
        assert [c.params for c in cdp.sent("Page.reload")] == [{"ignoreCache": True}]
        chord = parse_key_chord(keys).text  # canonical: Shift+Meta+R
        assert key_press(result).describe().startswith(f"{chord} ran the browser's Hard reload; ")

    async def test_a_reload_that_does_not_commit_in_time_leaves_the_url_after_unknown(
        self,
    ) -> None:
        """Never the document being left reported as the reloaded one."""
        cdp = HistoryProbeCDP([A], 0, reload_commits=False)
        shortcut = ran(await press(cdp, "Meta+R", "macos", action_effect_timeout_s=0.2))
        assert (shortcut.before, shortcut.after, shortcut.document_loaded) == (
            Location(url=A),
            "unknown",
            "unknown",
        )

    async def test_back_with_no_earlier_entry_fails_naming_the_shortcut(self) -> None:
        cdp = HistoryProbeCDP([A], 0)
        result = await press(cdp, "Meta+[", "macos")
        assert result.success is False and result.effect is None
        assert result.error is not None
        assert result.error.startswith(
            "Meta+[ is the browser's Back shortcut, and it did not run: "
        )
        assert "no navigation history entry" in result.error
        assert not cdp.inputs() and not cdp.sent("Page.navigateToHistoryEntry")

    async def test_the_focus_is_read_before_and_after(self) -> None:
        cdp = HistoryProbeCDP(
            [A, B],
            1,
            field="node",
            readings=[reading("button", "Search", digest=None, tag="BUTTON", entry="none")],
            focus={"facts": facts("link", "Home")},
        )
        effect = key_press(await press(cdp, "Meta+[", "macos"))
        assert effect.target == ElementFacts(role="button", name="Search")
        assert effect.focus_after == ElementFacts(role="link", name="Home")
        assert (
            cdp.index("field_ref") < cdp.index("Page.navigateToHistoryEntry") < cdp.index("focus")
        )


class TestCaretKeysOnMacos:
    """Meta+ArrowLeft / Meta+ArrowRight: Chrome goes back or forward only when the page did not use them."""

    @pytest.mark.parametrize("keys", ["Meta+ArrowLeft", "Meta+ArrowRight"])
    @pytest.mark.parametrize("entry", ["text", "widget", None])
    async def test_a_field_that_takes_text_gets_the_key(self, keys: str, entry: str | None) -> None:
        cdp = HistoryProbeCDP(
            [A, B, C],
            1,
            field="node",
            readings=[reading("textbox", "Name", digest="3:2", entry=entry)],
        )
        effect = key_press(await press(cdp, keys, "macos"))
        assert effect.shortcut is None
        assert effect.keys == (keys,)
        downs = [c.params for c in cdp.inputs() if c.params.get("type") == "keyDown"]
        assert [(d["key"], d["modifiers"]) for d in downs] == [(keys.split("+")[1], 4)]
        assert cdp.current == 1 and not cdp.sent("Page.navigateToHistoryEntry")

    async def test_with_nothing_focused_it_goes_back(self) -> None:
        cdp = HistoryProbeCDP([A, B], 1)
        assert ran(await press(cdp, "Meta+ArrowLeft", "macos")).command == "back"
        assert not cdp.inputs()

    async def test_meta_bracket_goes_back_from_a_text_field_too(self) -> None:
        cdp = HistoryProbeCDP(
            [A, B], 1, field="node", readings=[reading("textbox", "Name", digest="3:2")]
        )
        assert ran(await press(cdp, "Meta+[", "macos")).command == "back"
        assert not cdp.inputs()


# ── shortcuts that are not run: the key goes to the page ─────────────


class TestShortcutsForBrowserUi:
    @pytest.mark.parametrize(
        ("platform", "keys", "line"),
        [
            (
                "macos",
                "Meta+L",
                "Meta+L is the browser's address bar shortcut; this browser shows no address "
                "bar, so nothing in the browser happened.",
            ),
            (
                "macos",
                "Meta+T",
                "Meta+T is the browser's New tab shortcut; this browser shows no new-tab page, "
                "so nothing in the browser happened.",
            ),
            (
                "macos",
                "Shift+Meta+T",
                "Shift+Meta+T is the browser's Reopen closed tab shortcut; this browser shows "
                "no tab strip, so nothing in the browser happened.",
            ),
            (
                "windows",
                "Control+L",
                "Control+L is the browser's address bar shortcut; this browser shows no "
                "address bar, so nothing in the browser happened.",
            ),
            (
                "linux",
                "F6",
                "F6 is the browser's address bar shortcut; this browser shows no address bar, "
                "so nothing in the browser happened.",
            ),
            (
                "linux",
                "Control+F",
                "Control+F is the browser's Find shortcut; this browser shows no find bar, so "
                "nothing in the browser happened.",
            ),
        ],
    )
    async def test_the_key_goes_to_the_page_and_the_effect_says_what_it_is(
        self, platform: InputPlatform, keys: str, line: str
    ) -> None:
        cdp = HistoryProbeCDP([A], 0)
        effect = key_press(await press(cdp, keys, platform))
        assert isinstance(effect.shortcut, ShortcutWithoutUi)
        assert effect.keys == (keys,)
        assert [c.params["type"] for c in cdp.inputs()][0] == "keyDown"
        assert effect.describe() == (
            f"Pressed {keys} with nothing focused; nothing is focused now. {line}"
        )

    async def test_it_is_said_with_a_text_field_focused_too(self) -> None:
        cdp = HistoryProbeCDP(
            [A], 0, field="node", readings=[reading("textbox", "Name", digest="3:2")]
        )
        effect = key_press(await press(cdp, "Meta+L", "macos"))
        assert effect.shortcut == ShortcutWithoutUi(chord="Meta+L", command="address_bar")


class TestAnotherPlatformsShortcut:
    @pytest.mark.parametrize(
        ("platform", "keys", "line"),
        [
            ("macos", "Alt+ArrowLeft", "On macOS the browser's Back is Meta+[."),
            ("macos", "Control+W", "On macOS the browser's Close tab is Meta+W."),
            ("macos", "F5", "On macOS the browser's Reload is Meta+R."),
            (
                "macos",
                "Control+L",
                "On macOS the browser's address bar shortcut is Meta+L; this browser shows no "
                "address bar.",
            ),
            ("windows", "Meta+[", "On Windows the browser's Back is Alt+ArrowLeft."),
            ("linux", "Meta+2", "On Linux the browser's Select tab 2 is Control+2."),
        ],
    )
    async def test_with_nothing_editable_focused_the_effect_names_the_measured_binding(
        self, platform: InputPlatform, keys: str, line: str
    ) -> None:
        cdp = HistoryProbeCDP([A, B], 1)
        effect = key_press(await press(cdp, keys, platform))
        assert isinstance(effect.shortcut, ShortcutOfAnotherPlatform)
        assert effect.shortcut.platform == platform
        assert effect.keys == (keys,) and cdp.inputs(), "the key went to the page"
        assert not cdp.sent("Page.navigateToHistoryEntry") and not cdp.sent("Page.reload")
        assert effect.describe() == (
            f"Pressed {keys} with nothing focused; nothing is focused now. {line}"
        )

    async def test_the_modifiers_are_sent_as_written(self) -> None:
        cdp = HistoryProbeCDP([A, B], 1)
        await press(cdp, "Alt+ArrowLeft", "macos")
        downs = [c.params for c in cdp.inputs() if c.params.get("type") == "keyDown"]
        assert [(d["key"], d["modifiers"]) for d in downs] == [("ArrowLeft", 1)]

    @pytest.mark.parametrize("entry", ["text", "widget", "read-only", None])
    async def test_in_a_field_it_is_an_editing_key_and_nothing_is_said(
        self, entry: str | None
    ) -> None:
        """On macOS Alt+ArrowLeft moves the caret a word; Control+F moves it a character."""
        for keys in ("Alt+ArrowLeft", "Control+F"):
            cdp = HistoryProbeCDP(
                [A, B],
                1,
                field="node",
                readings=[reading("textbox", "Name", digest="3:2", entry=entry)],
            )
            effect = key_press(await press(cdp, keys, "macos"))
            assert effect.shortcut is None, keys
            assert effect.keys == (keys,)

    async def test_an_ordinary_key_is_no_shortcut(self) -> None:
        cdp = HistoryProbeCDP([A], 0)
        effect = key_press(await press(cdp, "Enter", "macos"))
        assert effect.shortcut is None
        assert effect.describe() == "Pressed Enter with nothing focused; nothing is focused now."


# ── read back from a trace ────────────────────────────────────────────


def _effect(shortcut: Any) -> KeyPressEffect:
    return KeyPressEffect(
        effect_id="browser.send_keys",
        target="nothing",
        sensitive=False,
        value_changed="unavailable",
        keys=() if isinstance(shortcut, ShortcutRan) else ("Meta+L",),
        text_entered=False,
        focus_after="nothing",
        shortcut=shortcut,
    )


@pytest.mark.parametrize(
    "shortcut",
    [
        None,
        ShortcutRan(
            chord="Meta+[",
            command="back",
            before=Location(url=B),
            after="unknown",
            document_loaded="unknown",
        ),
        ShortcutRan(
            chord="Meta+2",
            command="select_tab",
            tab=2,
            before=TabFacts(tab_id="T1", location=Location(url=A)),
            after=TabFacts(tab_id="T2", location="unknown"),
            document_loaded=False,
        ),
        ShortcutWithoutUi(chord="Meta+L", command="address_bar"),
        ShortcutOfAnotherPlatform(
            chord="Alt+ArrowLeft", command="back", platform="macos", binding="Meta+["
        ),
    ],
)
def test_a_recorded_effect_reads_back_as_its_type(shortcut: Any) -> None:
    effect = _effect(shortcut)
    restored = registered_effect("browser.send_keys").model_validate_json(effect.model_dump_json())
    assert isinstance(restored, KeyPressEffect)
    assert restored == effect and type(restored.shortcut) is type(shortcut)


def test_a_tab_command_never_carries_a_url_and_a_navigation_never_a_tab() -> None:
    with pytest.raises(ValueError, match="reports no TabFacts"):
        ShortcutRan(
            chord="Meta+[",
            command="back",
            before=TabFacts(tab_id="T1", location="unknown"),
            after="unknown",
            document_loaded="unknown",
        )
    with pytest.raises(ValueError, match="reports no Location"):
        ShortcutRan(
            chord="Meta+W",
            command="close_tab",
            before=Location(url=A),
            after="unknown",
            document_loaded="unknown",
        )
    with pytest.raises(ValueError, match="tab position"):
        ShortcutRan(
            chord="Meta+1",
            command="select_tab",
            before="unknown",
            after="unknown",
            document_loaded="unknown",
        )


def test_the_tab_lines() -> None:
    before = TabFacts(tab_id="T1", location=Location(url=A))
    after = TabFacts(tab_id="T2", location=Location(url=B))
    switch = ShortcutRan(
        chord="Control+Tab", command="next_tab", before=before, after=after, document_loaded=True
    )
    assert switch.line() == (
        f"Control+Tab ran the browser's Next tab; switched from tab T1 to tab T2 ({B}); "
        "the document has finished loading."
    )
    close = switch.model_copy(update={"chord": "Meta+W", "command": "close_tab"})
    assert close.line() == (
        f"Meta+W ran the browser's Close tab; closed tab T1 ({A}), and the focus moved to "
        f"tab T2 ({B}); the document has finished loading."
    )
    numbered = switch.model_copy(update={"chord": "Meta+2", "command": "select_tab", "tab": 2})
    assert numbered.line().startswith("Meta+2 ran the browser's Select tab 2; switched from")
