"""Browser page state has one writing: ``render_browser_state``.

``docs/architecture_design.md`` §4.4, "观测增强按消费方分层". URL, title and
tabs from the relative ``browser_meta`` fragment, in the format BrowserUse
already used, followed by the platform-neutral capture state. Skyvern and
BrowserUse both render it; UI-TARS never does.
"""

from __future__ import annotations

from types import SimpleNamespace

from spectragent.agents.rendering.browser_state import render_browser_state
from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.schemas.observation.capture_state import (
    FailedVisibleResources,
    PointerState,
    SettleState,
)
from spectragent.schemas.observation.failure import FragmentFailure

_TABS = [
    {"tab_id": "T1", "title": "News", "url": "https://news.test/", "is_active": False},
    {"tab_id": "T2", "title": "Article", "url": "https://news.test/a/1", "is_active": True},
]


def _meta() -> BrowserMetaObs:
    return BrowserMetaObs(url="https://news.test/a/1", page_title="Article", tabs=_TABS)


def _capture_state() -> SimpleNamespace:
    return SimpleNamespace(
        settle_state=SettleState(outcome="capped", waited_ms=3000, pending=("network",)),
        foreground_change=None,
        pointer=PointerState(status="unknown"),
        failed_visible_resources=FailedVisibleResources(
            status="measured", count=1, identifiers=("hero.png",)
        ),
    )


def test_url_title_and_tabs_in_the_one_format() -> None:
    assert render_browser_state(SimpleNamespace(browser_meta=_meta()), show_media_state=False) == (
        "URL: https://news.test/a/1\n\n"
        "Title: Article\n\n"
        "Tabs: [T1] News (https://news.test/), [T2] Article (https://news.test/a/1)"
    )


def test_the_capture_state_follows_the_page_state() -> None:
    observation = SimpleNamespace(browser_meta=_meta(), capture_state=_capture_state())
    assert render_browser_state(observation, show_media_state=False) == (
        "URL: https://news.test/a/1\n\n"
        "Title: Article\n\n"
        "Tabs: [T1] News (https://news.test/), [T2] Article (https://news.test/a/1)\n\n"
        "<capture_state>\n"
        "Settle: capped: the wait ended at its limit after 3000 ms; still pending: network.\n"
        "Foreground change: none.\n"
        "Pointer: unknown (no confirmed position).\n"
        "Failed visible resources: 1 failed: hero.png.\n"
        "</capture_state>"
    )


def test_parts_with_nothing_to_say_are_left_out() -> None:
    assert (
        render_browser_state(SimpleNamespace(browser_meta=BrowserMetaObs()), show_media_state=False)
        == ""
    )
    assert render_browser_state(SimpleNamespace(), show_media_state=False) == ""
    only_url = SimpleNamespace(browser_meta=BrowserMetaObs(url="https://x.test/"))
    assert render_browser_state(only_url, show_media_state=False) == "URL: https://x.test/"


def test_a_failed_capture_state_still_follows_the_page_state() -> None:
    failure = FragmentFailure(fragment_id="browser.capture_state", error="RuntimeError")
    text = render_browser_state(
        SimpleNamespace(browser_meta=_meta(), capture_state=failure), show_media_state=False
    )
    assert text.startswith("URL: https://news.test/a/1\n\n")
    assert text.endswith(
        "<capture_state>\n"
        "The capture state could not be measured this step: settle, foreground change, "
        "pointer and failed visible resources are all unknown.\n"
        "</capture_state>"
    )
