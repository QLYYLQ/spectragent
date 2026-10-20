"""``browser.navigate``, ``go_back`` and ``go_forward`` report a measured ``NavigationEffect``.

Through the real event scope and ``ActionWatchdog`` against a fake tab that
keeps a navigation history the way Chrome reports it
(``docs/architecture_design.md`` §4.5, "动作效果"): the URL before and after
is the current history entry's, ``document_loaded`` is
``document.readyState == "complete"`` once the action's own waits are over.
A history navigation commits after ``Page.navigateToHistoryEntry`` answers,
so its URL after is read once the entry has become current — and is unknown,
never the page being left, when that does not happen within the budget.
Measuring never changes the outcome: a probe that fails or runs out of
``action_effect_timeout_s`` leaves its fact unknown, and a failed action
reports no effect.

The real-Chrome half is ``test_browser_effects_navigation_live.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_cdp import ScopeGroup

from spectragent.core.capabilities.browser.actions.go_back import GoBackAction
from spectragent.core.capabilities.browser.actions.go_forward import GoForwardAction
from spectragent.core.capabilities.browser.actions.navigate import NavigateAction
from spectragent.core.capabilities.browser.effects import Location, NavigationEffect
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from tests.test_core.test_capabilities.test_browser_actions.conftest import FakeCapability

A, B = "https://a.example/", "https://b.example/"


class HistoryTab:
    """A tab's history and document state, answered over a fake CDP connection.

    ``Page.navigate`` commits at once (a URL that differs from the current one
    only by its fragment commits without a loader, as Chrome's same-document
    navigation does). ``Page.navigateToHistoryEntry`` answers first and
    commits after ``commit_after`` more history reads (``None``: never).
    ``fail`` makes a method raise and ``hang`` makes it never answer.
    """

    def __init__(
        self,
        urls: list[str],
        index: int,
        *,
        ready: str = "complete",
        commit_after: int | None = 0,
        fail: frozenset[str] = frozenset(),
        hang: frozenset[str] = frozenset(),
        fail_history_reads: int = 0,
    ) -> None:
        self.urls = list(urls)
        self.index = index
        self.ready = ready
        self.commit_after = commit_after
        self.fail = fail
        self.hang = hang
        self.fail_history_reads = fail_history_reads
        self.pending: int | None = None
        self.reads_until_commit = 0
        self.sent: list[str] = []

    def _history(self) -> dict[str, Any]:
        return {
            "currentIndex": self.index,
            "entries": [{"id": 10 + i, "url": url} for i, url in enumerate(self.urls)],
        }

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        params = params or {}
        self.sent.append(method)
        if method in self.hang:
            await asyncio.Event().wait()
        if method in self.fail:
            raise RuntimeError(f"CDP error: {method} failed")
        if method == "Page.getNavigationHistory":
            if self.fail_history_reads:
                self.fail_history_reads -= 1
                raise RuntimeError("CDP error: history unavailable")
            if self.pending is not None:
                if self.reads_until_commit <= 0:
                    self.index, self.pending = self.pending, None
                else:
                    self.reads_until_commit -= 1
            return self._history()
        if method == "Page.navigate":
            url = params["url"]
            same_document = url.split("#")[0] == self.urls[self.index].split("#")[0] and "#" in url
            self.urls = [*self.urls[: self.index + 1], url]
            self.index += 1
            return {} if same_document else {"frameId": "MAIN", "loaderId": "L1"}
        if method == "Page.navigateToHistoryEntry":
            target = params["entryId"] - 10
            if self.commit_after is not None:
                self.pending, self.reads_until_commit = target, self.commit_after
            return {}
        if method == "Runtime.evaluate" and params.get("expression") == "document.readyState":
            return {"result": {"type": "string", "value": self.ready}}
        return {}


@asynccontextmanager
async def tab(cdp: HistoryTab, *, effect_timeout_s: float = 1.0) -> AsyncIterator[FakeCapability]:
    """A capability whose focused tab is served by a real scope and ``ActionWatchdog``."""
    group = ScopeGroup("navigation-effects")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(
        cdp,  # type: ignore[arg-type]
        input_platform="linux",
        main_frame_id_getter=lambda: "MAIN",
        action_effect_timeout_s=effect_timeout_s,
    )
    watchdog.attach(scope, "sid_main")
    capability = FakeCapability()
    capability._scopes["sid_main"] = scope  # type: ignore[assignment]
    capability._default_session_id = "sid_main"
    try:
        yield capability
    finally:
        await group.close_all()


def _effect(result: Any) -> NavigationEffect:
    assert result.success, result.error
    assert isinstance(result.effect, NavigationEffect)
    return result.effect


class TestNavigate:
    async def test_reports_the_url_before_and_after_and_that_the_document_loaded(self) -> None:
        cdp = HistoryTab([A], 0)
        async with tab(cdp) as capability:
            result = await NavigateAction(wait_until="").execute({"url": B}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert effect.effect_id == "browser.navigate"
        assert (effect.before, effect.after, effect.document_loaded) == (
            Location(url=A),
            Location(url=B),
            True,
        )
        assert effect.describe() == (
            "The URL changed from https://a.example/ to https://b.example/; "
            "the document has finished loading."
        )

    async def test_the_url_reads_from_before_the_navigation_was_sent(self) -> None:
        cdp = HistoryTab([A], 0)
        async with tab(cdp) as capability:
            await NavigateAction(wait_until="").execute({"url": B}, capability=capability)  # type: ignore[arg-type]
        navigate = cdp.sent.index("Page.navigate")
        assert "Page.getNavigationHistory" in cdp.sent[:navigate]
        assert cdp.sent[navigate + 1 :] == ["Page.getNavigationHistory", "Runtime.evaluate"]

    async def test_a_fragment_navigation_includes_the_fragment(self) -> None:
        cdp = HistoryTab([A], 0)
        async with tab(cdp) as capability:
            result = await NavigateAction().execute({"url": A + "#part"}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert effect.after == Location(url="https://a.example/#part")
        assert effect.describe() == (
            "The URL changed from https://a.example/ to https://a.example/#part; "
            "the document has finished loading."
        )

    async def test_navigating_to_the_url_already_shown_says_it_stayed(self) -> None:
        cdp = HistoryTab([A], 0)
        async with tab(cdp) as capability:
            result = await NavigateAction(wait_until="").execute({"url": A}, capability=capability)  # type: ignore[arg-type]
        assert _effect(result).describe() == (
            "The URL stayed https://a.example/; the document has finished loading."
        )

    async def test_a_late_load_succeeds_and_says_the_document_was_still_loading(self) -> None:
        cdp = HistoryTab([A], 0, ready="interactive")
        async with tab(cdp) as capability:
            result = await NavigateAction(lifecycle_timeout_ms=50).execute(
                {"url": B},
                capability=capability,  # type: ignore[arg-type]
            )
        effect = _effect(result)
        assert effect.document_loaded is False
        assert effect.describe() == (
            "The URL changed from https://a.example/ to https://b.example/; "
            "the document was still loading when the action returned."
        )

    async def test_probes_that_fail_leave_the_facts_unknown_and_the_navigation_succeeds(
        self,
    ) -> None:
        cdp = HistoryTab([A], 0, fail=frozenset({"Page.getNavigationHistory", "Runtime.evaluate"}))
        async with tab(cdp) as capability:
            result = await NavigateAction(wait_until="").execute({"url": B}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert (effect.before, effect.after, effect.document_loaded) == (
            "unknown",
            "unknown",
            "unknown",
        )
        assert effect.describe() == (
            "The URL after the action is unknown; whether the document finished loading is unknown."
        )

    async def test_only_the_url_before_unknown(self) -> None:
        cdp = HistoryTab([A], 0, fail_history_reads=1)
        async with tab(cdp) as capability:
            result = await NavigateAction(wait_until="").execute({"url": B}, capability=capability)  # type: ignore[arg-type]
        assert _effect(result).describe() == (
            "The URL is now https://b.example/ (the URL before is unknown); "
            "the document has finished loading."
        )

    async def test_probes_that_never_answer_are_cut_off_by_the_budget(self) -> None:
        cdp = HistoryTab([A], 0, hang=frozenset({"Page.getNavigationHistory", "Runtime.evaluate"}))
        async with tab(cdp, effect_timeout_s=0.05) as capability:
            started = time.monotonic()
            result = await NavigateAction(wait_until="").execute({"url": B}, capability=capability)  # type: ignore[arg-type]
            took = time.monotonic() - started
        effect = _effect(result)
        assert (effect.before, effect.after, effect.document_loaded) == (
            "unknown",
            "unknown",
            "unknown",
        )
        assert took < 0.5, "the three probes share one 50 ms budget"

    async def test_a_failed_navigation_reports_no_effect(self) -> None:
        cdp = HistoryTab([A], 0, fail=frozenset({"Page.navigate"}))
        async with tab(cdp) as capability:
            result = await NavigateAction().execute({"url": B}, capability=capability)  # type: ignore[arg-type]
        assert result.success is False
        assert result.effect is None


class TestHistoryNavigation:
    async def test_go_back_reports_the_entry_it_went_to(self) -> None:
        cdp = HistoryTab([A, B], 1)
        async with tab(cdp) as capability:
            result = await GoBackAction().execute({}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert effect.effect_id == "browser.go_back"
        assert (effect.before, effect.after, effect.document_loaded) == (
            Location(url=B),
            Location(url=A),
            True,
        )
        assert effect.describe() == (
            "The URL changed from https://b.example/ to https://a.example/; "
            "the document has finished loading."
        )

    async def test_go_forward_reports_the_entry_it_went_to(self) -> None:
        cdp = HistoryTab([A, B], 0, ready="loading")
        async with tab(cdp) as capability:
            result = await GoForwardAction().execute({}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert effect.effect_id == "browser.go_forward"
        assert (effect.before, effect.after, effect.document_loaded) == (
            Location(url=A),
            Location(url=B),
            False,
        )
        assert effect.describe() == (
            "The URL changed from https://a.example/ to https://b.example/; "
            "the document was still loading when the action returned."
        )

    async def test_the_url_after_waits_for_the_entry_to_commit(self) -> None:
        """Chrome answers ``navigateToHistoryEntry`` first; the old URL is never the answer."""
        cdp = HistoryTab([A, B], 1, commit_after=3)
        async with tab(cdp) as capability:
            result = await GoBackAction().execute({}, capability=capability)  # type: ignore[arg-type]
        effect = _effect(result)
        assert effect.after == Location(url=A)
        assert cdp.sent.count("Page.getNavigationHistory") == 1 + 4

    async def test_an_entry_that_does_not_commit_within_the_budget_is_unknown(self) -> None:
        cdp = HistoryTab([A, B], 1, commit_after=None)
        async with tab(cdp, effect_timeout_s=0.1) as capability:
            started = time.monotonic()
            result = await GoBackAction().execute({}, capability=capability)  # type: ignore[arg-type]
            took = time.monotonic() - started
        effect = _effect(result)
        assert (effect.before, effect.after, effect.document_loaded) == (
            Location(url=B),
            "unknown",
            "unknown",
        )
        assert "Runtime.evaluate" not in cdp.sent, "the document shown is not the destination's"
        assert took < 0.6
        assert effect.describe() == (
            "The URL after the action is unknown; whether the document finished loading is unknown."
        )

    async def test_no_entry_to_go_to_fails_with_no_effect(self) -> None:
        cdp = HistoryTab([A], 0)
        async with tab(cdp) as capability:
            back = await GoBackAction().execute({}, capability=capability)  # type: ignore[arg-type]
            forward = await GoForwardAction().execute({}, capability=capability)  # type: ignore[arg-type]
        for result in (back, forward):
            assert result.success is False
            assert "no navigation history entry" in (result.error or "")
            assert result.effect is None
        assert "Page.navigateToHistoryEntry" not in cdp.sent
