"""Following a new tab and leaving a closed one: the pure rules (``tabs.py``, §4.14).

``TabRegistry`` keeps every tab in the order it opened, with the tab that
opened it and whether Chrome said the window was opened with the user's
activation (``Page.windowOpen.userGesture``). ``tabs_to_follow`` picks the tab
the observation after the agent's actions shows; ``tabs_after_close`` where
the focus goes when the focused tab closed itself. No CDP here: the
capability feeds the registry and moves the focus
(``test_browser_follow_tab.py``).
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.capabilities.browser.tabs import (
    WINDOW_OPEN_MATCH_S,
    FocusMove,
    FollowWindow,
    TabRegistry,
    tabs_after_close,
    tabs_to_follow,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _info(target_id: str, *, opener: str | None = None, url: str = "", **extra: Any) -> dict:
    info: dict[str, Any] = {"targetId": target_id, "type": "page", "url": url, **extra}
    if opener is not None:
        info["openerId"] = opener
    return info


def _registry(*tabs: str) -> tuple[TabRegistry, _Clock]:
    clock = _Clock()
    registry = TabRegistry(clock=clock)
    for target_id in tabs:
        registry.created(_info(target_id))
    return registry, clock


def _open(
    registry: TabRegistry, target_id: str, opener: str, *, gesture: bool | None = True
) -> None:
    """The way Chrome reports a window a page opens: ``Page.windowOpen``, then ``targetCreated``."""
    if gesture is not None:
        registry.window_opened(opener, gesture)
    registry.created(_info(target_id, opener=opener))


def _ids(tabs: list[Any]) -> list[str]:
    return [tab.target_id for tab in tabs]


class TestRegistry:
    def test_tabs_keep_the_order_they_opened(self) -> None:
        registry, _ = _registry("A", "B", "C")
        assert _ids(registry.open_tabs()) == ["A", "B", "C"]
        assert registry.in_opening_order(["C", "X", "A", "B"]) == ["A", "B", "C", "X"]

    def test_only_tabs_are_registered(self) -> None:
        registry, _ = _registry()
        registry.created({"targetId": "W", "type": "worker", "url": ""})
        registry.created(_info("P", subtype="prerender"))
        registry.created({"type": "page"})
        assert registry.open_tabs() == []

    def test_url_and_closing_follow_the_events(self) -> None:
        registry, _ = _registry("A")
        registry.changed(_info("A", url="https://shop.test/"))
        tab = registry.get("A")
        assert tab is not None and tab.url == "https://shop.test/"
        registry.destroyed("A")
        assert registry.is_closed("A") and registry.open_tabs() == []
        assert not registry.is_closed("unknown"), "an unknown tab is not known to be closed"

    def test_a_tab_list_adds_what_no_event_announced_and_closes_what_it_leaves_out(self) -> None:
        registry, _ = _registry("A", "B")
        registry.reconcile([_info("B"), _info("C"), {"targetId": "W", "type": "worker"}])
        assert registry.is_closed("A")
        assert _ids(registry.open_tabs()) == ["B", "C"], (
            "an unannounced tab goes after every known one"
        )

    def test_a_window_open_gives_its_activation_to_the_next_tab_its_opener_opens(self) -> None:
        registry, _ = _registry("A")
        _open(registry, "B", "A", gesture=True)
        _open(registry, "C", "A", gesture=False)
        assert [registry.get(t).user_activation for t in ("B", "C")] == [True, False]  # type: ignore[union-attr]

    def test_a_tab_no_window_open_announced_has_unknown_activation(self) -> None:
        """A link inside a cross-site iframe: its windowOpen goes to a session nobody listens on."""
        registry, _ = _registry("A")
        _open(registry, "B", "A", gesture=None)
        tab = registry.get("B")
        assert tab is not None and tab.user_activation is None

    def test_a_window_open_of_another_tab_is_not_matched(self) -> None:
        registry, _ = _registry("A", "Z")
        registry.window_opened("Z", False)
        _open(registry, "B", "A", gesture=None)
        tab = registry.get("B")
        assert tab is not None and tab.user_activation is None

    def test_a_window_open_whose_tab_never_came_is_not_matched_later(self) -> None:
        registry, clock = _registry("A")
        registry.window_opened("A", False)
        clock.now += WINDOW_OPEN_MATCH_S + 0.5
        _open(registry, "B", "A", gesture=None)
        tab = registry.get("B")
        assert tab is not None and tab.user_activation is None


class TestTabsToFollow:
    def test_the_newest_tab_the_focused_tab_opened_comes_first(self) -> None:
        registry, _ = _registry("A")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "B", "A")
        _open(registry, "C", "A")
        assert _ids(tabs_to_follow(registry, window)) == ["C", "B"]

    def test_a_tab_opened_before_the_window_is_not_followed(self) -> None:
        """A tab the page opened on its own while the agent was thinking (a timer, an ad)."""
        registry, _ = _registry("A")
        _open(registry, "B", "A")
        window = FollowWindow(focused="A", mark=registry.mark)
        assert tabs_to_follow(registry, window) == []

    def test_a_tab_another_tab_opened_is_not_followed(self) -> None:
        registry, _ = _registry("A", "Z")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "B", "Z")
        registry.created(_info("C"))  # no opener at all: a tab the capability created
        assert tabs_to_follow(registry, window) == []

    def test_a_tab_that_closed_again_is_not_followed(self) -> None:
        """A download's tab: Chrome closes it about 75 ms after it opened."""
        registry, _ = _registry("A")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "B", "A")
        registry.destroyed("B")
        assert tabs_to_follow(registry, window) == []

    def test_a_tab_opened_without_the_users_activation_is_not_followed(self) -> None:
        """One click opened two windows: a real browser's popup blocker lets only the first through."""
        registry, _ = _registry("A")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "first", "A", gesture=True)
        _open(registry, "second", "A", gesture=False)
        assert _ids(tabs_to_follow(registry, window)) == ["first"]

    def test_unknown_activation_is_followed(self) -> None:
        registry, _ = _registry("A")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "B", "A", gesture=None)
        assert _ids(tabs_to_follow(registry, window)) == ["B"]

    def test_tabs_already_given_up_on_are_skipped(self) -> None:
        registry, _ = _registry("A")
        window = FollowWindow(focused="A", mark=registry.mark)
        _open(registry, "B", "A")
        _open(registry, "C", "A")
        assert _ids(tabs_to_follow(registry, window, exclude={"C"})) == ["B"]


