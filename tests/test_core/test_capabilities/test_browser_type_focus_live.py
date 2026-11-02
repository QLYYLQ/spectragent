"""Typing goes into a text field or nowhere, in a real headless Chrome.

``docs/architecture_design.md`` §4.5, "动作效果" (the text-input actions):
``type`` / ``type_coordinate`` judge the focused element before any key is
sent. With nothing focused, a select-all chord selects the whole page's text
and the text then goes nowhere, so the action sends no clear and no text, and
fails, naming what has focus. Through the capability's own actions, against
``fields.html`` of the local effects site (``tests/_helpers/effects_site.py``),
each case on a freshly loaded page:

- typing at a point on no control, with and without a clear: the action
  fails, the page saw no keydown (no Backspace), nothing is selected and no
  field changed; the select-all chord the clear would have sent does select
  the page's text there (the control case);
- a button, by coordinate and by index, a read-only field, a ``textbox``
  widget marked ``aria-readonly`` and a light-DOM component with role
  ``combobox``: refused the same way, naming the element;
- a text field, a textarea, a ``contenteditable``, a text field in a same-site
  ``<iframe>``, an ``<iframe>`` whose body is the editor, a field a widget
  focuses a moment after the click, a text field inside a closed shadow root
  (which script cannot see: typed into as asked), and a text field by index:
  typed into as before;
- a ``div`` with role ``textbox`` that is no editor and handles keys itself:
  it gets the clear's keys without Chrome's select-all command, so it empties
  itself and nothing on the page is selected;
- ``type_coordinate`` without a point (user ruling 2026-11-01): typed into the
  text field a click focused, and into the field a widget focuses a moment
  after the click that opens it; refused the same way, sending nothing, with
  nothing focused and with a button focused;
- ``type_coordinate`` at a point while a field has focus (user rulings of
  2026-11-02, ``docs/architecture_design.md`` §4.9): a press on the caption
  beside another field keeps focus in the filled field, outside its field as
  drawn, and is refused, the field keeping its text; a press on a button that
  focuses a field elsewhere, a press into another field and a press into the
  focused field itself are typed into; so is a press that keeps focus where
  it was inside the field as drawn: another line of a code editor whose focus
  sits in a hidden one-line textarea, the focused input's own label, an icon
  in the input's box; and a press on the focused input's own ``<label for>``
  beside it, with no box of their own (user ruling 2026-11-02). A press on the
  label of another field moves focus to that field, which takes the text.

Unit counterpart: ``test_browser_actions/test_text_input_effects.py``
(``TestTypingNeedsATextField``, ``TestTypingAtAPointGoesWhereFocusWent``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import ElementFacts, TextInputEffect
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.effects_site import FIELDS_EMPTY_POINT, EffectsSite, centre, effects_site

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
TEXT = "Ada"
#: What the page holds before anything is typed.
UNTOUCHED = {
    "input": "",
    "area": "",
    "editable": "",
    "frame": "",
    "frame_editor": "",
    "readonly": "Shanghai",
    "late": "",
    "custom": "Old",
    "locked": "Fixed",
    "component": "Beijing",
    "closed": "",
    "first": "Grace",
    "last": "",
    "code": "",
    "labelled": "",
    "iconed": "",
    "given": "",
    "family": "",
}
#: The page's state: its selection, the keys its document saw, and every field's content.
STATE = """(() => {
  const $ = id => document.getElementById(id);
  const inner = id => $(id).contentDocument;
  return {
    selection: String(window.getSelection()),
    keys: window.__keys,
    fields: {
      input: $('text_input').value,
      area: $('text_area').value,
      editable: $('text_editable').innerText.trim(),
      frame: inner('text_frame').getElementById('inner').value,
      frame_editor: inner('text_frame_editor').body.innerText.trim(),
      readonly: $('text_readonly').value,
      late: $('text_late').value,
      custom: $('text_custom').textContent,
      locked: $('text_locked').textContent,
      component: $('text_component').textContent,
      closed: window.__closedField.value,
      first: $('text_first').value,
      last: $('text_last').value,
      code: $('text_code_input').value,
      labelled: $('text_labelled').value,
      iconed: $('text_iconed').value,
      given: $('text_given').value,
      family: $('text_family').value,
    },
  };
})()"""


@dataclass
class Case:
    result: ActionResult
    state: dict[str, Any]
    #: Where a coordinate case typed, or the index an index case typed into;
    #: ``None`` for typing without a point.
    aim: tuple[int, int] | int | None


@dataclass
class Session:
    cases: dict[str, Case] = field(default_factory=dict)
    platform: str = ""


@asynccontextmanager
async def _browser() -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.dom", "browser.browser_meta"}), 60)
        yield cap
    finally:
        await cap.teardown()


async def _state(cap: BrowserCapability) -> dict[str, Any]:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": STATE, "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    return dict(reply["result"]["value"])


async def _load(cap: BrowserCapability, site: EffectsSite) -> None:
    loaded = await cap.execute_action(
        Action(name="browser.navigate", params={"url": site.url("fields.html")})
    )
    assert loaded.success, loaded.error


async def _index_of(cap: BrowserCapability, cycle: int, element_id: str) -> int:
    """Observe the page and return the selector-map index of ``#element_id``."""
    await cap.begin_observation(cycle)
    await cap.resolve_fragment("browser.dom")
    for index, node in cap.selector_map.items():
        if node.attributes.get("id") == element_id:
            return index
    raise AssertionError(f"#{element_id} is not indexed")


