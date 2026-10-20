"""The browser's effect models and their ``describe()`` lines.

``describe()`` is the one line every Agent renders (``docs/architecture_design.md``
§4.5, "动作效果"): derived from the typed fields alone, deterministic, one line
of English facts, no judgement about the task; ``unknown`` and ``unavailable``
said as such; a sensitive field's content and length never said. String facts
are wrapped in models so a URL or option text reading ``unknown`` is never a gap.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    Location,
    NavigationEffect,
    OptionFacts,
    PointerEffect,
    ScrollEffect,
    ScrollOffset,
    SelectionEffect,
    TabFacts,
    TabSwitchEffect,
    TextInputEffect,
    ViewportPoint,
    WaitEffect,
)
from spectragent.schemas.action import ActionEffect

BUTTON = ElementFacts(role="button", name="Search")
FIELD = ElementFacts(role="textbox", name="Query")
PASSWORD = ElementFacts(role="textbox", name="Password")

#: One instance per shape worth describing, with the line it must produce.
CASES: list[tuple[ActionEffect, str]] = [
    (
        PointerEffect(
            effect_id="browser.click",
            point=ViewportPoint(x=412, y=230),
            hit=BUTTON,
            on_target=True,
            focus_after=FIELD,
        ),
        'Clicked at (412, 230) on button "Search", the targeted element; '
        'focus is now on textbox "Query".',
    ),
    (
        PointerEffect(
            effect_id="browser.click",
            point=ViewportPoint(x=120, y=100),
            hit=ElementFacts(role="generic", name="Cookie banner"),
            on_target=False,
            focus_after="nothing",
        ),
        'Clicked at (120, 100) on generic "Cookie banner", not the targeted element; '
        "nothing is focused now.",
    ),
    (
        PointerEffect(
            effect_id="browser.click_coordinate",
            point=ViewportPoint(x=5, y=6),
            hit="nothing",
            focus_after="unknown",
        ),
        "Clicked at (5, 6) on no element; what is focused now is unknown.",
    ),
    (
        PointerEffect(
            effect_id="browser.drag",
            point=ViewportPoint(x=10, y=20),
            release_point=ViewportPoint(x=300, y=20),
            hit="unavailable",
            focus_after="unavailable",
        ),
        "Dragged from (10, 20) to (300, 20), pressing on an element that is not measurable "
        "here; what is focused now is not measurable here.",
    ),
    (
        TextInputEffect(
            effect_id="browser.type",
            target=PASSWORD,
            sensitive=True,
            value_changed=True,
            keys=("Control+A", "Backspace"),
            text_entered=True,
            focus_after=PASSWORD,
        ),
        'Pressed Control+A, Backspace, then typed text into textbox "Password" (a sensitive '
        'field; its content is not shown); its value changed; focus is now on textbox "Password".',
    ),
    (
        TextInputEffect(
            effect_id="browser.type_coordinate",
            target=FIELD,
            sensitive=False,
            value_changed=False,
            text_entered=True,
            focus_after=FIELD,
        ),
        'Typed text into textbox "Query"; its value did not change; '
        'focus is now on textbox "Query".',
    ),
    (
        TextInputEffect(
            effect_id="browser.send_keys",
            target="nothing",
            sensitive=False,
            value_changed="unavailable",
            keys=("Escape",),
            text_entered=False,
            focus_after="nothing",
        ),
        "Pressed Escape with nothing focused; nothing is focused now.",
    ),
    (
        TextInputEffect(
            effect_id="browser.send_keys",
            target="unknown",
            sensitive="unknown",
            value_changed="unknown",
            keys=("Enter",),
            text_entered=False,
            focus_after="unknown",
        ),
        "Pressed Enter into an unknown element; whether its value changed is unknown; "
        "what is focused now is unknown.",
    ),
    (
        ScrollEffect(
            effect_id="browser.scroll",
            direction="down",
            container="page",
            before=ScrollOffset(x=0, y=0),
            after=ScrollOffset(x=0, y=300),
            at_boundary=False,
        ),
        "Scroll down moved the page by 300 px (y 0 → 300); it can scroll further down.",
    ),
    (
        ScrollEffect(
            effect_id="browser.scroll",
            direction="right",
            container="element",
            element=ElementFacts(role="list", name="Carousel"),
            before=ScrollOffset(x=600, y=0),
            after=ScrollOffset(x=600, y=0),
            at_boundary=True,
        ),
        'Scroll right moved element list "Carousel" by 0 px (x 600 → 600); '
        "it is at its right edge.",
    ),
    (
        NavigationEffect(
            effect_id="browser.navigate",
            before=Location(url="https://a.test/"),
            after=Location(url="https://b.test/x"),
            document_loaded=True,
        ),
        "The URL changed from https://a.test/ to https://b.test/x; "
        "the document has finished loading.",
    ),
    (
        NavigationEffect(
            effect_id="browser.go_back",
            before=Location(url="https://a.test/"),
            after="unknown",
            document_loaded="unknown",
        ),
        "The URL after the action is unknown; whether the document finished loading is unknown.",
    ),
    (
        TabSwitchEffect(
            effect_id="browser.switch_tab",
            before=TabFacts(tab_id="T1", url="https://a.test/"),
            after=TabFacts(tab_id="T2", url="https://b.test/"),
            document_loaded=False,
        ),
        "Switched from tab T1 to tab T2 (https://b.test/); "
        "the document was still loading when the action returned.",
    ),
    (
        SelectionEffect(
            effect_id="browser.select_dropdown",
            selected=OptionFacts(index=1, text="Banana"),
            value_changed=True,
        ),
        'The dropdown now shows option 1 "Banana"; the selection changed.',
    ),
    (
        SelectionEffect(
            effect_id="browser.select_dropdown",
            selected=OptionFacts(index=2, text=None),
            value_changed=False,
        ),
        "The dropdown now shows option 2 (text withheld: a sensitive field); "
        "the selection did not change.",
    ),
    (WaitEffect(effect_id="browser.wait", waited_ms=1003), "Waited 1003 ms."),
]


@pytest.mark.parametrize(("effect", "line"), CASES, ids=[c[0].effect_id for c in CASES])
def test_describe_is_one_deterministic_line(effect: ActionEffect, line: str) -> None:
    assert effect.describe() == line
    assert "\n" not in line
    again = type(effect).model_validate_json(effect.model_dump_json())
    assert again == effect and again.describe() == line


def test_a_url_or_option_text_that_reads_unknown_is_not_a_gap() -> None:
    effect = SelectionEffect(
        effect_id="browser.select_dropdown",
        selected=OptionFacts(index=0, text="unknown"),
        value_changed=True,
    )
    again = SelectionEffect.model_validate_json(effect.model_dump_json())
    assert isinstance(again.selected, OptionFacts) and again.selected.text == "unknown"
    nav = NavigationEffect(
        effect_id="browser.navigate",
        before=Location(url="unknown"),
        after=Location(url="unavailable"),
        document_loaded=True,
    )
    again_nav = NavigationEffect.model_validate_json(nav.model_dump_json())
    assert isinstance(again_nav.before, Location) and isinstance(again_nav.after, Location)


def test_a_long_name_or_url_is_clipped_in_the_line_only() -> None:
    url = "https://example.test/" + "a" * 400
    effect = NavigationEffect(
        effect_id="browser.navigate",
        before=Location(url=url),
        after=Location(url=url),
        document_loaded=True,
    )
    assert isinstance(effect.after, Location) and effect.after.url == url
    assert url not in effect.describe() and "..." in effect.describe()


def test_effects_are_frozen_and_refuse_unknown_fields() -> None:
    effect = WaitEffect(effect_id="browser.wait", waited_ms=5)
    with pytest.raises(ValidationError):
        effect.waited_ms = 6  # type: ignore[misc]
    with pytest.raises(ValidationError):
        WaitEffect(effect_id="browser.wait", waited_ms=5, extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        WaitEffect(effect_id="browser.wait", waited_ms=-1)


def test_a_scroll_delta_is_measured_only_when_both_offsets_were() -> None:
    known = ScrollEffect(
        effect_id="browser.scroll",
        direction="up",
        container="page",
        before=ScrollOffset(x=0, y=900),
        after=ScrollOffset(x=0, y=600),
        at_boundary=False,
    )
    assert known.delta == ScrollOffset(x=0, y=-300)
    gap = known.model_copy(update={"after": "unknown"})
    assert gap.delta is None
