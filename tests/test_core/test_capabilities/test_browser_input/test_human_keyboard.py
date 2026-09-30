"""HumanKeyboard controller — instant-rhythm equivalence + real key events.

The canary first: with the default (``instant``) rhythm, typing is exactly
one ``Input.insertText`` and nothing else, so no run that has not asked for
keystroke timing pays for it or changes shape.

Then what the humanized path has to be true of, measured against headless
Chrome before it was written: ``Input.insertText`` fires only ``beforeinput``
and ``input`` — no ``keydown``, no ``keypress``, no ``keyup`` — so a rhythm
delivered through it would be timing nobody can observe.
``Input.dispatchKeyEvent`` with a ``text`` field on the ``keyDown`` is the
path that produces the real sequence, and the one used here.

The schedule runs on the same virtual clock as the mouse's (``conftest``), so
"the page sees these intervals" is asserted rather than slept through.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.input import (
    HumanKeyboard,
    HumanRhythm,
    InstantRhythm,
    Keystroke,
)

from .conftest import RecordCDP, VirtualClock


class _ScriptedRhythm:
    """A humanized rhythm with fixed timings — the deadline tests' input."""

    def __init__(self, gap_ms: float = 100.0, hold_ms: float = 80.0) -> None:
        self.gap_ms = gap_ms
        self.hold_ms = hold_ms

    @property
    def humanized(self) -> bool:
        return True

    def generate(self, text: str) -> list[Keystroke]:
        return [Keystroke(ch, self.gap_ms, self.hold_ms) for ch in text]


class _VersionCDP(RecordCDP):
    """A browser whose OS can differ from the agent host's."""

    def __init__(
        self,
        user_agent: str,
        clock: VirtualClock | None = None,
        cost_ms: float = 0.0,
    ) -> None:
        super().__init__(clock, cost_ms)
        self.user_agent = user_agent

    async def send(self, method, params=None, session_id=None):  # type: ignore[no-untyped-def]
        result = await super().send(method, params, session_id)
        if method == "Browser.getVersion":
            return {"userAgent": self.user_agent}
        return result


# ── 1. the default is untouched ───────────────────────────────────────


