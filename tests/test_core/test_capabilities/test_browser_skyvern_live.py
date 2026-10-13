"""Skyvern-style JS extraction through a real Chrome and action path."""

from __future__ import annotations

import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action
from spectragent.scripts.generate_all import _collect_registrars

pytestmark = pytest.mark.requires_chrome


def configured_capability(
    *, max_text_length: int = 5000, serializer_type: str = "text"
) -> BrowserCapability:
    """Use the real YAML-shaped config path, including the cleaner receptor."""
    run = ConfigComposer(registrars=_collect_registrars()).compose(
        {
            "capability": [
                {
                    "capability_id": "browser",
                    "profile": "skyvern",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "none"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": {
                        "cleaner_type": "skyvern",
                        "max_text_length": max_text_length,
                    },
                    "dom_serializer": {"serializer_type": serializer_type},
                    "backend": {"provider_id": "local_chrome", "headless": True},
                }
            ]
        }
    )
    cap = run.single("capability")
    assert isinstance(cap, BrowserCapability)
    assert isinstance(cap.dom_cleaner, SkyvernCleaner)
    return cap


@pytest.fixture
def site_url(tmp_path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_skyvern_js_tree_changes_observation_and_clicks_by_index(
    site_url, monkeypatch
) -> None:
    tmp_path, base_url = site_url
    page = tmp_path / "skyvern.html"
    page.write_text(
        """<!doctype html><html><body>
        <button id="go" onclick="this.dataset.clicked='yes'">Go</button>
        <input id="secret" type="password" value="do-not-record">
        <textarea id="otp" autocomplete="one-time-code">123456</textarea>
        <input id="pin" inputmode="numeric" value="654321">
        <iframe id="frame" srcdoc='<button id="inside" onclick="this.textContent=String.fromCharCode(67,108,105,99,107,101,100)">Inside</button>'></iframe>
        </body></html>""",
        encoding="utf-8",
    )
    (tmp_path / "next.html").write_text(
        '<html><body><button id="next">Next</button></body></html>', encoding="utf-8"
    )
    cap = configured_capability(max_text_length=1)
    try:
        await cap.setup(start_url=f"{base_url}/skyvern.html")
        methods: list[str] = []
        raw_send = cap.cdp.send

        async def record_send(method, *args, **kwargs):
            methods.append(method)
            return await raw_send(method, *args, **kwargs)

        monkeypatch.setattr(cap.cdp, "send", record_send)
        fragment = await cap.resolve_fragment("browser.dom")
        state = fragment.element_tree
        indexed = {
            node.attributes.get("id"): (key, node) for key, node in state.selector_map.items()
        }
        assert "go" in indexed
        assert "inside" in indexed
        assert indexed["go"][1].text_content == "G…"
        assert "do-not-record" not in state.serialized_dom
        assert "123456" not in state.serialized_dom
        assert "654321" not in state.serialized_dom
        secret = indexed["secret"][1]
        assert secret.attributes.get("data-af-redacted") == "true"
        assert "value" not in secret.attributes
        assert indexed["otp"][1].text_content == ""
        assert "value" not in indexed["otp"][1].attributes
        assert "value" not in indexed["pin"][1].attributes
        assert not {
            "DOM.getDocument",
            "DOMSnapshot.captureSnapshot",
            "Accessibility.getFullAXTree",
        } & set(methods)

        result = await cap.execute_action(
            Action(name="browser.click", params={"index": indexed["inside"][0]})
        )
        assert result.success, result.error
        cap._cycle_cache.pop(cap.default_session_id, None)
        after = (await cap.resolve_fragment("browser.dom")).element_tree
        clicked = next(n for n in after.selector_map.values() if n.attributes.get("id") == "inside")
        assert clicked.text_content == "C…"

        navigated = await cap.execute_action(
            Action(name="browser.navigate", params={"url": f"{base_url}/next.html"})
        )
        assert navigated.success, navigated.error
        cap._cycle_cache.pop(cap.default_session_id, None)
        next_state = (await cap.resolve_fragment("browser.dom")).element_tree
        assert {n.attributes.get("id") for n in next_state.selector_map.values()} == {"next"}

        await cap.cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 800, "height": 600, "deviceScaleFactor": 1, "mobile": False},
            session_id=cap.default_session_id,
        )
        cap._cycle_cache.pop(cap.default_session_id, None)
        resized = (await cap.resolve_fragment("browser.dom")).element_tree
        assert (resized.scroll_info.viewport_width, resized.scroll_info.viewport_height) == (
            800,
            600,
        )
    finally:
        await cap.teardown()


