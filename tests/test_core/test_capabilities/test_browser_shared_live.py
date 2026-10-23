"""Two runs share Chrome while their targets, state and recordings stay separate."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import NetworkArchiver, StorageArchiver
from spectragent.core.capabilities.browser.backends.base import SharedBrowserProvider
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.composer import ComposedRun
from spectragent.core.experiment import ExperimentScope

pytestmark = pytest.mark.requires_chrome


@asynccontextmanager
async def fixture_site():
    requests = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            path = data.decode().split(" ")[1]
            requests.append(path)
            parsed = urlsplit(path)
            label = parse_qs(parsed.query).get("run", ["unknown"])[0]
            content_type = "text/html"
            if parsed.path == "/root":
                content = f"""<script>
                document.cookie='owner={label};path=/';localStorage.setItem('owner','{label}');sessionStorage.setItem('owner','{label}');
                window.worker=new Worker('/worker.js?run={label}');
                window.shared=new SharedWorker('/shared.js?run={label}');shared.port.start();
                navigator.serviceWorker.register('/service.js?run={label}',{{scope:'/'}});
                </script><iframe src="http://localhost:{port}/frame?run={label}"></iframe>"""
            elif parsed.path == "/worker.js":
                content_type = "application/javascript"
                content = f"fetch('/seen?kind=worker&run={label}');"
            elif parsed.path == "/shared.js":
                content_type = "application/javascript"
                content = f"fetch('/seen?kind=shared&run={label}');onconnect=e=>e.ports[0].postMessage('ok');"
            elif parsed.path == "/service.js":
                content_type = "application/javascript"
                content = f"self.addEventListener('install',e=>e.waitUntil(fetch('/seen?kind=service&run={label}')));"
            elif parsed.path in ("/frame", "/popup"):
                content = f"<script>fetch('/seen?kind={parsed.path[1:]}&run={label}')</script>"
            else:
                content = "ok"
            body = content.encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        await server.wait_closed()


def capability(scope):
    provider = LocalChromeProvider(
        lifetime="experiment", extra_args=["--site-per-process", "--no-proxy-server"]
    )
    scope.bind(ComposedRun({"browser_provider": {"shared": provider}}))
    cap = BrowserCapability(profile="vision_only")
    cap.backend = provider
    cap.bind_archivers([NetworkArchiver(require_replayable=False), StorageArchiver()])
    return cap


async def evaluate(cap, expression):
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        cap.default_session_id,
    )
    assert "exceptionDetails" not in reply
    return reply["result"].get("value")


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.02)


def recorded_urls(batch):
    urls = []
    for item in batch.files:
        if item.archiver_id == "browser.network" and item.name == "index.jsonl":
            for line in item.data.decode().splitlines():
                row = json.loads(line)
                if row.get("kind") == "exchange":
                    urls.append(row.get("url", ""))
    return urls


async def test_concurrent_contexts_keep_state_targets_and_recordings_then_close_one():
    async with fixture_site() as (origin, requests), ExperimentScope() as scope:
        a, b = capability(scope), capability(scope)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    a.setup(negotiation={"browser.browser_meta"}, start_url=f"{origin}/root?run=A"),
                    b.setup(negotiation={"browser.browser_meta"}, start_url=f"{origin}/root?run=B"),
                ),
                45,
            )
            assert (
                a.browser_context_id
                and b.browser_context_id
                and a.browser_context_id != b.browser_context_id
            )
            assert isinstance(a.backend, SharedBrowserProvider)
            assert isinstance(b.backend, SharedBrowserProvider)
            assert a.backend.managed_browser is b.backend.managed_browser
            for cap, label in ((a, "A"), (b, "B")):
                assert await evaluate(
                    cap, "[document.cookie,localStorage.owner,sessionStorage.owner]"
                ) == [f"owner={label}", label, label]
                targets = await cap.cdp.send("Target.getTargets")
                assert {t["browserContextId"] for t in targets["targetInfos"]} == {
                    cap.browser_context_id
                }
                await evaluate(
                    cap, f"window.open('{origin}/popup?run={label}','_blank','noopener');'ok'"
                )
            await until(
                lambda: all(
                    f"/seen?kind={kind}&run={label}" in requests
                    for kind in ("worker", "shared", "service", "frame", "popup")
                    for label in ("A", "B")
                )
            )
            with pytest.raises(PermissionError):
                await a.cdp.send("Runtime.evaluate", {"expression": "1"}, b.default_session_id)
            batches = await asyncio.gather(a.collect_archive(), b.collect_archive())
            for batch, label, other in ((batches[0], "A", "B"), (batches[1], "B", "A")):
                urls = recorded_urls(batch)
                assert any(f"kind=shared&run={label}" in url for url in urls), urls
                assert any(f"/popup?run={label}" in url for url in urls), urls
                assert not any(f"run={other}" in url for url in urls), urls
            assert a._context_lease is not None
            async with CDPClient(a._context_lease.cdp_url, proxy=None) as outside:
                sentinel = (await outside.send("Target.createTarget", {"url": "about:blank"}))[
                    "targetId"
                ]
                await a.teardown()
                assert await evaluate(b, "localStorage.owner") == "B"
                remaining = await outside.send("Target.getTargets")
                assert sentinel in {t["targetId"] for t in remaining["targetInfos"]}
                await b.create_tab(f"{origin}/popup?run=B_after_A")
                await until(lambda: "/seen?kind=popup&run=B_after_A" in requests)
                later = recorded_urls(await b.collect_archive())
                assert any("run=B_after_A" in url for url in later), later
        finally:
            await asyncio.gather(a.teardown(), b.teardown())


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_setup_releases_only_its_lease(monkeypatch, cancel):
    import importlib

    module = importlib.import_module("spectragent.core.capabilities.browser.capability")
    async with fixture_site() as (origin, _requests), ExperimentScope() as scope:
        a, b = capability(scope), capability(scope)
        try:
            await a.setup(negotiation={"browser.browser_meta"}, start_url=f"{origin}/root?run=A")
            assert a._context_lease is not None
            entered = asyncio.Event()

            async def fail_measurement(_client, **_budget):
                entered.set()
                if cancel:
                    await asyncio.Event().wait()
                raise RuntimeError("injected setup failure after lease acquisition")

            monkeypatch.setattr(module, "measure_input_platform", fail_measurement)
            setup = asyncio.create_task(b.setup(negotiation={"browser.browser_meta"}))
            await entered.wait()
            failed_context = b.browser_context_id
            assert failed_context and failed_context != a.browser_context_id
            if cancel:
                setup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await setup
            else:
                with pytest.raises(RuntimeError, match="injected setup failure"):
                    await setup
            await b.teardown()
            assert await evaluate(a, "localStorage.owner") == "A"
            async with CDPClient(a._context_lease.cdp_url, proxy=None) as outside:
                contexts = await outside.send("Target.getBrowserContexts")
                assert failed_context not in contexts["browserContextIds"]
                assert a.browser_context_id in contexts["browserContextIds"]
        finally:
            await asyncio.gather(a.teardown(), b.teardown())


async def test_concurrent_runtimes_commit_independent_complete_traces(tmp_path):
    from spectragent.agents.memory.simple import SimpleMemory
    from spectragent.core.environments.browser import BrowserEnvironment
    from spectragent.core.hooks.trace_writer import TraceWriter
    from spectragent.core.llm.messages import UserMessage
    from spectragent.core.llm.views import ChatInvokeCompletion
    from spectragent.core.loops import ReactLoop
    from spectragent.core.runtime import AgentRuntime
    from spectragent.core.trace.reader import RunTrace
    from spectragent.schemas.action import Action
    from tests._helpers.assembly import AGENT_ID, make_run

    class Agent:
        agent_id = "fixture"
        __execution_limit_fields__ = frozenset()
        __wiring__ = {
            "action": ("action", ["browser.done"]),
            "observation": ("observation", ["browser.browser_meta"]),
        }

        async def build_react_prompt(self, obs, *, context):
            return [UserMessage(content="finish the local fixture task")]

        def get_react_output_model(self):
            return None

        def parse_react_output(self, output):
            return [Action(name="browser.done", params={"extracted_content": "fixture complete"})]

    async with fixture_site() as (origin, requests), ExperimentScope() as scope:

        class Model:
            inference_kind = "chat"
            model = name = "scripted_fixture"

            def __init__(self, label):
                self.label = label

            async def ainvoke(self, messages, **kwargs):
                await until(
                    lambda: all(
                        f"/seen?kind={kind}&run={self.label}" in requests
                        for kind in ("worker", "shared", "service", "frame")
                    )
                )
                return ChatInvokeCompletion(completion="done", usage=None)

            async def aclose(self, *, timeout=5):
                return ()

        runs = []
        hooks = []
        for label in ("A", "B"):
            cap = capability(scope)
            hook = TraceWriter(
                trace_dir=str(tmp_path),
                archive=[NetworkArchiver(require_replayable=False), StorageArchiver()],
            )
            run = make_run(
                agent=Agent(),
                loop=ReactLoop(),
                env=BrowserEnvironment(capabilities={"browser": cap}),
                llm=Model(label),
                memory=SimpleMemory(),
                hooks=[hook],
            )
            runs.append(run)
            hooks.append(hook)
        results = await asyncio.gather(
            *(
                AgentRuntime.for_agent(run, AGENT_ID).execute(
                    "fixture",
                    task_id=label,
                    run_id=f"shared-{label}",
                    env_context={"start_url": f"{origin}/root?run={label}"},
                )
                for run, label in zip(runs, ("A", "B"), strict=True)
            )
        )
        assert all(result.agent_done.value == "done" for result in results)
        for label, other in (("A", "B"), ("B", "A")):
            trace = RunTrace(tmp_path / f"shared-{label}")
            assert trace.manifest.status == "complete"
            urls = []
            for _, record in trace.iter_archive("browser.network", include_trailing=True):
                if record.path.endswith("index.jsonl"):
                    urls.extend(
                        json.loads(line).get("url", "")
                        for line in trace.read_extra(record.path).decode().splitlines()
                    )
            assert any(f"kind=shared&run={label}" in url for url in urls), urls
            assert not any(f"run={other}" in url for url in urls), urls
