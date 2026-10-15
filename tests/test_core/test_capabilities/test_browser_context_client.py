"""A context view preserves CDP callback delivery while restricting its surface."""

import asyncio

import pytest

from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.context_client import BrowserContextClient


async def test_async_callbacks_are_awaited_and_failure_does_not_drop_later_callbacks():
    raw = CDPClient("ws://unused")
    view = BrowserContextClient(raw, "owned")
    seen = []

    async def first(_params, _sid):
        await asyncio.sleep(0)
        seen.append("async")

    def broken(_params, _sid):
        seen.append("broken")
        raise ValueError("one observer failed")

    def last(_params, _sid):
        seen.append("last")

    view.on_event("Page.loadEventFired", first)
    view.on_event("Page.loadEventFired", broken)
    view.on_event("Page.loadEventFired", last)
    await raw._dispatch_event(
        "Target.attachedToTarget",
        {"sessionId": "sid", "targetInfo": {"targetId": "tab", "browserContextId": "owned"}},
        None,
    )
    await raw._dispatch_event("Page.loadEventFired", {}, "sid")
    assert seen == ["async", "broken", "last"]
    view.off_event("Page.loadEventFired", first)
    await raw._dispatch_event("Page.loadEventFired", {}, "sid")
    assert seen == ["async", "broken", "last", "broken", "last"]


async def test_foreign_events_and_retired_sessions_do_not_reach_callbacks():
    raw = CDPClient("ws://unused")
    view = BrowserContextClient(raw, "owned")
    seen = []
    view.on_event("Page.loadEventFired", lambda params, sid: seen.append(sid))
    for context, sid in (("owned", "ours"), ("foreign", "theirs")):
        await raw._dispatch_event(
            "Target.attachedToTarget",
            {"sessionId": sid, "targetInfo": {"targetId": sid, "browserContextId": context}},
            None,
        )
    await raw._dispatch_event("Page.loadEventFired", {}, "theirs")
    await raw._dispatch_event("Page.loadEventFired", {}, "ours")
    await raw._dispatch_event("Target.detachedFromTarget", {"sessionId": "ours"}, None)
    await raw._dispatch_event("Page.loadEventFired", {}, "ours")
    assert seen == ["ours"]
    with pytest.raises(PermissionError):
        await view.send("Runtime.evaluate", {"expression": "1"}, "ours")


@pytest.mark.parametrize(
    "method", ["Browser.close", "Target.disposeBrowserContext", "Storage.clearDataForOrigin"]
)
async def test_browser_global_mutations_are_refused_even_with_own_session(method):
    raw = CDPClient("ws://unused")
    view = BrowserContextClient(raw, "owned")
    await raw._dispatch_event(
        "Target.attachedToTarget",
        {"sessionId": "sid", "targetInfo": {"targetId": "tab", "browserContextId": "owned"}},
        None,
    )
    with pytest.raises(PermissionError):
        await view.send(method, {}, "sid")
