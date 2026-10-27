"""Keys af sends, in a real Chrome: no browser UI opens and the agent's page stays in front.

On macOS, Chrome matched a key event that carried ``nativeVirtualKeyCode`` and
that the page left unhandled against its own menu, and opened
``chrome://settings/help`` in front of the agent's tab, which stayed a
background page for the rest of the run (``document.hidden``, about 1
animation frame in 600 ms instead of about 38). Enter in a search field whose
form submits natively did it on most sites (architecture §4.13;
``input/keyboard.py``, "No native key code"; the unit side is
``test_browser_input/test_key_events.py``).

Against the local fixture site only (scenario ``keys``), through the
capability's own actions, each case on a freshly loaded page:

- Enter in a text field whose form submits natively: the result page loads
  in the same tab;
- every key ``browser.send_keys`` names, a letter, a digit, punctuation and
  modifier chords, with nothing editable focused, and the keys that opened it
  from a text field too;
- a React-style handler sees ``key`` Enter and ``keyCode`` 13 and handles
  the press itself;
- typing with the ``human`` rhythm (a key event per character, Enter
  included) and clearing a field first;
- typing where a click landed on no field (focus taken elsewhere, as a login
  dialog does) under both rhythms: the action fails and sends no key at all
  (``docs/architecture_design.md`` §4.5, "动作效果"), so no clear lands there.

After each, the browser lists the agent's tab only, the page reads
``document.visibilityState == 'visible'``, and its animation frames run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.input import HumanRhythm, InstantRhythm
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.fixture_site import FixtureSite, keys_point

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 240
#: A foreground page runs about 38 animation frames in 600 ms, a background one 1 (measured).
MIN_FRAMES = 10
#: Every key ``browser.send_keys`` names, a letter, a digit, punctuation, and chords.
NAMED = (
    "enter",
    "tab",
    "space",
    "escape",
    "backspace",
    "delete",
    "arrowup",
    "arrowdown",
    "arrowleft",
    "arrowright",
    "home",
    "end",
    "pageup",
    "pagedown",
)
BODY_KEYS = (*NAMED, "a", "5", "%", "shift+tab", "ctrl+a", "meta+a", "alt+a", "ctrl+c")
#: Keys that opened the browser's page from a focused text field too (measured with the native code).
FIELD_KEYS = ("space", "escape", "pageup", "pagedown", "a", "5")
#: What the agent's tab reports: where it is, whether it is in front, its frames, its listeners.
STATE = (
    "(async () => { let n = 0;"
    " await new Promise(r => { const t = performance.now();"
    " (function k() { n++; if (performance.now() - t < 600) requestAnimationFrame(k); else r(); })();"
    " setTimeout(r, 1500); });"
    " const value = id => { const el = document.getElementById(id); return el ? el.value : null; };"
    " return {path: location.pathname + location.search, visibility: document.visibilityState,"
    " frames: n, keys: window.__keys || null, react: window.__reactEnter || null,"
    " query: value('query'), react_value: value('react'), notes: value('notes')}; })()"
)


@dataclass
class Case:
    """One case: what the actions returned and what the browser and page said afterwards."""

    results: list[ActionResult]
    #: Every page target the browser lists: the agent's tab, and nothing else.
    tabs: list[str]
    state: dict[str, Any]


@dataclass
class Session:
    cases: dict[str, Case] = field(default_factory=dict)


@asynccontextmanager
async def _browser(site: FixtureSite, *, rhythm: Any) -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS)
    cap.typing_rhythm = rhythm
    cap.backend = LocalChromeProvider(headless=True, extra_args=site.chrome_args())  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.browser_meta"}), 60)
        yield cap
    finally:
        await cap.teardown()


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=f"browser.{name}", params=params))


async def _page_tabs(cap: BrowserCapability) -> list[dict[str, Any]]:
    reply = await cap.cdp.send("Target.getTargets", {}, timeout=5)
    return [info for info in reply.get("targetInfos", []) if info.get("type") == "page"]


async def _state(cap: BrowserCapability) -> dict[str, Any]:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": STATE, "returnByValue": True, "awaitPromise": True},
        session_id=cap.default_session_id,
        timeout=10,
    )
    return dict(reply["result"]["value"])


async def _path(cap: BrowserCapability) -> str:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": "location.pathname", "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    return str(reply.get("result", {}).get("value", ""))


async def _case(
    cap: BrowserCapability,
    site: FixtureSite,
    steps: list[tuple[str, dict[str, Any]]],
    *,
    navigates: bool = False,
) -> Case:
    """Load the keys page, run *steps*, then read the tab list and the agent's page.

    *navigates*: the steps submit the form, so wait for the result page first.
    Anything else that opened is closed again and the agent's tab brought back
    to the front, so the next case starts the same way whatever this one did.
    """
    loaded = await _act(cap, "navigate", url=site.url("keys"))
    assert loaded.success, loaded.error
    agent = cap._session_to_target[cap.default_session_id]
    results = [await _act(cap, name, **params) for name, params in steps]
    await asyncio.sleep(0.25)  # a page Chrome opens appears within milliseconds (measured)
    for _ in range(50):
        if not navigates or await _path(cap) == "/keys/result.html":
            break
        await asyncio.sleep(0.1)
    state = await _state(cap)
    tabs = await _page_tabs(cap)
    case = Case(results=results, tabs=[str(info.get("url")) for info in tabs], state=state)
    for info in tabs:
        if info["targetId"] != agent:
            await cap.cdp.send("Target.closeTarget", {"targetId": info["targetId"]}, timeout=5)
    await cap.cdp.send("Target.activateTarget", {"targetId": agent}, timeout=5)
    return case


def _click_into(name: str) -> tuple[str, dict[str, Any]]:
    x, y = keys_point(name)
    return ("click_coordinate", {"coordinate_x": x, "coordinate_y": y})


def _press(keys: str) -> tuple[str, dict[str, Any]]:
    return ("send_keys", {"keys": keys})


def _type(name: str, text: str, *, clear: bool = False) -> tuple[str, dict[str, Any]]:
    x, y = keys_point(name)
    return ("type_coordinate", {"coordinate_x": x, "coordinate_y": y, "text": text, "clear": clear})


#: A point on the page with no field: a click there leaves nothing editable focused.
NOWHERE_POINT = (640, 500)
NOWHERE = (
    "type_coordinate",
    {
        "coordinate_x": NOWHERE_POINT[0],
        "coordinate_y": NOWHERE_POINT[1],
        "text": "x",
        "clear": True,
    },
)


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with FixtureSite() as fixture:
        yield fixture


@pytest.fixture(scope="module")
def session(site: FixtureSite) -> Session:
    async def _all() -> Session:
        result = Session()
        async with _browser(site, rhythm=InstantRhythm()) as cap:
            result.cases["enter submits"] = await _case(
                cap, site, [_type("query", "kettle"), _press("enter")], navigates=True
            )
            for keys in BODY_KEYS:
                result.cases[f"body {keys}"] = await _case(cap, site, [_press(keys)])
            for keys in FIELD_KEYS:
                result.cases[f"field {keys}"] = await _case(
                    cap, site, [_click_into("query"), _press(keys)]
                )
            result.cases["react enter"] = await _case(
                cap, site, [_type("react", "kettle"), _press("enter")]
            )
            result.cases["instant type nowhere"] = await _case(cap, site, [NOWHERE])
        async with _browser(site, rhythm=HumanRhythm(seed=3)) as cap:
            result.cases["human typing"] = await _case(
                cap, site, [_type("notes", "Ab1 !?"), _type("notes", "Kettle, 2 L", clear=True)]
            )
            result.cases["human enter submits"] = await _case(
                cap, site, [_type("query", "blue kettle\n")], navigates=True
            )
            result.cases["human type nowhere"] = await _case(cap, site, [NOWHERE])
        return result

    return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))


def _in_front(case: Case) -> None:
    """Every action succeeded; the agent's tab is the only page, in front, its frames running."""
    assert all(result.success for result in case.results), [r.error for r in case.results]
    _alone_in_front(case)


