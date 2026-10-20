"""Navigation, tab, selection and wait effects measured in a real Chrome (§4.5, "动作效果").

Against the local effects site only (``tests/_helpers/effects_site.py``),
through the capability's own actions in one headless Chrome:

- ``navigate`` to ``a.html`` and on to ``b.html``: the URL before and after,
  and the document finished loading; a fragment navigation keeps the
  fragment; a navigation to a port nothing listens on fails with no effect;
- ``go_back`` / ``go_forward`` between ``a.html`` and ``b.html``: the URL
  after is the history entry's once it committed, never the page left;
- ``switch_tab`` to the tab ``opener.html``'s ``target=_blank`` link opened
  (``new_tabs: stay``, so the switch is the agent's own), then back: the tab
  ids are the CDP target ids ``browser_meta`` lists;
- ``select_dropdown`` "Banana" (option 1, changed), the same again (not
  changed), and an option of the same ``<select>`` once the page made it a
  payment field (its text withheld);
- ``wait`` 0.3 s: at least 300 ms, by the monotonic clock.

The fake-CDP half is ``test_browser_actions/test_navigation_effects.py``,
``test_switch_tab.py``, ``test_select_dropdown.py`` and ``test_wait.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import (
    Location,
    NavigationEffect,
    OptionFacts,
    SelectionEffect,
    TabFacts,
    TabSwitchEffect,
    WaitEffect,
)
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.effects_site import EffectsSite, centre, effects_site

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 120


@dataclass
class Session:
    results: dict[str, ActionResult] = field(default_factory=dict)
    #: The CDP target ids of the tab that opened ``second.html`` and of ``second.html``'s tab.
    tabs: dict[str, str] = field(default_factory=dict)


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=f"browser.{name}", params=params))


async def _page_targets(cap: BrowserCapability) -> list[dict[str, Any]]:
    reply = await cap.cdp.send("Target.getTargets", {}, timeout=5)
    return [info for info in reply.get("targetInfos", []) if info.get("type") == "page"]


async def _fruit_index(cap: BrowserCapability) -> int:
    """The selector-map index of ``click.html``'s ``<select id=fruit>``, freshly observed."""
    cap._cycle_cache.pop(cap.default_session_id, None)
    fragment = await cap.resolve_fragment("browser.dom")
    return next(
        key
        for key, node in fragment.element_tree.selector_map.items()
        if node.attributes.get("id") == "fruit"
    )


async def _run(site: EffectsSite) -> Session:
    session = Session()
    results = session.results
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS, new_tabs="stay")
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(), 60)
        results["navigate a"] = await _act(cap, "navigate", url=site.url("a.html"))
        results["navigate b"] = await _act(cap, "navigate", url=site.url("b.html"))
        results["back"] = await _act(cap, "go_back")
        results["forward"] = await _act(cap, "go_forward")
        results["fragment"] = await _act(cap, "navigate", url=site.url("b.html") + "#part")
        results["unreachable"] = await _act(cap, "navigate", url="http://127.0.0.1:9/")

        assert (await _act(cap, "navigate", url=site.url("opener.html"))).success
        session.tabs["opener"] = cap._session_to_target[cap.default_session_id]
        x, y = centre("newtab")
        assert (await _act(cap, "click_coordinate", coordinate_x=x, coordinate_y=y)).success
        for _ in range(50):
            opened = [
                t["targetId"]
                for t in await _page_targets(cap)
                if str(t.get("url", "")).endswith("/second.html")
            ]
            if opened:
                session.tabs["second"] = opened[0]
                break
            await asyncio.sleep(0.1)
        results["switch"] = await _act(cap, "switch_tab", target_id=None)
        results["switch back"] = await _act(cap, "switch_tab", target_id=session.tabs["opener"])

        assert (await _act(cap, "navigate", url=site.url("click.html"))).success
        fruit = await _fruit_index(cap)
        results["select banana"] = await _act(cap, "select_dropdown", index=fruit, text="Banana")
        results["select banana again"] = await _act(
            cap, "select_dropdown", index=fruit, text="Banana"
        )
        await cap.cdp.send(
            "Runtime.evaluate",
            {
                "expression": "document.getElementById('fruit')"
                ".setAttribute('autocomplete', 'cc-exp-month')"
            },
            session_id=cap.default_session_id,
            timeout=5,
        )
        fruit = await _fruit_index(cap)
        results["select sensitive"] = await _act(
            cap, "select_dropdown", index=fruit, option_index=2
        )
        results["wait"] = await _act(cap, "wait", seconds=0.3)
    finally:
        await cap.teardown()
    return session


@pytest.fixture(scope="module")
def site() -> Iterator[EffectsSite]:
    with effects_site() as running:
        yield running