async def test_skyvern_js_tree_reaches_out_of_process_frame(site_url) -> None:
    tmp_path, base_url = site_url
    port = base_url.rsplit(":", 1)[1]
    (tmp_path / "outer.html").write_text(
        f"<html><body><iframe id='cross' src='http://localhost:{port}/cross.html'></iframe></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "cross.html").write_text(
        '<html><body><button id="inside-cross" onclick="this.textContent=\'Clicked\'">Cross</button></body></html>',
        encoding="utf-8",
    )
    cap = configured_capability()
    try:
        await cap.setup(start_url=f"{base_url}/outer.html")
        state = (await cap.resolve_fragment("browser.dom")).element_tree
        found = [
            (key, node)
            for key, node in state.selector_map.items()
            if node.attributes.get("id") == "inside-cross"
        ]
        assert len(found) == 1, state.serialized_dom
        key, node = found[0]
        assert node.session_id != cap.default_session_id
        owner = node.frame_owners()[0]
        owner_box = await cap.cdp.send(
            "DOM.getBoxModel",
            {"backendNodeId": owner.backend_node_id},
            session_id=owner.session_id,
        )
        node_box = await cap.cdp.send(
            "DOM.getBoxModel",
            {"backendNodeId": node.backend_node_id},
            session_id=node.session_id,
        )
        assert node.bounds is not None
        assert (
            abs(node.bounds.x - (owner_box["model"]["content"][0] + node_box["model"]["border"][0]))
            < 2
        )
        assert (
            abs(node.bounds.y - (owner_box["model"]["content"][1] + node_box["model"]["border"][1]))
            < 2
        )
        result = await cap.execute_action(Action(name="browser.click", params={"index": key}))
        assert result.success, result.error
        cap._cycle_cache.pop(cap.default_session_id, None)
        after = (await cap.resolve_fragment("browser.dom")).element_tree
        clicked = next(
            n for n in after.selector_map.values() if n.attributes.get("id") == "inside-cross"
        )
        assert clicked.text_content == "Clicked"
    finally:
        await cap.teardown()


async def test_skyvern_js_capture_is_tab_bound_and_releases_both_scopes(site_url) -> None:
    tmp_path, base_url = site_url
    (tmp_path / "first.html").write_text(
        '<html><body><button id="first">First</button></body></html>', encoding="utf-8"
    )
    (tmp_path / "second.html").write_text(
        '<html><body><button id="second">Second</button></body></html>', encoding="utf-8"
    )
    cap = configured_capability()
    try:
        await cap.setup(start_url=f"{base_url}/first.html")
        first_sid = cap.default_session_id
        first_target = cap._session_to_target[first_sid]
        first = (await cap.resolve_fragment("browser.dom")).element_tree
        assert {n.attributes.get("id") for n in first.selector_map.values()} == {"first"}

        second_sid = await cap.create_tab()
        second_target = cap._session_to_target[second_sid]
        await cap.switch_tab(second_target)
        navigated = await cap.execute_action(
            Action(name="browser.navigate", params={"url": f"{base_url}/second.html"})
        )
        assert navigated.success, navigated.error
        second = (await cap.resolve_fragment("browser.dom")).element_tree
        assert {n.attributes.get("id") for n in second.selector_map.values()} == {"second"}

        await cap.switch_tab(first_target)
        first_again = (await cap.resolve_fragment("browser.dom")).element_tree
        assert {n.attributes.get("id") for n in first_again.selector_map.values()} == {"first"}
        assert first_sid in cap._js_scopes and second_sid in cap._js_scopes
        await cap.close_tab(second_sid)
        assert second_sid not in cap._scopes and second_sid not in cap._js_scopes
        assert first_sid in cap._scopes and first_sid in cap._js_scopes
    finally:
        await cap.teardown()


async def test_skyvern_text_nodes_reach_serialization_and_dom_budget(site_url) -> None:
    tmp_path, base_url = site_url
    paragraphs = "".join(f"<p>hello paragraph {i}</p>" for i in range(300))
    (tmp_path / "prose.html").write_text(
        f'<html><body><button id="go">Submit</button>{paragraphs}</body></html>',
        encoding="utf-8",
    )
    cap = configured_capability()
    cap.dom_max_serialized_chars = 1000
    try:
        await cap.setup(start_url=f"{base_url}/prose.html")
        fragment = await cap.resolve_fragment("browser.dom")
        serialized = fragment.element_tree.serialized_dom
        assert "Submit" in serialized
        assert "hello paragraph 0" in serialized
        assert fragment.budget is not None
        assert fragment.budget.serialized_chars_before > 1000
        assert fragment.budget.serialized_chars_after <= 1000
        assert len(serialized) < 1100  # includes the explicit budget note
    finally:
        await cap.teardown()


async def test_skyvern_html_serializer_keeps_text_once(site_url) -> None:
    tmp_path, base_url = site_url
    (tmp_path / "html.html").write_text(
        '<html><body><button id="go">Submit</button><p>hello prose</p></body></html>',
        encoding="utf-8",
    )
    cap = configured_capability(serializer_type="html")
    try:
        await cap.setup(start_url=f"{base_url}/html.html")
        serialized = (await cap.resolve_fragment("browser.dom")).element_tree.serialized_dom
        assert serialized.count("Submit") == 1
        assert serialized.count("hello prose") == 1
        assert "<#text" not in serialized
    finally:
        await cap.teardown()