class TestInstantRhythmEquivalence:
    """``instant`` must reproduce the legacy single bulk insert exactly."""

    async def test_typing_is_one_insert_text_and_nothing_else(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        keyboard = HumanKeyboard(cdp)  # instant by default
        await keyboard.type_text("hello world", session_id="sid")

        assert [m for m, _ in cdp.calls] == ["Input.insertText"]
        assert cdp.calls[0][1] == {"text": "hello world"}
        assert cdp.key_events() == []
        assert no_sleep == []

    async def test_an_explicit_instant_rhythm_is_the_same(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        keyboard = HumanKeyboard(cdp, rhythm=InstantRhythm())
        assert keyboard.humanized is False
        await keyboard.type_text("abc")
        assert cdp.inserts() == ["abc"]
        assert no_sleep == []

    @pytest.mark.parametrize("rhythm", [None, _ScriptedRhythm()], ids=["instant", "human"])
    async def test_the_session_is_per_call_so_a_frame_gets_the_keys(
        self, no_sleep: list[float], rhythm: object
    ) -> None:
        """Keystrokes follow focus, and focus can be in an out-of-process frame.

        Unlike the cursor's, the session is a parameter rather than
        instance state — that is the whole reason ``_insert_text_at_focus``
        can hand its own resolved session down.
        """
        sessions: list[str | None] = []

        class _SessionCDP(RecordCDP):
            async def send(self, method, params=None, session_id=None):  # type: ignore[no-untyped-def]
                sessions.append(session_id)
                return await super().send(method, params, session_id)

        cdp = _SessionCDP()
        await HumanKeyboard(cdp, rhythm=rhythm).type_text("x", session_id="frame-42")  # type: ignore[arg-type]
        assert sessions and set(sessions) == {"frame-42"}

    async def test_empty_text_still_goes_through_the_bulk_path(self, no_sleep: list[float]) -> None:
        # Nothing to time, and the legacy call is what downstream code (and
        # a page's own ``input`` handler for a cleared field) has seen.
        cdp = RecordCDP()
        await HumanKeyboard(cdp, rhythm=HumanRhythm(seed=1)).type_text("")
        assert cdp.inserts() == [""]


class TestClearFocused:
    @pytest.mark.parametrize(
        ("user_agent", "modifier", "code", "vk", "mask"),
        [
            ("Chrome (Macintosh; Intel Mac OS X)", "Meta", "MetaLeft", 91, 4),
            ("Chrome (X11; Linux x86_64)", "Control", "ControlLeft", 17, 2),
        ],
        ids=["mac-browser", "linux-cloud-browser"],
    )
    async def test_the_browser_os_gets_a_physical_select_all_and_backspace(
        self,
        no_sleep: list[float],
        user_agent: str,
        modifier: str,
        code: str,
        vk: int,
        mask: int,
    ) -> None:
        cdp = _VersionCDP(user_agent)
        keyboard = HumanKeyboard(cdp, rhythm=InstantRhythm())
        await keyboard.clear_focused(session_id="focused-frame")

        assert cdp.calls[0][0] == "Browser.getVersion"
        events = cdp.key_events()
        assert [(e["type"], e["key"], e["code"], e["windowsVirtualKeyCode"]) for e in events] == [
            ("keyDown", modifier, code, vk),
            ("keyDown", "a", "KeyA", 65),
            ("keyUp", "a", "KeyA", 65),
            ("keyUp", modifier, code, vk),
            ("keyDown", "Backspace", "Backspace", 8),
            ("keyUp", "Backspace", "Backspace", 8),
        ]
        assert [e["nativeVirtualKeyCode"] for e in events] == [vk, 65, 65, vk, 8, 8]
        assert [e.get("modifiers", 0) for e in events] == [mask, mask, mask, 0, 0, 0]
        assert [e.get("commands") for e in events] == [
            None,
            ["selectAll"],
            None,
            None,
            None,
            None,
        ]
        assert all("text" not in event for event in events)
        assert no_sleep == []

    async def test_the_browser_platform_is_reused_for_the_next_clear(
        self, no_sleep: list[float]
    ) -> None:
        cdp = _VersionCDP("Chrome (X11; Linux x86_64)")
        keyboard = HumanKeyboard(cdp)
        await keyboard.clear_focused()
        await keyboard.clear_focused()
        assert [method for method, _ in cdp.calls].count("Browser.getVersion") == 1
        assert [e["key"] for e in cdp.key_events() if e["type"] == "keyDown"] == [
            "Control",
            "a",
            "Backspace",
            "Control",
            "a",
            "Backspace",
        ]

    async def test_human_rhythm_pauses_after_selection_before_deleting(
        self, clock: VirtualClock
    ) -> None:
        class _ClearRhythm:
            humanized = True

            def generate(self, text: str) -> list[Keystroke]:
                assert text == "a\b"
                return [Keystroke("a", 100.0, 80.0), Keystroke("\b", 240.0, 90.0)]

        class _TimedCDP(_VersionCDP):
            def __init__(self) -> None:
                super().__init__("Chrome (Macintosh)", clock, cost_ms=17.0)
                self.key_times: list[float] = []

            async def send(self, method, params=None, session_id=None):  # type: ignore[no-untyped-def]
                if method == "Input.dispatchKeyEvent":
                    self.key_times.append(clock.now)
                return await super().send(method, params, session_id)

        cdp = _TimedCDP()
        await HumanKeyboard(cdp, rhythm=_ClearRhythm()).clear_focused()  # type: ignore[arg-type]
        assert len(cdp.key_times) == 6
        # The flight is measured from A's release; the modifier's own
        # release consumes one CDP round trip inside that same interval.
        assert cdp.key_times[4] - cdp.key_times[2] == pytest.approx(0.240)
        assert cdp.key_times[4] - cdp.key_times[3] >= 0.200
        assert cdp.key_times[5] - cdp.key_times[4] == pytest.approx(0.090)

    @pytest.mark.requires_chrome
    async def test_chrome_sees_real_keys_and_the_field_is_cleared(self) -> None:
        """The CDP editing command must select; modifier bits alone do not."""
        from spectragent.core.capabilities.browser import BrowserCapability
        from spectragent.core.capabilities.browser.backends.local_chrome import (
            LocalChromeProvider,
        )

        cap = BrowserCapability()
        cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
        try:
            await cap.setup()
            sid = cap.default_session_id
            await cap.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "document.body.innerHTML = '<input id=field>';"
                        "window.keysSeen = [];"
                        "const field = document.getElementById('field');"
                        "field.value = 'old value'; field.focus();"
                        "field.setSelectionRange(field.value.length, field.value.length);"
                        "field.addEventListener('keydown', e => keysSeen.push({"
                        "key:e.key, code:e.code, keyCode:e.keyCode,"
                        "ctrl:e.ctrlKey, meta:e.metaKey}));"
                    )
                },
                session_id=sid,
            )
            await HumanKeyboard(cap.cdp).clear_focused(session_id=sid)
            result = await cap.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": "({value: field.value, keys: window.keysSeen})",
                    "returnByValue": True,
                },
                session_id=sid,
            )
            observed = (result or {})["result"]["value"]
            assert observed["value"] == ""
            keys = observed["keys"]
            assert len(keys) == 3
            assert keys[0]["key"] in {"Meta", "Control"}
            assert keys[1]["key"] == "a" and keys[1]["code"] == "KeyA"
            assert keys[1]["keyCode"] == 65
            assert keys[1]["meta"] or keys[1]["ctrl"]
            assert keys[2]["key"] == keys[2]["code"] == "Backspace"
            assert keys[2]["keyCode"] == 8
        finally:
            await cap.teardown()