@pytest.fixture(scope="module")
def session(site: EffectsSite) -> Session:
    return asyncio.run(asyncio.wait_for(_run(site), _TIMEOUT_S))


def _navigation(result: ActionResult) -> NavigationEffect:
    assert result.success, result.error
    assert isinstance(result.effect, NavigationEffect)
    return result.effect


def test_navigate_reports_the_url_change_and_a_loaded_document(
    session: Session, site: EffectsSite
) -> None:
    first = _navigation(session.results["navigate a"])
    assert first.effect_id == "browser.navigate"
    assert first.before == Location(url="about:blank")
    assert first.after == Location(url=site.url("a.html"))
    assert first.document_loaded is True
    effect = _navigation(session.results["navigate b"])
    assert (effect.before, effect.after, effect.document_loaded) == (
        Location(url=site.url("a.html")),
        Location(url=site.url("b.html")),
        True,
    )
    assert effect.describe() == (
        f"The URL changed from {site.url('a.html')} to {site.url('b.html')}; "
        "the document has finished loading."
    )


def test_go_back_and_go_forward_report_the_entry_they_went_to(
    session: Session, site: EffectsSite
) -> None:
    back = _navigation(session.results["back"])
    assert back.effect_id == "browser.go_back"
    assert (back.before, back.after) == (
        Location(url=site.url("b.html")),
        Location(url=site.url("a.html")),
    )
    assert back.document_loaded in (True, False), "measured, not unknown"
    forward = _navigation(session.results["forward"])
    assert forward.effect_id == "browser.go_forward"
    assert (forward.before, forward.after) == (
        Location(url=site.url("a.html")),
        Location(url=site.url("b.html")),
    )
    assert forward.document_loaded in (True, False)


def test_a_fragment_navigation_keeps_the_fragment(session: Session, site: EffectsSite) -> None:
    effect = _navigation(session.results["fragment"])
    assert effect.after == Location(url=site.url("b.html") + "#part")
    assert effect.document_loaded is True


def test_a_failed_navigation_reports_no_effect(session: Session) -> None:
    result = session.results["unreachable"]
    assert result.success is False
    assert result.effect is None


def test_switch_tab_reports_both_tabs_by_the_ids_browser_meta_lists(
    session: Session, site: EffectsSite
) -> None:
    opener, second = session.tabs["opener"], session.tabs["second"]
    result = session.results["switch"]
    assert result.success, result.error
    effect = result.effect
    assert isinstance(effect, TabSwitchEffect)
    assert effect.effect_id == "browser.switch_tab"
    assert effect.before == TabFacts(tab_id=opener, url=site.url("opener.html"))
    assert effect.after == TabFacts(tab_id=second, url=site.url("second.html"))
    assert effect.document_loaded in (True, False), "measured, not unknown"
    back = session.results["switch back"]
    assert isinstance(back.effect, TabSwitchEffect)
    assert (back.effect.before, back.effect.after, back.effect.document_loaded) == (
        TabFacts(tab_id=second, url=site.url("second.html")),
        TabFacts(tab_id=opener, url=site.url("opener.html")),
        True,
    )
    assert back.effect.describe() == (
        f"Switched from tab {second} to tab {opener} ({site.url('opener.html')}); "
        "the document has finished loading."
    )


def test_select_reports_the_option_and_whether_the_selection_changed(session: Session) -> None:
    changed = session.results["select banana"]
    assert changed.success, changed.error
    assert isinstance(changed.effect, SelectionEffect)
    assert changed.effect.effect_id == "browser.select_dropdown"
    assert (changed.effect.selected, changed.effect.value_changed) == (
        OptionFacts(index=1, text="Banana"),
        True,
    )
    again = session.results["select banana again"]
    assert isinstance(again.effect, SelectionEffect)
    assert (again.effect.selected, again.effect.value_changed) == (
        OptionFacts(index=1, text="Banana"),
        False,
    )
    assert again.effect.describe() == (
        'The dropdown now shows option 1 "Banana"; the selection did not change.'
    )


def test_a_sensitive_select_withholds_the_option_text(session: Session) -> None:
    result = session.results["select sensitive"]
    assert result.success, result.error
    assert isinstance(result.effect, SelectionEffect)
    assert result.effect.selected == OptionFacts(index=2, text=None)
    assert result.effect.describe() == (
        "The dropdown now shows option 2 (text withheld: a sensitive field); the selection changed."
    )


def test_wait_reports_how_long_it_waited(session: Session) -> None:
    result = session.results["wait"]
    assert result.success, result.error
    assert isinstance(result.effect, WaitEffect)
    assert 300 <= result.effect.waited_ms < 2000
    assert result.effect.describe() == f"Waited {result.effect.waited_ms} ms."
