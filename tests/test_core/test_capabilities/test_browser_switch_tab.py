"""``browser.switch_tab`` actually moves agent focus.

Regression for the "reports success and does nothing" bug: the action
used to emit a ``SwitchTabEvent`` that no watchdog was ever connected
to. agent-cdp events are born-completed, so ``await event`` returned
instantly with an empty result set and the action reported
``success=True`` while the page did not move — the worst failure shape
there is, because the LLM believes it switched and then acts on the
wrong tab for the rest of the run.

Tab focus is capability state (``_default_session_id``), so these tests
assert the focus transition itself, not just the CDP call.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.schemas.action import Action


class _MultiTabCDP:
    """CDP stub with a configurable set of page targets.

    ``Target.attachToTarget`` mints a distinct session id per target so
    the capability's target↔session maps behave like the real thing.
    """

    def __init__(self, target_ids: list[str]) -> None:
        self.is_connected = True
        self.target_ids = list(target_ids)
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.event_callbacks: dict[str, list[Any]] = {}

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {"targetId": t, "type": "page", "url": f"https://{t}.example"}
                    for t in self.target_ids
                ]
            }
        if method == "Target.attachToTarget":
            return {"sessionId": f"sid-{params['targetId']}"}  # type: ignore[index]
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1"}}}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        self.event_callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Any) -> None:
        cbs = self.event_callbacks.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    def activations(self) -> list[str]:
        return [
            p["targetId"] for m, p, _ in self.sent if m == "Target.activateTarget"
        ]


class _Scope:
    def __init__(self, scope_id: str = "scope") -> None:
        self.scope_id = scope_id

    def connect(self, *_a: Any, **_k: Any) -> Any:
        return None

    def emit(self, _event: Any) -> None:
        return None


class _ScopeGroup:
    def __init__(self, _name: str = "test") -> None:
        pass

    async def create_scope(self, scope_id: str, **_k: Any) -> _Scope:
        return _Scope(scope_id)

    async def close_scope(self, _scope_id: str) -> None:
        return None

    async def close_all(self) -> None:
        return None


async def _make_cap(target_ids: list[str], attached: list[str]) -> tuple[
    BrowserCapability, _MultiTabCDP
]:
    """Capability whose browser has ``target_ids`` tabs, ``attached`` adopted."""
    from spectragent.core.capabilities.browser.profiles import resolve_profile

    cap = BrowserCapability()
    cdp = _MultiTabCDP(target_ids)
    cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
    cap._cdp = cdp  # type: ignore[assignment]
    cap._scope_group = _ScopeGroup()  # type: ignore[assignment]
    cap._resolved_profile = resolve_profile("standard")
    cap._create_watchdogs_for_scope = (  # type: ignore[method-assign]
        lambda level="tab", cdp_session_id=None: []
    )
    for tid in attached:
        sid = await cap._attach_tab(tid)
        if cap._default_session_id is None:
            cap._default_session_id = sid
    return cap, cdp


class TestSwitchTabMovesFocus:
    async def test_explicit_target_activates_and_repoints_focus(self) -> None:
        cap, cdp = await _make_cap(["T1", "T2"], attached=["T1", "T2"])
        assert cap._default_session_id == "sid-T1"

        new_sid = await cap.switch_tab("T2")

        assert new_sid == "sid-T2"
        # The focus actually moved — this is what the old code never did.
        assert cap._default_session_id == "sid-T2"
        assert cdp.activations() == ["T2"]

    async def test_none_picks_most_recently_opened_other_tab(self) -> None:
        cap, cdp = await _make_cap(["T1", "T2", "T3"], attached=["T1"])

        new_sid = await cap.switch_tab(None)

        assert new_sid == "sid-T3"
        assert cap._default_session_id == "sid-T3"
        assert cdp.activations() == ["T3"]

    async def test_unadopted_tab_is_attached_on_switch(self) -> None:
        """A tab opened by the page (target=_blank) has no scope yet."""
        cap, _ = await _make_cap(["T1", "T2"], attached=["T1"])
        assert "sid-T2" not in cap._scopes

        await cap.switch_tab("T2")

        assert "sid-T2" in cap._scopes
        assert cap._target_to_session["T2"] == "sid-T2"

    async def test_switch_clears_stale_cache_of_target_tab(self) -> None:
        from spectragent.core.capabilities.browser.capability import (
            _ObserveCycleCache,
        )

        cap, _ = await _make_cap(["T1", "T2"], attached=["T1", "T2"])
        cap._cycle_cache["sid-T2"] = _ObserveCycleCache(fired=True)

        await cap.switch_tab("T2")

        # The agent is about to observe the newly focused tab; a cache
        # from an earlier cycle would describe a stale page.
        assert "sid-T2" not in cap._cycle_cache


class TestSwitchTabFailsLoudly:
    async def test_unknown_target_raises_and_lists_open_tabs(self) -> None:
        cap, _ = await _make_cap(["T1", "T2"], attached=["T1"])

        with pytest.raises(ValueError, match="unknown tab") as exc:
            await cap.switch_tab("does-not-exist")

        assert "T1" in str(exc.value) and "T2" in str(exc.value)
        assert cap._default_session_id == "sid-T1"

    async def test_single_tab_none_target_raises(self) -> None:
        cap, _ = await _make_cap(["T1"], attached=["T1"])

        with pytest.raises(ValueError, match="no other tab is open"):
            await cap.switch_tab(None)


class TestSwitchTabActionContract:
    """End-to-end through ``execute_action`` — what the LLM actually sees."""

    async def test_success_implies_focus_moved(self) -> None:
        cap, cdp = await _make_cap(["T1", "T2"], attached=["T1", "T2"])

        result = await cap.execute_action(
            Action(name="browser.switch_tab", params={"target_id": "T2"})
        )

        assert result.success is True
        # The whole point: success is now load-bearing.
        assert cap._default_session_id == "sid-T2"
        assert cdp.activations() == ["T2"]
        assert "T2" in (result.extracted_content or "")

    async def test_failure_is_reported_not_swallowed(self) -> None:
        cap, cdp = await _make_cap(["T1", "T2"], attached=["T1"])

        result = await cap.execute_action(
            Action(name="browser.switch_tab", params={"target_id": "nope"})
        )

        assert result.success is False
        assert "unknown tab" in (result.error or "")
        assert cap._default_session_id == "sid-T1"
        assert cdp.activations() == []

    async def test_subsequent_action_targets_the_new_tab(self) -> None:
        """Focus must be sticky — and it is the *only* way a tab is chosen.

        Nothing routes a tab to an action any more: ``execute`` takes
        ``(params, *, capability)``, so a later action can only reach T2
        by reading the focus ``switch_tab`` moved.
        """
        cap, _ = await _make_cap(["T1", "T2"], attached=["T1", "T2"])
        await cap.execute_action(
            Action(name="browser.switch_tab", params={"target_id": "T2"})
        )

        seen: list[str] = []

        async def _spy(_params, *, capability):  # noqa: ANN001
            from spectragent.schemas.action import ActionResult

            seen.append(capability.default_session_id)
            return ActionResult(success=True)

        cap._actions_by_name["browser.wait"].execute = _spy  # type: ignore[method-assign]
        await cap.execute_action(Action(name="browser.wait", params={}))

        assert seen == ["sid-T2"]


class TestNoDeadTabEvents:
    """The unhandled tab events are gone, not merely unused."""

    def test_switch_and_close_tab_events_are_deleted(self) -> None:
        from spectragent.core.capabilities.browser.watchdogs import events

        assert not hasattr(events, "SwitchTabEvent")
        assert not hasattr(events, "CloseTabEvent")