class TestTabsAfterClose:
    def test_the_tab_that_opened_it_first(self) -> None:
        registry, _ = _registry("A", "Z")
        _open(registry, "B", "A")
        registry.destroyed("B")
        assert _ids(tabs_after_close(registry, "B")) == ["A", "Z"]

    def test_else_the_most_recently_opened_tab_still_open(self) -> None:
        registry, _ = _registry("A", "Z", "Y")
        _open(registry, "B", "A")
        registry.destroyed("A")
        registry.destroyed("B")
        assert _ids(tabs_after_close(registry, "B")) == ["Y", "Z"]

    def test_nowhere_when_no_tab_is_open(self) -> None:
        registry, _ = _registry("A")
        registry.destroyed("A")
        assert tabs_after_close(registry, "A") == []


class TestTheRecord:
    def test_a_move_is_written_and_read_back(self) -> None:
        move = FocusMove(reason="new_tab", from_tab="A", to_tab="B")
        meta = BrowserMetaObs(url="https://shop.test/b", focus_moved=move)
        payload = meta.model_dump(mode="json", exclude=meta.trace_json_exclude())
        assert payload["focus_moved"] == {"reason": "new_tab", "from_tab": "A", "to_tab": "B"}
        assert BrowserMetaObs.from_trace(payload, None).focus_moved == move

    def test_no_move_writes_what_was_always_written(self) -> None:
        shot = ScreenshotObs(image_bytes=b"\x89PNG", viewport_width=1280, viewport_height=720)
        assert "focus_moved" in shot.trace_json_exclude()
        assert "focus_moved" not in shot.model_dump(mode="json", exclude=shot.trace_json_exclude())