async def _click(cap: BrowserCapability, point: tuple[int, int]) -> None:
    click = await cap.execute_action(
        Action(
            name="browser.click_coordinate",
            params={"coordinate_x": point[0], "coordinate_y": point[1]},
        )
    )
    assert click.success, click.error


def _session() -> Session:
    async def _all() -> Session:
        session = Session()
        with effects_site() as site:
            async with _browser() as cap:
                session.platform = str(cap._input_platform)

                async def at(
                    label: str,
                    point: tuple[int, int],
                    *,
                    clear: bool | None = None,
                    focused: str | None = None,
                ) -> None:
                    """Type at *point* on a fresh page, after a click into *focused* (a box name)."""
                    await _load(cap, site)
                    if focused is not None:
                        await _click(cap, centre(focused))
                    params: dict[str, Any] = {
                        "coordinate_x": point[0],
                        "coordinate_y": point[1],
                        "text": TEXT,
                    }
                    if clear is not None:
                        params["clear"] = clear
                    result = await cap.execute_action(
                        Action(name="browser.type_coordinate", params=params)
                    )
                    await asyncio.sleep(0.1)
                    session.cases[label] = Case(result, await _state(cap), point)

                async def by_index(label: str, cycle: int, element_id: str) -> None:
                    await _load(cap, site)
                    index = await _index_of(cap, cycle, element_id)
                    result = await cap.execute_action(
                        Action(name="browser.type", params={"index": index, "text": TEXT})
                    )
                    await asyncio.sleep(0.1)
                    session.cases[label] = Case(result, await _state(cap), index)

                async def into_focus(label: str, clicked: tuple[int, int] | None) -> None:
                    """Type without a point, after a click at *clicked* (``None``: none)."""
                    await _load(cap, site)
                    if clicked is not None:
                        await _click(cap, clicked)
                    result = await cap.execute_action(
                        Action(name="browser.type_coordinate", params={"text": TEXT})
                    )
                    await asyncio.sleep(0.1)
                    session.cases[label] = Case(result, await _state(cap), None)

                await at("empty", FIELDS_EMPTY_POINT)
                await at("empty without a clear", FIELDS_EMPTY_POINT, clear=False)

                await _load(cap, site)
                chord = "Meta+A" if session.platform == "macos" else "Control+A"
                result = await cap.execute_action(
                    Action(name="browser.send_keys", params={"keys": chord})
                )
                session.cases["select all chord"] = Case(
                    result, await _state(cap), FIELDS_EMPTY_POINT
                )

                await at("button", centre("text_button"))
                await by_index("button by index", 1, "text_button")
                await at("read-only", centre("text_readonly"))
                await at("locked", centre("text_locked"))
                await at("component", centre("text_component"))

                await at("input", centre("text_input"))
                await at("textarea", centre("text_area"))
                await at("editable", centre("text_editable"))
                await at("frame", centre("text_frame"))
                await at("frame editor", centre("text_frame_editor"))
                await at("late", centre("text_opener"))
                await at("custom", centre("text_custom"))
                await at("closed", centre("text_closed"))
                await by_index("input by index", 2, "text_input")

                await into_focus("input, no point", centre("text_input"))
                await into_focus("late, no point", centre("text_opener"))
                await into_focus("nothing, no point", None)
                await into_focus("button, no point", centre("text_button"))

                await at("caption", centre("text_last_label"), focused="text_first")
                await at("opener, a field focused", centre("text_opener"), focused="text_first")
                await at("another field", centre("text_last"), focused="text_first")
                await at("the focused field", centre("text_first"), focused="text_first")
                await at("code, another line", centre("text_code_line3"), focused="text_code_line1")
                await at("its own label", centre("text_labelled_label"), focused="text_labelled")
                await at("an icon in its box", centre("text_icon"), focused="text_iconed")
                await at("its label beside it", centre("text_given_label"), focused="text_given")
                await at("another field's label", centre("text_family_label"), focused="text_given")
        return session

    return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    yield _session()


