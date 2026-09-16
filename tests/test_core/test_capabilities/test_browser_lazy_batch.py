"""Lazy-batch resolution + per-cycle cache invalidation tests.

These tests directly manipulate ``BrowserCapability``'s internal state
(``_cycle_cache``, ``_scopes``, ``_default_session_id``) to exercise the
caching behaviour without requiring a real CDP connection. The
``_populate_cycle`` method is monkey-patched to record invocations and
return canned fragments — this lets us verify exactly how many times
the batched event would have fired.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.capability import (
    _ObserveCycleCache,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.schemas.action import Action, ActionResult


@pytest.fixture
def cap_with_fake_session() -> BrowserCapability:
    """A BrowserCapability with a fake session installed.

    Skips ``setup()`` entirely. ``execute_action`` will fail because no
    real scope is attached, but the cache invalidation logic runs
    BEFORE the scope check, so we can test invalidation by inspecting
    ``_cycle_cache`` after each call.
    """
    cap = BrowserCapability()
    cap._default_session_id = "fake_sid"
    # Install a sentinel scope so execute_action's "Unknown session_id"
    # path is not hit. We don't care that it's not a real EventScope —
    # the action will fail at the event-emit stage, but the cache
    # invalidation runs before that.
    cap._scopes["fake_sid"] = object()  # type: ignore[assignment]
    cap._selector_maps["fake_sid"] = {}
    return cap


class TestCacheInvalidation:
    """Verify ``execute_action`` clears the per-tab observe cache."""

    def test_action_clears_cache_for_session(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        cap = cap_with_fake_session
        cache = _ObserveCycleCache(
            fragments={
                "browser.dom": DOMObs(element_tree=None, selector_map={}),
                "browser.screenshot": ScreenshotObs(),
                "browser.browser_meta": BrowserMetaObs(),
            },
            fired=True,
        )
        cap._cycle_cache["fake_sid"] = cache
        assert "fake_sid" in cap._cycle_cache

        # Use a sync helper to drive the async method.
        import asyncio

        async def _go() -> ActionResult:
            return await cap.execute_action(
                Action(name="browser.navigate", params={"url": "https://example.com"}),
            )

        result = asyncio.run(_go())

        # The action will fail because the scope is a fake object,
        # but the cache must already be cleared at this point.
        assert "fake_sid" not in cap._cycle_cache
        # Sanity: the action result reports failure (not the point of
        # the test, but verifies we got past the first guards).
        assert result.success is False

    def test_action_only_clears_target_session(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        cap = cap_with_fake_session
        # Install a second tab's cache that should NOT be invalidated.
        cap._scopes["other_sid"] = object()  # type: ignore[assignment]
        cap._selector_maps["other_sid"] = {}
        other_cache = _ObserveCycleCache(
            fragments={"browser.dom": DOMObs(element_tree=None, selector_map={})},
            fired=True,
        )
        cap._cycle_cache["other_sid"] = other_cache
        cap._cycle_cache["fake_sid"] = _ObserveCycleCache(fired=True)

        import asyncio

        async def _go() -> None:
            await cap.execute_action(
                Action(name="browser.navigate", params={"url": "https://x.example.com"}),
            )

        asyncio.run(_go())

        assert "fake_sid" not in cap._cycle_cache
        # The other tab's cache must be untouched.
        assert "other_sid" in cap._cycle_cache
        assert cap._cycle_cache["other_sid"] is other_cache

    def test_unknown_action_does_not_invalidate_cache(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        # An unknown action name short-circuits before cache
        # invalidation. The page state could not have changed (the CDP
        # call never went out), so leaving the cache intact is correct.
        cap = cap_with_fake_session
        cap._cycle_cache["fake_sid"] = _ObserveCycleCache(fired=True)

        import asyncio

        async def _go() -> ActionResult:
            return await cap.execute_action(
                Action(name="totally_made_up", params={}),
            )

        result = asyncio.run(_go())
        assert result.success is False
        # Cache should still be intact.
        assert "fake_sid" in cap._cycle_cache


class TestLazyBatchPopulation:
    """Verify the cache short-circuits when fragments are already populated."""

    def test_resolve_dom_returns_cached_value(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        cap = cap_with_fake_session
        cached_dom = DOMObs(element_tree=None, selector_map={1: "b"})
        cap._cycle_cache["fake_sid"] = _ObserveCycleCache(
            fragments={
                "browser.dom": cached_dom,
                "browser.screenshot": ScreenshotObs(image_bytes=b"X"),
                "browser.browser_meta": BrowserMetaObs(
                    url="https://cached.example.com"
                ),
            },
            fired=True,  # mark as already populated
        )

        import asyncio

        async def _go() -> tuple:
            d = await cap.resolve_fragment("browser.dom")
            s = await cap.resolve_fragment("browser.screenshot")
            b = await cap.resolve_fragment("browser.browser_meta")
            return d, s, b

        d, s, b = asyncio.run(_go())
        # All three should come from the cache (no event fired because
        # ``fired=True`` short-circuits ``_populate_cycle``).
        cached = cap._cycle_cache["fake_sid"].fragments
        assert d is cached_dom
        assert s is cached["browser.screenshot"]
        assert b is cached["browser.browser_meta"]
        assert b.url == "https://cached.example.com"

    def test_resolve_unknown_fragment_raises(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        cap = cap_with_fake_session
        cap._cycle_cache["fake_sid"] = _ObserveCycleCache(
            fragments={
                "browser.dom": DOMObs(),
                "browser.screenshot": ScreenshotObs(),
                "browser.browser_meta": BrowserMetaObs(),
            },
            fired=True,
        )

        import asyncio

        async def _go() -> object:
            return await cap.resolve_fragment(
                "totally_made_up_fragment"
            )

        with pytest.raises(ValueError, match="cannot resolve fragment"):
            asyncio.run(_go())

    def test_resolve_with_no_session_raises(self) -> None:
        cap = BrowserCapability()
        # No default session and no kwarg.

        import asyncio

        async def _go() -> object:
            return await cap.resolve_fragment("browser.dom")

        with pytest.raises(RuntimeError, match="No active browser session"):
            asyncio.run(_go())


class _ReplayScope:
    """Fake EventScope that answers BrowserStateRequestEvent from a script.

    ``replies`` is one entry per expected emission: either a
    ``BrowserStateSummary`` (handler succeeded) or ``None`` (handler ran
    but produced nothing — exactly what DOMWatchdog / ScreenshotWatchdog
    do when their CDP calls fail, since both swallow and return None).
    The event is born-completed, so ``await event`` returns immediately
    and results are read straight off ``event_results``.
    """

    def __init__(self, replies: list[object]) -> None:
        self._replies = list(replies)
        self.emissions = 0

    def emit(self, event: object) -> None:
        reply = self._replies[self.emissions] if self.emissions < len(self._replies) else None
        self.emissions += 1
        event.record_result(  # type: ignore[attr-defined]
            connection_id=f"conn-{self.emissions}",
            handler_name="FakeWatchdog",
            result=reply,
        )


class TestCachePartialFailure:
    """A cycle missing a requested fragment retries, then raises.

    Pre-fix, ``_populate_cycle`` just returned. ``cache.dom`` stayed
    ``None``, ``resolve_fragment`` handed that ``None`` to the negotiated
    observation model where env fragments are required, and the caller
    saw an opaque pydantic ValidationError about a missing field. The
    "next call retries" the comment promised never happened either:
    ``browser.dom`` is resolved exactly once per step.

    The retry predicate is now per-fragment rather than "did *any*
    watchdog answer", so a cycle where the screenshot lands and the DOM
    scrape loses a race against navigation still re-fires.
    """

    @staticmethod
    def _cap_with_scope(replies: list[object]) -> tuple[BrowserCapability, _ReplayScope]:
        cap = BrowserCapability()
        cap._default_session_id = "fake_sid"
        scope = _ReplayScope(replies)
        cap._scopes["fake_sid"] = scope  # type: ignore[assignment]
        cap._selector_maps["fake_sid"] = {}
        # Narrow to the one fragment under test, the way negotiation does.
        cap.apply_negotiation({"browser.dom"})
        return cap, scope

    def test_retries_and_recovers_on_second_attempt(self) -> None:
        import asyncio

        from spectragent.core.capabilities.browser.watchdogs.events import (
            BrowserStateSummary,
        )

        cap, scope = self._cap_with_scope(
            [
                None,
                BrowserStateSummary(
                    fragments={"browser.dom": DOMObs(element_tree="TREE")}
                ),
            ]
        )

        dom = asyncio.run(cap.resolve_fragment("browser.dom"))

        assert scope.emissions == 2, "a failed cycle must actually re-fire"
        assert dom is not None
        assert dom.element_tree == "TREE"
        assert cap._cycle_cache["fake_sid"].fired is True

    def test_partial_reply_still_retries(self) -> None:
        """A summary that omits the requested fragment is not a success."""
        import asyncio

        from spectragent.core.capabilities.browser.watchdogs.events import (
            BrowserStateSummary,
        )

        cap, scope = self._cap_with_scope(
            [
                # Screenshot landed, DOM did not — the old ``had_success``
                # predicate accepted this and served an empty DOM.
                BrowserStateSummary(
                    fragments={"browser.screenshot": ScreenshotObs(image_bytes=b"PNG")}
                ),
                BrowserStateSummary(
                    fragments={"browser.dom": DOMObs(element_tree="TREE")}
                ),
            ]
        )

        dom = asyncio.run(cap.resolve_fragment("browser.dom"))

        assert scope.emissions == 2
        assert dom.element_tree == "TREE"

    def test_raises_with_diagnostics_when_all_attempts_fail(self) -> None:
        import asyncio

        cap, scope = self._cap_with_scope([None, None])

        # The error names the missing *fragment*, so the message points at
        # the thing that is actually absent.
        with pytest.raises(RuntimeError, match=r"no watchdog produced \['browser.dom'\]"):
            asyncio.run(cap.resolve_fragment("browser.dom"))

        assert scope.emissions == 2
        # Never serve a half-built fragment: the cache stays unfired.
        assert cap._cycle_cache["fake_sid"].fired is False
        assert cap._cycle_cache["fake_sid"].fragments == {}

    def test_failure_never_returns_none_dom(self) -> None:
        """The bug's actual symptom: a None fragment reaching the caller."""
        import asyncio

        cap, _ = self._cap_with_scope([None, None])

        async def _go() -> object:
            try:
                return await cap.resolve_fragment("browser.dom")
            except RuntimeError:
                return "raised"

        assert asyncio.run(_go()) == "raised"


