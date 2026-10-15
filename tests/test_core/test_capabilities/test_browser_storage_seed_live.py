"""Initial authenticated state and partitioned storage in real Chrome, no model calls."""

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import StorageArchiver
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.storage_seed import StorageSeed
from spectragent.schemas.action import Action
from spectragent.schemas.browser_storage import (
    BrowserStorageState,
    SessionStorageArea,
    StorageArea,
    StorageTab,
)

pytestmark = pytest.mark.requires_chrome


@pytest.fixture
def auth_site() -> Iterator[tuple[str, list[tuple[str, str]]]]:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Cookie", "")))
            if self.path.startswith("/auth") and "sid=authenticated" not in self.headers.get(
                "Cookie", ""
            ):
                self.send_response(401)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<p>login required</p>")
                return
            if self.path == "/frame":
                body = """<script>
                parent.postMessage({local:localStorage.getItem('token'),
                    session:sessionStorage.getItem('tab')}, '*');
                </script>"""
            elif self.path == "/partition":
                body = f"""<script>window.message=null;onmessage=e=>window.message=e.data</script>
                    <iframe src="http://third.test:{cast(ThreadingHTTPServer, self.server).server_port}/frame"></iframe>"""
            else:
                body = """<script>window.first={local:localStorage.getItem('token'),
                    session:sessionStorage.getItem('tab'), cookie:document.cookie};
                    localStorage.setItem('token','after-first-script');
                    document.write('<p>'+JSON.stringify(window.first)+'</p>');</script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://one.test:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def capability() -> BrowserCapability:
    cap = BrowserCapability(settle_quiet_ms=30)
    cap.backend = LocalChromeProvider(
        headless=True,
        extra_args=[
            "--no-proxy-server",
            "--host-resolver-rules=MAP *.test 127.0.0.1",
        ],
    )
    cap.bind_archivers([StorageArchiver()])
    return cap


async def evaluate(cap: BrowserCapability, expression: str) -> Any:
    result = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        cap.default_session_id,
    )
    return result["result"].get("value")


async def test_seed_precedes_first_request_and_script_and_keeps_two_tab_sessions(
    auth_site: tuple[str, list[Any]],
) -> None:
    origin, requests = auth_site
    cap = capability()
    seed = BrowserStorageState(
        cookies=[{"name": "sid", "value": "authenticated", "domain": "one.test", "path": "/"}],
        local=[
            StorageArea(
                storage_key=origin + "/", origin=origin, items={"token": "before-first-script"}
            )
        ],
        tabs=[StorageTab(tab="alpha"), StorageTab(tab="beta")],
        session=[
            SessionStorageArea(tab=tab, storage_key=origin + "/", origin=origin, items={"tab": tab})
            for tab in ("alpha", "beta")
        ],
    )
    try:
        await cap.setup(storage_state=seed, start_url=origin + "/auth")
        assert await evaluate(cap, "window.first") == {
            "local": "before-first-script",
            "session": "alpha",
            "cookie": "sid=authenticated",
        }
        assert requests[0] == ("/auth", "sid=authenticated")
        assert all("__af_storage_seed__" not in path for path, _ in requests)
        # Hydration installs no recurring script: navigation sees the page's own mutation.
        assert (
            await cap.execute_action(
                Action(name="browser.navigate", params={"url": origin + "/again"})
            )
        ).success
        assert (await evaluate(cap, "window.first"))["local"] == "after-first-script"
        beta = next(target for target, tab in cap._storage_tabs.items() if tab == "beta")
        sid = await cap._attach_tab(beta, "about:blank")
        await cap.cdp.send("Page.navigate", {"url": origin + "/beta"}, sid)
        for _ in range(100):
            reply = await cap.cdp.send(
                "Runtime.evaluate", {"expression": "window.first", "returnByValue": True}, sid
            )
            if value := reply["result"].get("value"):
                break
            await asyncio.sleep(0.01)
        assert value["session"] == "beta"
        files = (await cap.collect_archive()).files
        initial = next(f for f in files if f.name == "initial_state.json")
        assert initial.data == StorageSeed.freeze(seed).data
        current = json.loads(next(f.data for f in files if f.name == "storage.json"))
        assert {area["tab"] for area in current["session"] if area["origin"] == origin} == {
            "alpha",
            "beta",
        }
    finally:
        await cap.teardown()


async def test_two_third_party_partitions_and_oopif_session_are_captured_and_seeded(
    auth_site: tuple[str, list[Any]],
) -> None:
    origin, requests = auth_site
    port = origin.rsplit(":", 1)[1]
    third = f"http://third.test:{port}"
    two = f"http://two.test:{port}"
    # Chrome's opaque key is checked against the live frame before writing.
    seed = BrowserStorageState(
        local=[
            StorageArea(
                origin=third,
                ancestor_origins=[top],
                storage_key=f"{third}/^0http://{name}.test",
                items={"token": name},
            )
            for top, name in ((origin, "one"), (two, "two"))
        ],
        tabs=[StorageTab(tab="alpha"), StorageTab(tab="beta")],
        session=[
            SessionStorageArea(
                tab=tab,
                origin=third,
                ancestor_origins=[top],
                storage_key=f"{third}/^0http://{name}.test",
                items={"tab": tab},
            )
            for tab, top, name in (("alpha", origin, "one"), ("beta", two, "two"))
        ],
        cookies=[
            {
                "name": "chip",
                "value": name,
                "domain": "third.test",
                "path": "/",
                "secure": True,
                "partitionKey": {
                    "topLevelSite": f"http://{name}.test",
                    "hasCrossSiteAncestor": True,
                },
            }
            for name in ("one", "two")
        ],
    )
    cap = capability()
    try:
        await cap.setup(storage_state=seed, start_url=origin + "/partition")
        for _ in range(200):
            message = await evaluate(cap, "window.message")
            if message:
                break
            await asyncio.sleep(0.01)
        assert message == {"local": "one", "session": "alpha"}
        beta = next(target for target, tab in cap._storage_tabs.items() if tab == "beta")
        sid = await cap._attach_tab(beta, "about:blank")
        await cap.cdp.send("Page.navigate", {"url": two + "/partition"}, sid)
        for _ in range(200):
            reply = await cap.cdp.send(
                "Runtime.evaluate", {"expression": "window.message", "returnByValue": True}, sid
            )
            if message := reply["result"].get("value"):
                break
            await asyncio.sleep(0.01)
        assert message == {"local": "two", "session": "beta"}
        files = (await cap.collect_archive()).files
        state = BrowserStorageState.model_validate(
            {
                k: v
                for k, v in json.loads(
                    next(f.data for f in files if f.name == "storage.json")
                ).items()
                if k not in {"captured_at", "redaction"}
            }
        )
        assert not state.unavailable
        assert {(a.storage_key, a.items["token"]) for a in state.local if a.origin == third} == {
            (a.storage_key, a.items["token"]) for a in seed.local
        }
        assert {(a.tab, a.items["tab"]) for a in state.session if a.origin == third} == {
            ("alpha", "alpha"),
            ("beta", "beta"),
        }
        assert {
            (c["partitionKey"]["topLevelSite"], c["value"])
            for c in state.cookies
            if c["name"] == "chip"
        } == {("http://one.test", "one"), ("http://two.test", "two")}
        assert all("__af_storage_seed__" not in path for path, _ in requests)
    finally:
        await cap.teardown()


def test_capture_offline_replay_and_branch_use_initial_seed_after_source_path_deleted(
    auth_site: tuple[str, list[Any]],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from spectragent.core.archive.source import open_archive
    from spectragent.core.capabilities.browser.storage_seed import read_initial_seed
    from tests._helpers import branch_driver as branch
    from tests._helpers import replay_driver as driver
    from tests._helpers.scripted_llm import ScriptedLLMServer

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    origin, requests = auth_site
    seed = BrowserStorageState(
        cookies=[{"name": "sid", "value": "authenticated", "domain": "one.test", "path": "/"}],
        local=[
            StorageArea(
                storage_key=origin + "/",
                origin=origin,
                items={"token": "initial", "auth_token": "must-not-redact-the-seed"},
            )
        ],
        tabs=[StorageTab(tab="alpha")],
        session=[
            SessionStorageArea(
                tab="alpha", storage_key=origin + "/", origin=origin, items={"tab": "alpha"}
            )
        ],
    )
    path: Path = tmp_path / "seed.json"
    path.write_text(seed.model_dump_json())
    fixture = SimpleNamespace(
        chrome_args=lambda **_: ["--no-proxy-server", "--host-resolver-rules=MAP *.test 127.0.0.1"]
    )
    with ScriptedLLMServer([driver.done("authenticated")]) as llm:
        raw = driver.record_config(
            site=cast(Any, fixture), llm=llm, trace_dir=tmp_path / "recorded"
        )
        source = asyncio.run(
            driver.run_task(
                raw,
                task="Inspect the authenticated page.",
                start_url=origin + "/auth",
                storage_state=str(path),
            )
        )
    source.require_finished(1, "authenticated")
    assert read_initial_seed(open_archive(source.run_dir)).data == StorageSeed.freeze(seed).data
    setup_record = source.trace.config.task_setup
    assert setup_record is not None
    assert setup_record.context == {"start_url": origin + "/auth"}
    assert setup_record.redactions.entries == []
    path.write_text('{"changed": true}')
    path.unlink()
    assert '"local":"initial"' in branch.page_text(source.trace, 0)
    before = len(requests)
    with ScriptedLLMServer([driver.done("authenticated")]) as llm:
        replay = driver.to_replay_config(raw, llm=llm, trace_dir=tmp_path / "replayed")
        from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError

        conflicting = seed.model_copy(deep=True)
        conflicting.local[0].items["token"] = "conflicting"
        with pytest.raises(BrowserCooperationError, match="conflicts with the replay archive"):
            asyncio.run(
                driver.run_task(
                    replay,
                    task="Inspect the authenticated page.",
                    start_url=origin + "/auth",
                    replay_archive=source.run_dir,
                    storage_state=conflicting,
                )
            )
        outcome = asyncio.run(
            driver.run_task(
                replay,
                task="Inspect the authenticated page.",
                start_url=origin + "/auth",
                replay_archive=source.run_dir,
            )
        )
    outcome.require_finished(1, "authenticated")
    assert '"local":"initial"' in branch.page_text(outcome.trace, 0)
    assert len(requests) == before, "offline replay reached the live fixture"
    with ScriptedLLMServer([driver.done("branched")]) as teacher:
        config = branch.branch_config(
            raw,
            llm=teacher,
            trace_dir=tmp_path / "branched",
            sources_root=tmp_path / "recorded",
            step=0,
            continue_steps=0,
        )
        trace = branch.require_ran(branch.run_branch_task(config, f"{source.trace.run_id}/s0/b0"))
    assert '"local":"initial"' in branch.page_text(trace, 0)
    assert len(requests) == before, "offline branch reached the live fixture"


@pytest.mark.parametrize("cancel", [False, True])
async def test_hydration_failure_and_cancellation_close_only_their_own_tabs(
    cancel: bool, monkeypatch: Any
) -> None:
    from spectragent.core.capabilities.browser.cdp_client import CDPClient
    from spectragent.core.capabilities.browser.storage_seed import hydrate_storage

    provider = LocalChromeProvider(headless=True)
    session = await provider.open()
    assert session.cdp_url is not None
    cdp = CDPClient(session.cdp_url, proxy=None, compression=None)
    await cdp.start()
    entered = asyncio.Event()
    original = cdp.send

    async def interrupted(method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "DOMStorage.setDOMStorageItem":
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("injected seed write failure")
        return await original(method, *args, **kwargs)

    try:
        before = {t["targetId"] for t in (await cdp.send("Target.getTargets", {}))["targetInfos"]}
        monkeypatch.setattr(cdp, "send", interrupted)
        seed = BrowserStorageState(
            local=[
                StorageArea(
                    storage_key="http://seed.test/",
                    origin="http://seed.test",
                    items={"token": "value"},
                )
            ]
        )
        task = asyncio.create_task(hydrate_storage(cdp, StorageSeed.freeze(seed), None))
        await asyncio.wait_for(entered.wait(), 10)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        after = {t["targetId"] for t in (await cdp.send("Target.getTargets", {}))["targetInfos"]}
        assert before == after
        assert not cdp._callbacks.get("Fetch.requestPaused")
        assert not cdp._callbacks.get("Target.attachedToTarget")
    finally:
        await cdp.stop()
        await provider.close(session)


async def test_authentication_fixture_rejects_an_unseeded_browser(
    auth_site: tuple[str, list[Any]],
) -> None:
    origin, requests = auth_site
    cap = capability()
    try:
        await cap.setup(start_url=origin + "/auth")
        assert await evaluate(cap, "document.body.innerText") == "login required"
        assert requests[0] == ("/auth", "")
    finally:
        await cap.teardown()