def _refused(case: Case, error: str) -> TextInputEffect:
    """Failed with *error*: no key reached the page, nothing selected, no field changed."""
    assert (case.result.success, case.result.error) == (False, error)
    assert case.result.typed_into_sensitive_field is None
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect), case.result
    assert (effect.keys, effect.text_entered) == ((), False)
    assert case.state["keys"] == [], "a key reached the page"
    assert case.state["selection"] == ""
    assert case.state["fields"] == UNTOUCHED
    return effect


@pytest.mark.parametrize("label", ["empty", "empty without a clear"])
def test_typing_where_no_field_has_focus_fails_and_sends_nothing(
    session: Session, label: str
) -> None:
    effect = _refused(
        session.cases[label],
        "No text field took focus at the clicked point: nothing has focus, so no text was typed.",
    )
    assert (effect.target, effect.focus_after) == ("nothing", "nothing")


def test_the_clears_select_all_there_would_have_selected_the_page(session: Session) -> None:
    case = session.cases["select all chord"]
    assert case.result.success, case.result.error
    assert "Sentence 0 of the page's own text." in case.state["selection"]


def test_a_button_by_coordinate_is_refused_and_named(session: Session) -> None:
    case = session.cases["button"]
    effect = _refused(
        case,
        'No text field took focus at the clicked point: focus is on button "Search", which does '
        "not take typed text, so no text was typed.",
    )
    search = ElementFacts(role="button", name="Search")
    assert (effect.target, effect.focus_after) == (search, search)


def test_a_button_by_index_is_refused_and_named(session: Session) -> None:
    case = session.cases["button by index"]
    effect = _refused(
        case,
        f"Typing into element {case.aim} found no text field with focus: focus is on "
        'button "Search", which does not take typed text, so no text was typed.',
    )
    assert effect.target == ElementFacts(role="button", name="Search")


def test_a_read_only_field_is_refused_and_said_to_be_one(session: Session) -> None:
    effect = _refused(
        session.cases["read-only"],
        'No text field took focus at the clicked point: focus is on textbox "From", which is '
        "read-only, so no text was typed.",
    )
    assert (effect.target, effect.value_changed) == (
        ElementFacts(role="textbox", name="From"),
        False,
    )


@pytest.mark.parametrize(
    ("label", "where"),
    [
        ("input", "input"),
        ("textarea", "area"),
        ("editable", "editable"),
        ("frame", "frame"),
        ("frame editor", "frame_editor"),
        ("late", "late"),
        ("input by index", "input"),
        # Typing without a point right after the click that opens it: the wait covers it.
        ("late, no point", "late"),
    ],
)
def test_a_text_field_takes_the_text(session: Session, label: str, where: str) -> None:
    case = session.cases[label]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert (effect.text_entered, effect.value_changed) == (True, True)
    assert case.state["fields"] == {**UNTOUCHED, where: TEXT}
    assert case.state["selection"] == ""


def test_a_read_only_textbox_widget_is_refused(session: Session) -> None:
    effect = _refused(
        session.cases["locked"],
        'No text field took focus at the clicked point: focus is on textbox "Locked", which is '
        "read-only, so no text was typed.",
    )
    assert effect.target == ElementFacts(role="textbox", name="Locked")


def test_a_component_whose_role_is_no_text_role_is_refused(session: Session) -> None:
    # A light-DOM component (a hyphenated tag with no shadow root): its role
    # says what it is.
    effect = _refused(
        session.cases["component"],
        'No text field took focus at the clicked point: focus is on combobox "City", which '
        "does not take typed text, so no text was typed.",
    )
    assert effect.target == ElementFacts(role="combobox", name="City")