class TestEveryObservationSeesThePageAsItIsNow:
    """A cycle is one observation, not "everything until the next action".

    Recorded failure (UI-TARS on the real reCAPTCHA v2 key): the reply to
    the step after the checkbox click was empty, so that step ran no
    action, and the next step's screenshot was *byte-identical* to the
    previous one — a spinner — although the page's own state log shows
    the image challenge fully open 5.5 s before that capture. The agent
    clicked the checkbox again, which dismissed the challenge. The cache
    had been invalidated only by ``execute_action``; a ParseError step
    never calls it.
    """

    @staticmethod
    def _shot(tag: bytes) -> object:
        from spectragent.core.capabilities.browser.watchdogs.events import (
            BrowserStateSummary,
        )

        return BrowserStateSummary(
            fragments={
                "browser.screenshot": ScreenshotObs(image_bytes=tag),
                "browser.dom": DOMObs(element_tree=tag.decode()),
            }
        )

    def _cap(self, replies: list[object], fragments: set[str]) -> tuple[BrowserCapability, _ReplayScope]:
        cap = BrowserCapability()
        cap._default_session_id = "fake_sid"
        scope = _ReplayScope(replies)
        cap._scopes["fake_sid"] = scope  # type: ignore[assignment]
        cap._selector_maps["fake_sid"] = {}
        cap.apply_negotiation(fragments)
        return cap, scope

    def test_a_second_observation_without_an_action_captures_again(self) -> None:
        import asyncio

        cap, scope = self._cap(
            [self._shot(b"spinner"), self._shot(b"challenge")], {"browser.screenshot"}
        )

        async def _two_observations() -> tuple[object, object]:
            first = await cap.resolve_fragment("browser.screenshot")
            # No execute_action in between: the reply failed to parse.
            second = await cap.resolve_fragment("browser.screenshot")
            return first, second

        first, second = asyncio.run(_two_observations())
        assert scope.emissions == 2
        assert first.image_bytes == b"spinner"  # type: ignore[attr-defined]
        assert second.image_bytes == b"challenge"  # type: ignore[attr-defined]

    def test_one_observation_of_several_fragments_still_costs_one_event(self) -> None:
        import asyncio

        cap, scope = self._cap(
            [self._shot(b"one"), self._shot(b"two")],
            {"browser.screenshot", "browser.dom"},
        )

        async def _observations() -> list[object]:
            got = []
            for _ in range(2):  # what Environment.observe does, twice
                got.append(await cap.resolve_fragment("browser.dom"))
                got.append(await cap.resolve_fragment("browser.screenshot"))
            return got

        dom1, shot1, dom2, shot2 = asyncio.run(_observations())
        assert scope.emissions == 2
        assert (dom1.element_tree, shot1.image_bytes) == ("one", b"one")  # type: ignore[attr-defined]
        assert (dom2.element_tree, shot2.image_bytes) == ("two", b"two")  # type: ignore[attr-defined]

    def test_through_the_environment_two_observes_are_two_captures(self) -> None:
        import asyncio

        from spectragent.core.environments.browser import BrowserEnvironment
        from spectragent.core.negotiation import ObservationNegotiationResult

        cap, scope = self._cap(
            [self._shot(b"spinner"), self._shot(b"challenge")], {"browser.screenshot"}
        )
        env = BrowserEnvironment(capabilities={"browser": cap})
        negotiated = ObservationNegotiationResult(
            active_fragments=frozenset({"browser.screenshot"}),
            env_sourced=frozenset({"browser.screenshot"}),
            loop_sourced=frozenset(),
        )

        async def _observe_twice() -> tuple[object, object]:
            return await env.observe(negotiated), await env.observe(negotiated)

        first, second = asyncio.run(_observe_twice())
        assert scope.emissions == 2
        assert first.screenshot.image_bytes == b"spinner"  # type: ignore[attr-defined]
        assert second.screenshot.image_bytes == b"challenge"  # type: ignore[attr-defined]