def _alone_in_front(case: Case) -> None:
    """The agent's tab is the only page, in front, its animation frames running."""
    assert len(case.tabs) == 1, f"another page opened: {case.tabs}"
    assert case.state["visibility"] == "visible", case.state
    assert case.state["frames"] >= MIN_FRAMES, case.state


def test_enter_in_a_field_submits_its_form_in_the_same_tab(session: Session) -> None:
    case = session.cases["enter submits"]
    _in_front(case)
    assert case.state["path"] == "/keys/result.html?query=kettle"


@pytest.mark.parametrize("keys", BODY_KEYS)
def test_a_key_with_nothing_editable_focused_opens_nothing(session: Session, keys: str) -> None:
    case = session.cases[f"body {keys}"]
    _in_front(case)
    downs = [k for k in case.state["keys"] or [] if k["type"] == "keydown"]
    assert downs, "the page saw no keydown"
    assert downs[-1]["key"] and downs[-1]["keyCode"] != 0
    assert downs[-1]["code"] != ""


@pytest.mark.parametrize("keys", FIELD_KEYS)
def test_a_key_in_a_text_field_opens_nothing(session: Session, keys: str) -> None:
    _in_front(session.cases[f"field {keys}"])


def test_a_react_style_handler_sees_a_real_enter(session: Session) -> None:
    case = session.cases["react enter"]
    _in_front(case)
    assert case.state["react"] == {
        "key": "Enter",
        "keyCode": 13,
        "code": "Enter",
        "which": 13,
        "value": "kettle",
    }
    assert case.state["path"] == "/keys/page.html", "the handler kept the page"
    events = [(k["type"], k["key"], k["keyCode"]) for k in case.state["keys"] or []]
    assert ("keydown", "Enter", 13) in events and ("keyup", "Enter", 13) in events, json.dumps(
        events
    )


@pytest.mark.parametrize("label", ["instant type nowhere", "human type nowhere"])
def test_typing_where_no_field_has_focus_sends_no_key(session: Session, label: str) -> None:
    """A click on no field (what a login dialog taking focus left): no clear, no text."""
    case = session.cases[label]
    (result,) = case.results
    x, y = NOWHERE_POINT
    assert (result.success, result.error) == (
        False,
        "No text field took focus at the clicked point: nothing has focus, so no text was typed.",
    )
    assert case.state["keys"] == [], "a key reached the page"
    _alone_in_front(case)


def test_human_typing_and_clearing_open_nothing(session: Session) -> None:
    case = session.cases["human typing"]
    _in_front(case)
    assert case.state["notes"] == "Kettle, 2 L", "the clear removed the first text"
    keys = [k["key"] for k in case.state["keys"] or [] if k["type"] == "keydown"]
    assert "Backspace" in keys and "K" in keys


def test_human_typing_submits_with_its_enter(session: Session) -> None:
    case = session.cases["human enter submits"]
    _in_front(case)
    assert case.state["path"] == "/keys/result.html?query=blue+kettle"
