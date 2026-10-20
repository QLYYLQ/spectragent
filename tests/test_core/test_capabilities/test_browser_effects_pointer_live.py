"""Pointer and text-input effects, measured in a real headless Chrome.

``docs/architecture_design.md`` §4.5, "动作效果". Through the capability's own
actions, against the local effects site (``tests/_helpers/effects_site.py``):

- a coordinate click on ``Go`` hits the button, whose handler moves focus to
  the ``Name`` field;
- a click by index on ``Hidden target`` after ``window.cover()`` put a
  ``Cookie banner`` over it hits the banner and says it missed the target;
  on ``Go`` it hits the target;
- typing into ``Name`` changes its value, typing the same text again over a
  clear does not, and the field is not sensitive; typing into the password
  field is marked sensitive, and neither the text nor its length appears in
  the effect or its line;
- ``send_keys`` with either select-all chord is measured, and a URL is
  refused with no effect;
- a drag reports its press and release points and the button it pressed on;
- text typed into an unlabelled ``contenteditable`` editor never names it: not
  in the typing's effect, nor in a later click's or key press's.

Every action's measurement stays inside its budget; the probe time per
action is recorded on each case (``spent_s``). Unit counterparts:
``test_browser_actions/test_pointer_effects.py`` and
``test_browser_actions/test_text_input_effects.py``.
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
from spectragent.core.capabilities.browser.effect_probes import ProbeBudget
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    PointerEffect,
    TextInputEffect,
    ViewportPoint,
)
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.effects_site import EffectsSite, centre, effects_site

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
#: What the password field is given; neither it nor its length may be shown.
SECRET = "hunter2-correct-horse"
#: What the editor is given; an editor's text is its value and never its name.
DIARY = "my private diary entry"
GO = ElementFacts(role="button", name="Go")
NAME = ElementFacts(role="textbox", name="Name")
PASSWORD = ElementFacts(role="textbox", name="Password")


@dataclass
class Step:
    result: ActionResult
    #: Probe time the action spent measuring its effect, seconds.
    spent_s: float | None


@dataclass
class Session:
    steps: dict[str, Step] = field(default_factory=dict)
    platform: str = ""
    budget_s: float = 0.0


@asynccontextmanager
async def _browser() -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.dom", "browser.browser_meta"}), 60)
        yield cap
    finally:
        await cap.teardown()


class _Budgets:
    """Records every measurement budget the action watchdog hands out."""

    def __init__(self) -> None:
        self.made: list[ProbeBudget] = []
        self._original = ActionWatchdog._effect_budget

    def __enter__(self) -> _Budgets:
        recorder = self

        def record(watchdog: ActionWatchdog) -> ProbeBudget:
            budget = recorder._original(watchdog)
            recorder.made.append(budget)
            return budget

        ActionWatchdog._effect_budget = record  # type: ignore[method-assign]
        return self

    def __exit__(self, *_exc: object) -> None:
        ActionWatchdog._effect_budget = self._original  # type: ignore[method-assign]


async def _evaluate(cap: BrowserCapability, expression: str) -> Any:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    return (reply.get("result") or {}).get("value")


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
        with effects_site() as site, _Budgets() as budgets:
            async with _browser() as cap:
                session.platform = str(cap._input_platform)
                session.budget_s = cap.action_effect_timeout_s

                async def act(label: str, name: str, **params: Any) -> ActionResult:
                    made = len(budgets.made)
                    result = await cap.execute_action(Action(name=f"browser.{name}", params=params))
                    spent = budgets.made[-1].spent if len(budgets.made) > made else None
                    session.steps[label] = Step(result, spent)
                    return result

                await _load(cap, site, "click.html")
                gx, gy = centre("go")
                await act("click go", "click_coordinate", coordinate_x=gx, coordinate_y=gy)

                await _load(cap, site, "click.html")
                go = await _index_of(cap, 1, "go")
                await act("click go by index", "click", index=go)

                await _load(cap, site, "click.html")
                under = await _index_of(cap, 2, "under")
                await _evaluate(cap, "window.cover()")
                await act("click covered", "click", index=under)

                await _load(cap, site, "click.html")
                nx, ny = centre("name")
                await act(
                    "type name", "type_coordinate", coordinate_x=nx, coordinate_y=ny, text="Ada"
                )
                await act(
                    "type name again",
                    "type_coordinate",
                    coordinate_x=nx,
                    coordinate_y=ny,
                    text="Ada",
                )
                name = await _index_of(cap, 3, "name")
                await act("type name by index", "type", index=name, text="Grace", clear=False)
                await act("select all control", "send_keys", keys="Control+A")
                await act("select all meta", "send_keys", keys="Meta+A")
                await act("send a url", "send_keys", keys="https://example.com/account")
                px, py = centre("pw")
                await act(
                    "type password",
                    "type_coordinate",
                    coordinate_x=px,
                    coordinate_y=py,
                    text=SECRET,
                )
                session.steps["password value"] = Step(
                    ActionResult(
                        success=True,
                        extracted_content=str(
                            await _evaluate(cap, "document.getElementById('pw').value")
                        ),
                    ),
                    None,
                )

                await _load(cap, site, "click.html")
                ux, uy = centre("under")
                await act("drag", "drag", start_x=gx, start_y=gy, end_x=ux, end_y=uy)

                await _load(cap, site, "editor.html")
                ex, ey = centre("editor")
                await act(
                    "type into editor",
                    "type_coordinate",
                    coordinate_x=ex,
                    coordinate_y=ey,
                    text=DIARY,
                )
                await act("click editor", "click_coordinate", coordinate_x=ex, coordinate_y=ey)
                await act("key into editor", "send_keys", keys="End")
                session.steps["editor text"] = Step(
                    ActionResult(
                        success=True,
                        extracted_content=str(
                            await _evaluate(cap, "document.getElementById('editor').innerText")
                        ),
                    ),
                    None,
                )
        return session

    return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))


async def _load(cap: BrowserCapability, site: EffectsSite, page: str) -> None:
    loaded = await cap.execute_action(
        Action(name="browser.navigate", params={"url": site.url(page)})
    )
    assert loaded.success, loaded.error


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    yield _session()


def _pointer(session: Session, label: str) -> PointerEffect:
    step = session.steps[label]
    assert step.result.success, step.result.error
    assert isinstance(step.result.effect, PointerEffect), step.result
    return step.result.effect


def _text(session: Session, label: str) -> TextInputEffect:
    step = session.steps[label]
    assert step.result.success, step.result.error
    assert isinstance(step.result.effect, TextInputEffect), step.result
    return step.result.effect


def test_a_click_hits_the_button_and_its_handler_moves_focus(session: Session) -> None:
    effect = _pointer(session, "click go")
    assert effect.point == ViewportPoint(x=centre("go")[0], y=centre("go")[1])
    assert (effect.hit, effect.on_target, effect.focus_after) == (GO, None, NAME)
    assert effect.describe() == (
        f'Clicked at {effect.point.text()} on button "Go"; focus is now on textbox "Name".'  # type: ignore[union-attr]
    )


def test_a_click_by_index_says_it_hit_its_target(session: Session) -> None:
    effect = _pointer(session, "click go by index")
    assert (effect.hit, effect.on_target) == (GO, True)
    assert effect.describe().endswith(
        'on button "Go", the targeted element; focus is now on textbox "Name".'
    )


def test_a_covered_target_is_missed_and_the_cover_named(session: Session) -> None:
    effect = _pointer(session, "click covered")
    assert (effect.hit, effect.on_target) == (
        ElementFacts(role="generic", name="Cookie banner"),
        False,
    )
    assert 'on generic "Cookie banner", not the targeted element' in effect.describe()


def test_typing_into_a_field_changes_it_and_the_same_text_again_does_not(
    session: Session,
) -> None:
    first = _text(session, "type name")
    assert (first.target, first.sensitive, first.value_changed, first.text_entered) == (
        NAME,
        False,
        True,
        True,
    )
    select_all = "Meta" if session.platform == "macos" else "Control"
    assert first.keys == (f"{select_all}+A", "Backspace")
    assert first.focus_after == NAME
    again = _text(session, "type name again")
    assert again.value_changed is False
    assert session.steps["type name"].result.typed_into_sensitive_field is False


def test_typing_by_index_is_measured_too(session: Session) -> None:
    effect = _text(session, "type name by index")
    assert (effect.target, effect.value_changed, effect.keys, effect.focus_after) == (
        NAME,
        True,
        (),
        NAME,
    )


def test_a_password_field_is_sensitive_and_its_content_never_shown(session: Session) -> None:
    effect = _text(session, "type password")
    assert (effect.target, effect.sensitive, effect.value_changed) == (PASSWORD, True, True)
    assert session.steps["type password"].result.typed_into_sensitive_field is True
    # The text did go in: the page holds it, the effect does not.
    assert session.steps["password value"].result.extracted_content == SECRET
    for shown in (effect.model_dump_json(), effect.describe()):
        assert SECRET not in shown
        assert str(len(SECRET)) not in shown
    assert "(a sensitive field; its content is not shown)" in effect.describe()


@pytest.mark.parametrize(
    ("label", "chord"), [("select all control", "Control+A"), ("select all meta", "Meta+A")]
)
def test_either_select_all_chord_is_pressed_and_measured(
    session: Session, label: str, chord: str
) -> None:
    effect = _text(session, label)
    assert (effect.target, effect.keys, effect.text_entered) == (NAME, (chord,), False)
    assert effect.value_changed is False
    assert effect.describe().startswith(f'Pressed {chord} into textbox "Name"; its value did not')


def test_a_url_in_send_keys_is_refused_without_an_effect(session: Session) -> None:
    result = session.steps["send a url"].result
    assert not result.success
    assert (result.error or "").endswith("To enter text, use type.")
    assert result.effect is None
    assert session.steps["send a url"].spent_s is None, "nothing was measured"


def test_a_drag_reports_where_it_pressed_and_released(session: Session) -> None:
    effect = _pointer(session, "drag")
    assert (effect.point, effect.release_point) == (
        ViewportPoint(x=centre("go")[0], y=centre("go")[1]),
        ViewportPoint(x=centre("under")[0], y=centre("under")[1]),
    )
    assert (effect.hit, effect.on_target) == (GO, None)


def test_every_measurement_stays_inside_its_budget(session: Session) -> None:
    measured = {label: step.spent_s for label, step in session.steps.items() if step.spent_s}
    assert measured, "no action measured anything"
    assert all(spent < session.budget_s for spent in measured.values()), measured


def test_text_typed_into_an_editor_never_names_it(session: Session) -> None:
    """An editor's text is what was typed: it is its value, not a name for it."""
    assert DIARY in str(session.steps["editor text"].result.extracted_content), "it went in"
    typed = _text(session, "type into editor")
    editor = ElementFacts(role="textbox", name="")
    assert (typed.target, typed.value_changed, typed.focus_after) == (editor, True, editor)
    clicked = _pointer(session, "click editor")
    assert clicked.focus_after == editor
    assert isinstance(clicked.hit, ElementFacts) and clicked.hit.name == ""
    pressed = _text(session, "key into editor")
    for effect in (typed, clicked, pressed):
        for shown in (effect.model_dump_json(), effect.describe()):
            assert DIARY not in shown and "Draft" not in shown, shown