class TestMultiTabIsolation:
    """Per-tab caches must not bleed across tabs.

    Which tab is read comes from the capability's own focus, not from a
    caller-supplied handle: ``resolve_fragment`` takes no session
    argument, so switching tabs means moving the focus.
    """

    def test_two_tabs_have_independent_caches(
        self, cap_with_fake_session: BrowserCapability
    ) -> None:
        cap = cap_with_fake_session
        cap._scopes["tab_b"] = object()  # type: ignore[assignment]
        cap._selector_maps["tab_b"] = {}

        # Seed two distinct caches.
        cap._cycle_cache["fake_sid"] = _ObserveCycleCache(
            fragments={"browser.dom": DOMObs(selector_map={1: "x"})},
            fired=True,
        )
        cap._cycle_cache["tab_b"] = _ObserveCycleCache(
            fragments={"browser.dom": DOMObs(selector_map={2: "y"})},
            fired=True,
        )

        import asyncio

        async def _go() -> tuple:
            a = await cap.resolve_fragment("browser.dom")
            cap._default_session_id = "tab_b"  # what switch_tab() does
            b = await cap.resolve_fragment("browser.dom")
            return a, b

        a, b = asyncio.run(_go())
        assert a.selector_map == {1: "x"}
        assert b.selector_map == {2: "y"}
