"""The page sees the requested modifier, and a following plain key stays plain."""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.schemas.action import Action

pytestmark = pytest.mark.requires_chrome


async def read(cap, expression):
    response = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    return response["result"].get("value")


async def test_named_chords_reach_real_dom_modifiers_and_leave_no_modifier_held():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await cap.setup(negotiation={"browser.browser_meta"})
        await read(
            cap,
            """
document.body.innerHTML='<input id="first" value="first"><input id="second" value="old value">';
window.keys=[];
document.addEventListener('keydown',e=>keys.push({key:e.key,ctrl:e.ctrlKey,alt:e.altKey,meta:e.metaKey,shift:e.shiftKey}));
true
""",
        )
        for chord, expected in (
            ("ctrl+a", "ctrl"),
            ("Control+a", "ctrl"),
            ("alt+a", "alt"),
            ("meta+a", "meta"),
            ("cmd+a", "meta"),
            ("command+a", "meta"),
            ("shift+Tab", "shift"),
        ):
            await read(cap, "keys=[];document.getElementById('second').focus();true")
            result = await cap.execute_action(
                Action(name="browser.send_keys", params={"keys": chord})
            )
            assert result.success, result.error
            events = await read(cap, "keys")
            assert len(events) == 1, (chord, events)
            event = events[0]
            assert {name: event[name] for name in ("ctrl", "alt", "meta", "shift")} == {
                name: name == expected for name in ("ctrl", "alt", "meta", "shift")
            }
            if chord == "shift+Tab":
                assert await read(cap, "document.activeElement.id") == "first"
            await cap.execute_action(Action(name="browser.send_keys", params={"keys": "z"}))
            plain = (await read(cap, "keys"))[-1]
            assert plain == {"key": "z", "ctrl": False, "alt": False, "meta": False, "shift": False}
    finally:
        await cap.teardown()


async def test_only_native_select_all_selects_and_the_page_can_cancel_it():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await cap.setup(negotiation={"browser.browser_meta"})
        assert cap._input_platform is not None
        platform = cap._input_platform
        native = (
            ("meta+a", "cmd+a", "Command+a") if platform == "macos" else ("ctrl+a", "Control+a")
        )
        other = "ctrl+a" if platform == "macos" else "meta+a"
        await read(
            cap,
            """
document.body.innerHTML='<input id="field" value="original">';
window.field=document.getElementById('field');window.prevent=false;window.keys=[];
field.addEventListener('keydown',e=>{if(prevent&&e.key.toLowerCase()==='a')e.preventDefault();keys.push({key:e.key,ctrl:e.ctrlKey,meta:e.metaKey,prevented:e.defaultPrevented})});true
""",
        )

        async def reset(prevent=False):
            await read(
                cap,
                f"field.value='original';field.focus();field.setSelectionRange(8,8);keys=[];window.prevent={str(prevent).lower()};true",
            )

        for chord in native:
            await reset()
            result = await cap.execute_action(
                Action(name="browser.send_keys", params={"keys": chord})
            )
            assert result.success, result.error
            assert await read(cap, "[field.selectionStart,field.selectionEnd,field.value]") == [
                0,
                8,
                "original",
            ]
            await cap.execute_action(Action(name="browser.send_keys", params={"keys": "z"}))
            assert await read(cap, "field.value") == "z"

        await reset()
        result = await cap.execute_action(Action(name="browser.send_keys", params={"keys": other}))
        assert result.success, result.error
        assert await read(cap, "[field.selectionStart,field.selectionEnd]") != [0, 8]
        key = (await read(cap, "keys"))[0]
        assert key["ctrl"] is (platform == "macos")
        assert key["meta"] is (platform != "macos")

        await reset(prevent=True)
        await cap.execute_action(Action(name="browser.send_keys", params={"keys": native[0]}))
        assert await read(cap, "[field.selectionStart,field.selectionEnd,field.value]") == [
            8,
            8,
            "original",
        ]
        assert (await read(cap, "keys"))[0]["prevented"] is True
    finally:
        await cap.teardown()


async def test_invalid_chords_are_action_failures_without_dispatching_keys():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await cap.setup(negotiation={"browser.browser_meta"})
        await read(
            cap, "window.keys=[];document.addEventListener('keydown',e=>keys.push(e.key));true"
        )
        for chord in ("", " ", "ctrl+", "ctrl++a", "hyper+a", "commnad+a"):
            result = await cap.execute_action(
                Action(name="browser.send_keys", params={"keys": chord})
            )
            assert not result.success, chord
            assert result.error, chord
        assert await read(cap, "keys") == []
    finally:
        await cap.teardown()


async def test_dispatch_failure_is_not_reported_as_a_successful_key_action(monkeypatch):
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await cap.setup(negotiation={"browser.browser_meta"})
        send = cap.cdp.send

        async def refuse(method, params=None, *args, **kwargs):
            if method == "Input.dispatchKeyEvent" and (params or {}).get("type") == "keyDown":
                raise TimeoutError()
            return await send(method, params, *args, **kwargs)

        monkeypatch.setattr(cap.cdp, "send", refuse)
        result = await cap.execute_action(Action(name="browser.send_keys", params={"keys": "a"}))
        assert not result.success
        assert result.error and "TimeoutError" in result.error
    finally:
        await cap.teardown()