# ── 2. the humanized path dispatches real key events ──────────────────


class TestHumanizedKeyEvents:
    async def test_one_keydown_keyup_pair_per_character(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        keyboard = HumanKeyboard(cdp, rhythm=_ScriptedRhythm())
        assert keyboard.humanized is True
        await keyboard.type_text("ab")

        assert [p["type"] for p in cdp.key_events()] == [
            "keyDown",
            "keyUp",
            "keyDown",
            "keyUp",
        ]
        assert cdp.inserts() == []  # no bulk insert anywhere

    async def test_the_keydown_carries_the_text_that_makes_keypress_fire(
        self, no_sleep: list[float]
    ) -> None:
        """Measured: without ``text`` on the keyDown, Chrome fires no keypress
        and no input, so the character is never entered at all."""
        cdp = RecordCDP()
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm()).type_text("a")
        down, up = cdp.key_events()
        assert down == {
            "type": "keyDown",
            "text": "a",
            "key": "a",
            "code": "KeyA",
            "windowsVirtualKeyCode": 65,
            "nativeVirtualKeyCode": 65,
        }
        assert up == {
            "type": "keyUp",
            "key": "a",
            "code": "KeyA",
            "windowsVirtualKeyCode": 65,
            "nativeVirtualKeyCode": 65,
        }

    @pytest.mark.parametrize(
        ("char", "code", "vk", "shift"),
        [
            ("a", "KeyA", 65, False),
            ("Z", "KeyZ", 90, True),
            ("7", "Digit7", 55, False),
            (" ", "Space", 32, False),
            (".", "Period", 190, False),
            ("-", "Minus", 189, False),
            ("!", "Digit1", 49, True),  # the shifted key reports the *key*
            ("?", "Slash", 191, True),
            ("\n", "Enter", 13, False),
            ("\t", "Tab", 9, False),
        ],
    )
    async def test_the_us_layout_is_reported_per_character(
        self, no_sleep: list[float], char: str, code: str, vk: int, shift: bool
    ) -> None:
        cdp = RecordCDP()
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm()).type_text(char)
        down = cdp.key_events()[0]
        assert (down["code"], down["windowsVirtualKeyCode"]) == (code, vk)
        assert down.get("modifiers", 0) == (8 if shift else 0)

    async def test_the_modifier_is_on_the_release_too(self, no_sleep: list[float]) -> None:
        # A listener reading ``shiftKey`` off keyup must see it as well.
        cdp = RecordCDP()
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm()).type_text("A")
        assert all(e.get("modifiers") == 8 for e in cdp.key_events())

    async def test_a_rhythm_that_rewrites_the_text_is_refused(self) -> None:
        class _Liar:
            humanized = True

            def generate(self, text: str) -> list[Keystroke]:
                return [Keystroke("x", 1.0, 1.0)]

        with pytest.raises(ValueError, match="one Keystroke per character"):
            await HumanKeyboard(RecordCDP(), rhythm=_Liar()).type_text("abc")


# ── 3. the modelled intervals are the ones the page sees ──────────────


