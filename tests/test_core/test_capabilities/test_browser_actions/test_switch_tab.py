"""``browser.switch_tab`` reports a measured ``TabSwitchEffect``.

The action moves the capability's focus itself (no watchdog), so it measures
itself too, through the capability's public control connection and within
``action_effect_timeout_s`` (``docs/architecture_design.md`` §4.5, "动作效果"):
the tab focused before — ``Target.getTargetInfo`` on the focused session
names it — and the tab ``switch_tab`` selected, each with its current history
entry's URL, and whether the newly focused tab's document had finished
loading. The focus transition itself is ``test_browser_switch_tab.py``; the
real-Chrome half is ``test_browser_effects_navigation_live.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent_cdp import ScopeGroup

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.effects import Location, TabFacts, TabSwitchEffect
from spectragent.core.capabilities.browser.profiles import resolve_profile
from spectragent.schemas.action import Action, ActionResult


class TabsCDP:
    """A browser with page targets ``T1``, ``T2``…; tab ``Tn`` shows ``https://tn.example/``."""

    def __init__(
        self,
        target_ids: list[str],
        *,
        ready: str = "complete",
        fail: frozenset[str] = frozenset(),
        hang: frozenset[str] = frozenset(),
    ) -> None:
        self.is_connected = True
        self.target_ids = list(target_ids)
        self.ready = ready
        self.fail = fail
        self.hang = hang
        self.sent: list[tuple[str, str | None]] = []

    @staticmethod
    def url(target_id: str) -> str:
        return f"https://{target_id.lower()}.example/"

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        params = params or {}
        self.sent.append((method, session_id))
        target = session_id.removeprefix("sid-") if session_id else None
        if method in self.hang:
            await asyncio.Event().wait()
        if method in self.fail:
            raise RuntimeError(f"CDP error: {method} failed")
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {"targetId": t, "type": "page", "url": self.url(t)} for t in self.target_ids
                ]
            }
        if method == "Target.attachToTarget":
            return {"sessionId": f"sid-{params['targetId']}"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": f"frame-{target}"}}}
        if method == "Target.getTargetInfo" and target is not None:
            return {"targetInfo": {"targetId": target, "type": "page", "url": self.url(target)}}
        if method == "Page.getNavigationHistory" and target is not None:
            return {"currentIndex": 0, "entries": [{"id": 1, "url": self.url(target)}]}
        if method == "Runtime.evaluate" and params.get("expression") == "document.readyState":
            return {"result": {"type": "string", "value": self.ready}}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None


async def _capability(cdp: TabsCDP, *, effect_timeout_s: float = 1.0) -> BrowserCapability:
    """A capability focused on ``T1`` of *cdp*'s tabs, all of them adopted."""
    cap = BrowserCapability(action_effect_timeout_s=effect_timeout_s)
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    cap._cdp = cdp  # type: ignore[assignment]
    cap._scope_group = ScopeGroup("switch-tab-effects")  # type: ignore[assignment]
    cap._resolved_profile = resolve_profile("standard")
    cap._create_watchdogs_for_scope = (  # type: ignore[method-assign]
        lambda level="tab", cdp_session_id=None: []
    )
    for target_id in cdp.target_ids:
        sid = await cap._attach_tab(target_id)
        if cap._default_session_id is None:
            cap._default_session_id = sid
    return cap


async def _switch(cap: BrowserCapability, target_id: str | None) -> ActionResult:
    return await cap.execute_action(
        Action(name="browser.switch_tab", params={"target_id": target_id})
    )


def _effect(result: ActionResult) -> TabSwitchEffect:
    assert result.success, result.error
    assert isinstance(result.effect, TabSwitchEffect)
    return result.effect


class TestTabSwitchEffect:
    async def test_reports_the_tab_before_and_after_and_that_its_document_loaded(self) -> None:
        cap = await _capability(TabsCDP(["T1", "T2"]))
        effect = _effect(await _switch(cap, "T2"))
        assert effect.effect_id == "browser.switch_tab"
        assert (effect.before, effect.after, effect.document_loaded) == (
            TabFacts(tab_id="T1", location=Location(url="https://t1.example/")),
            TabFacts(tab_id="T2", location=Location(url="https://t2.example/")),
            True,
        )
        assert effect.describe() == (
            "Switched from tab T1 to tab T2 (https://t2.example/); "
            "the document has finished loading."
        )

    async def test_the_most_recent_tab_is_named_by_the_tab_it_selected(self) -> None:
        cap = await _capability(TabsCDP(["T1", "T2", "T3"]))
        effect = _effect(await _switch(cap, None))
        assert effect.after == TabFacts(tab_id="T3", location=Location(url="https://t3.example/"))

    async def test_a_tab_still_loading_says_so(self) -> None:
        cap = await _capability(TabsCDP(["T1", "T2"], ready="interactive"))
        effect = _effect(await _switch(cap, "T2"))
        assert effect.document_loaded is False
        assert effect.describe().endswith(
            "; the document was still loading when the action returned."
        )

    async def test_an_unnamed_tab_before_is_unknown_and_the_rest_is_measured(self) -> None:
        cap = await _capability(TabsCDP(["T1", "T2"], fail=frozenset({"Target.getTargetInfo"})))
        result = await _switch(cap, "T2")
        effect = _effect(result)
        assert (effect.before, effect.after) == (
            "unknown",
            TabFacts(tab_id="T2", location=Location(url="https://t2.example/")),
        )
        assert effect.describe() == (
            "Switched from a tab that is unknown to tab T2 (https://t2.example/); "
            "the document has finished loading."
        )

    async def test_probes_that_never_answer_leave_unknowns_and_the_focus_still_moves(
        self,
    ) -> None:
        cdp = TabsCDP(
            ["T1", "T2"],
            hang=frozenset(
                {"Target.getTargetInfo", "Page.getNavigationHistory", "Runtime.evaluate"}
            ),
        )
        cap = await _capability(cdp, effect_timeout_s=0.05)
        started = time.monotonic()
        result = await _switch(cap, "T2")
        took = time.monotonic() - started
        effect = _effect(result)
        assert cap.default_session_id == "sid-T2"
        # The tab switched to is known from the switch itself; only its URL is not.
        assert (effect.before, effect.after, effect.document_loaded) == (
            "unknown",
            TabFacts(tab_id="T2", location="unknown"),
            "unknown",
        )
        assert took < 0.5, "every probe shares one 50 ms budget"
        assert effect.describe() == (
            "Switched from a tab that is unknown to tab T2 (its URL is unknown); "
            "whether the document finished loading is unknown."
        )

    async def test_a_failed_switch_reports_no_effect(self) -> None:
        cap = await _capability(TabsCDP(["T1", "T2"]))
        result = await _switch(cap, "nope")
        assert result.success is False
        assert result.effect is None
        assert cap.default_session_id == "sid-T1"