def test_a_field_in_a_closed_shadow_root_is_typed_into(session: Session) -> None:
    # Script sees only the host; the DOM agent sees its closed shadow root, so
    # whether a text field has focus cannot be told and the text is typed.
    case = session.cases["closed"]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert (effect.target, effect.text_entered) == (ElementFacts(role="generic", name=""), True)
    assert case.state["fields"] == {**UNTOUCHED, "closed": TEXT}
    assert case.state["selection"] == ""


def test_a_textbox_widget_gets_the_keys_but_the_page_is_not_selected(session: Session) -> None:
    # A widget with role textbox that is no editor handles keys itself: the
    # clear's chord and Backspace reach it (it empties itself), without
    # Chrome's select-all command, which would select the whole page. Under
    # the instant rhythm the text itself has no editor to go into.
    case = session.cases["custom"]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert effect.target == ElementFacts(role="textbox", name="Custom")
    select_all = "Meta" if session.platform == "macos" else "Control"
    assert effect.keys == (f"{select_all}+A", "Backspace")
    assert case.state["selection"] == ""
    assert case.state["fields"] == {**UNTOUCHED, "custom": ""}
    assert case.state["keys"] == [select_all, "a", "Backspace"]


def test_typing_without_a_point_goes_into_the_field_a_click_focused(session: Session) -> None:
    case = session.cases["input, no point"]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert (effect.target, effect.text_entered, effect.value_changed) == (
        ElementFacts(role="textbox", name="Name"),
        True,
        True,
    )
    assert case.state["fields"] == {**UNTOUCHED, "input": TEXT}
    assert case.state["selection"] == ""


def test_typing_without_a_point_where_nothing_has_focus_fails_and_sends_nothing(
    session: Session,
) -> None:
    effect = _refused(
        session.cases["nothing, no point"],
        "No text field has focus: nothing has focus, so no text was typed.",
    )
    assert (effect.target, effect.focus_after) == ("nothing", "nothing")


def test_typing_without_a_point_into_a_focused_button_is_refused_and_named(
    session: Session,
) -> None:
    effect = _refused(
        session.cases["button, no point"],
        'No text field has focus: focus is on button "Search", which does not take typed text, '
        "so no text was typed.",
    )
    assert effect.target == ElementFacts(role="button", name="Search")


def test_a_press_that_leaves_focus_in_a_filled_field_away_from_it_is_refused(
    session: Session,
) -> None:
    # The caption beside "Last name" keeps focus in "First name", which holds
    # "Grace": refused, nothing cleared or typed.
    first = ElementFacts(role="textbox", name="First name")
    effect = _refused(
        session.cases["caption"],
        'No text field took focus at the clicked point: focus stayed on textbox "First name", '
        "away from the clicked point, so no text was typed.",
    )
    assert (effect.target, effect.focus_after) == (first, first)


@pytest.mark.parametrize(
    ("label", "where"),
    [
        # The button takes focus, then the field it opens, away from the point.
        ("opener, a field focused", "late"),
        ("another field", "last"),
        ("the focused field", "first"),
    ],
)
def test_focus_that_moved_or_a_point_in_the_focused_field_takes_the_text(
    session: Session, label: str, where: str
) -> None:
    case = session.cases[label]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert (effect.text_entered, effect.value_changed) == (True, True)
    assert case.state["fields"] == {**UNTOUCHED, where: TEXT}


@pytest.mark.parametrize(
    ("label", "where"),
    [
        # Focus stays in the hidden one-line textarea; the editor around it is the field.
        ("code, another line", "code"),
        # The press blurs the input, and the label focuses it again.
        ("its own label", "labelled"),
        ("an icon in its box", "iconed"),
        # A label beside its input: their only shared ancestor holds other fields.
        ("its label beside it", "given"),
    ],
)
def test_a_press_that_keeps_focus_inside_the_field_as_drawn_takes_the_text(
    session: Session, label: str, where: str
) -> None:
    case = session.cases[label]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert (effect.text_entered, effect.value_changed) == (True, True)
    assert case.state["fields"] == {**UNTOUCHED, where: TEXT}


def test_a_press_on_another_fields_label_moves_focus_there_and_types(session: Session) -> None:
    # Chrome's label activation focuses that label's own field: focus moved.
    case = session.cases["another field's label"]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert effect.target == ElementFacts(role="textbox", name="Family name")
    assert case.state["fields"] == {**UNTOUCHED, "family": TEXT}