class TestTheScheduleIsTheRhythm:
    async def test_the_gaps_and_holds_are_spent_as_wall_time(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock)
        start = clock.now
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm(100.0, 80.0)).type_text("abc")

        # gap, hold, gap, hold, gap, hold — the string's typing duration.
        assert clock.sleeps == pytest.approx([0.100, 0.080] * 3)
        assert clock.now - start == pytest.approx(0.540)

    async def test_dispatch_latency_is_absorbed_into_the_interval(
        self, clock: VirtualClock
    ) -> None:
        """The whole reason this rides the deadline scheduler.

        Sleeping the gap and *then* awaiting the CDP round trip would make a
        100 ms inter-key interval arrive as ~117 ms against headless Chrome
        — the timing a collector reads would be the transport's, not the
        model's.
        """
        cdp = RecordCDP(clock, cost_ms=17.0)
        start = clock.now
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm(100.0, 80.0)).type_text("abcd")

        assert len(cdp.key_events()) == 8
        # Each slot pays for its own round trip out of its own span; only the
        # last one has nothing after it to absorb it.
        assert clock.sleeps == pytest.approx([0.100] + [0.063, 0.083] * 3 + [0.063])
        assert clock.now - start == pytest.approx(4 * 0.180 + 0.017)

    async def test_a_transport_slower_than_the_schedule_never_strands_a_keyup(
        self, clock: VirtualClock
    ) -> None:
        """A dropped waypoint is a dropped frame; a dropped keyUp is a stuck key.

        So a slot whose time is gone rides along with the next slot sent
        instead of being skipped — the typing coarsens, the key sequence
        stays exactly one press and one release per character, in order.
        """
        cdp = RecordCDP(clock, cost_ms=400.0)
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm(100.0, 80.0)).type_text("abcd")

        events = cdp.key_events()
        assert [e["type"] for e in events] == ["keyDown", "keyUp"] * 4
        assert [e["key"] for e in events] == ["a", "a", "b", "b", "c", "c", "d", "d"]

    async def test_a_real_rhythm_produces_per_character_variation(
        self, clock: VirtualClock
    ) -> None:
        cdp = RecordCDP(clock)
        await HumanKeyboard(cdp, rhythm=HumanRhythm(seed=21)).type_text("hello there")
        gaps = clock.sleeps[::2]
        assert len(gaps) == 11
        assert len({round(g, 6) for g in gaps}) == 11  # no two alike

    async def test_the_same_seed_types_the_same_way_through_the_controller(
        self, clock: VirtualClock
    ) -> None:
        async def run() -> list[float]:
            before = len(clock.sleeps)
            keyboard = HumanKeyboard(RecordCDP(clock), rhythm=HumanRhythm(seed=33))
            await keyboard.type_text("replay me")
            return clock.sleeps[before:]

        assert await run() == pytest.approx(await run())


# ── 4. CJK is explicitly different, not silently wrong ────────────────


class TestNonAsciiDelivery:
    async def test_cjk_goes_through_per_character_insert_text(self, clock: VirtualClock) -> None:
        """No key event bears '你'.

        Dispatching a keydown whose ``key`` is a Han character produces (as
        measured) ``code: ""`` and ``keyCode: 0``, which no physical key
        can. A real IME would fire ``keydown``/229 plus composition events
        for pinyin we never saw. So the character is inserted on its own,
        on the rhythm's schedule: the page gets ``beforeinput``/``input``
        per character at human intervals and no contradictory key event.
        """
        cdp = RecordCDP(clock)
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm(120.0, 60.0)).type_text("你好")

        assert cdp.key_events() == []
        assert cdp.inserts() == ["你", "好"]
        # Still two slots per character, so the string takes the time the
        # rhythm asked for — the dwell it cannot spend on a release it
        # spends holding still.
        assert clock.sleeps == pytest.approx([0.120, 0.060] * 2)

    async def test_a_mixed_string_uses_both_paths_in_order(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock)
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm()).type_text("a好b")

        assert [m for m, _ in cdp.calls] == [
            "Input.dispatchKeyEvent",  # a down
            "Input.dispatchKeyEvent",  # a up
            "Input.insertText",  # 好
            "Input.dispatchKeyEvent",  # b down
            "Input.dispatchKeyEvent",  # b up
        ]
        assert cdp.inserts() == ["好"]

    @pytest.mark.parametrize("text", ["café", "naïve", "日本語", "Привет", "😀"])
    async def test_every_non_ascii_character_still_arrives(
        self, no_sleep: list[float], text: str
    ) -> None:
        cdp = RecordCDP()
        await HumanKeyboard(cdp, rhythm=_ScriptedRhythm()).type_text(text)
        typed = "".join(
            e["text"] for m, e in cdp.calls if m == "Input.insertText" or e.get("type") == "keyDown"
        )
        assert typed == text
