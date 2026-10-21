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
- a button, by coordinate and by index, and a read-only field: refused the
  same way, naming the element;
- a text field, a textarea, a ``contenteditable``, a text field in a same-site
  ``<iframe>``, an ``<iframe>`` whose body is the editor, a field a widget
  focuses a moment after the click, a ``div`` with role ``textbox``, and a text
  field by index: typed into as before.

Unit counterpart: ``test_browser_actions/test_text_input_effects.py``
(``TestTypingNeedsATextField``).
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
    },
  };
})()"""


@dataclass
class Case:
    result: ActionResult
    state: dict[str, Any]
    #: Where a coordinate case typed, or the index an index case typed into.
    aim: tuple[int, int] | int


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


def _session() -> Session:
    async def _all() -> Session:
        session = Session()
        with effects_site() as site:
            async with _browser() as cap:
                session.platform = str(cap._input_platform)

                async def at(
                    label: str, point: tuple[int, int], *, clear: bool | None = None
                ) -> None:
                    await _load(cap, site)
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

                await at("input", centre("text_input"))
                await at("textarea", centre("text_area"))
                await at("editable", centre("text_editable"))
                await at("frame", centre("text_frame"))
                await at("frame editor", centre("text_frame_editor"))
                await at("late", centre("text_opener"))
                await at("custom", centre("text_custom"))
                await by_index("input by index", 2, "text_input")
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
    x, y = FIELDS_EMPTY_POINT
    effect = _refused(
        session.cases[label],
        f"No text field took focus at ({x}, {y}): nothing has focus, so no text was typed.",
    )
    assert (effect.target, effect.focus_after) == ("nothing", "nothing")


def test_the_clears_select_all_there_would_have_selected_the_page(session: Session) -> None:
    case = session.cases["select all chord"]
    assert case.result.success, case.result.error
    assert "Sentence 0 of the page's own text." in case.state["selection"]


def test_a_button_by_coordinate_is_refused_and_named(session: Session) -> None:
    case = session.cases["button"]
    x, y = centre("text_button")
    effect = _refused(
        case,
        f'No text field took focus at ({x}, {y}): focus is on button "Search", which does '
        "not take typed text, so no text was typed.",
    )
    search = ElementFacts(role="button", name="Search")
    assert (effect.target, effect.focus_after) == (search, search)


def test_a_button_by_index_is_refused_and_named(session: Session) -> None:
    case = session.cases["button by index"]
    effect = _refused(
        case,
        f'Element {case.aim} is not a text field: focus is on button "Search", which does '
        "not take typed text, so no text was typed.",
    )
    assert effect.target == ElementFacts(role="button", name="Search")


def test_a_read_only_field_is_refused_and_said_to_be_one(session: Session) -> None:
    x, y = centre("text_readonly")
    effect = _refused(
        session.cases["read-only"],
        f'No text field took focus at ({x}, {y}): focus is on textbox "From", which is '
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


def test_an_element_that_says_it_is_a_textbox_is_typed_into(session: Session) -> None:
    # A widget with role textbox handles keys itself; the action does not
    # second-guess it. (Under the instant rhythm its text has no editor to go
    # into, which the effect reports.)
    case = session.cases["custom"]
    assert case.result.success, case.result.error
    effect = case.result.effect
    assert isinstance(effect, TextInputEffect)
    assert effect.target == ElementFacts(role="textbox", name="Custom")
