"""CDPClient transport options, pushed liveness and per-command timeouts.

Against a real local websockets server, except where the point is the exact
keyword arguments handed to ``websockets.connect``. See
``docs/architecture_design.md`` §4.14 ("为什么是第二条连接"): the recorder
connection needs ``max_frame_size=None``, ``compression=None`` and
``proxy=None``, and both connections need to learn that they died.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from spectragent.core.capabilities.browser import cdp_client as cdp_client_module
from spectragent.core.capabilities.browser.cdp_client import DEFAULT_MAX_FRAME_SIZE, CDPClient


class _Server:
    """A tiny JSON-RPC peer standing in for Chrome's DevTools endpoint."""

    def __init__(self) -> None:
        self.extension_headers: list[str | None] = []
        self.closed = asyncio.Event()
        self.url = ""
        #: Every method received, in arrival order.
        self.methods: list[str] = []

    async def handler(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        self.extension_headers.append(ws.request.headers.get("Sec-WebSocket-Extensions"))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                method = msg["method"]
                self.methods.append(method)
                params = msg.get("params") or {}
                if method == "Test.echo":
                    await ws.send(json.dumps({"id": msg["id"], "result": params}))
                elif method == "Test.big":
                    data = "x" * int(params["size"])
                    await ws.send(json.dumps({"id": msg["id"], "result": {"data": data}}))
                elif method == "Test.late":
                    await asyncio.sleep(float(params["delay"]))
                    await ws.send(json.dumps({"id": msg["id"], "result": {}}))
                elif method == "Test.close":
                    await ws.close(code=1000, reason="bye")
                    return
                # "Test.never": no reply at all.
        finally:
            self.closed.set()


@pytest.fixture
async def server() -> AsyncIterator[_Server]:
    peer = _Server()
    async with serve(peer.handler, "127.0.0.1", 0, max_size=None) as srv:
        port = next(iter(srv.sockets)).getsockname()[1]
        peer.url = f"ws://127.0.0.1:{port}"
        yield peer


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestOptionsReachConnect:
    """The three transport knobs are handed to ``websockets.connect`` verbatim."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []

        class _IdleConnection:
            close_code = None
            close_reason = ""

            def __aiter__(self) -> _IdleConnection:
                return self

            async def __anext__(self) -> str:
                await asyncio.Event().wait()
                raise StopAsyncIteration

            async def close(self) -> None:
                return None

        async def _connect(url: str, **kwargs: Any) -> _IdleConnection:
            seen.append({"url": url, **kwargs})
            return _IdleConnection()

        monkeypatch.setattr(cdp_client_module.websockets, "connect", _connect)
        return seen

    async def test_recorder_settings_are_passed_through(self, monkeypatch) -> None:
        seen = self._capture(monkeypatch)
        client = CDPClient("ws://x/devtools", max_frame_size=None, compression=None, proxy=None)
        await client.start()
        await client.stop()
        assert seen == [
            {
                "url": "ws://x/devtools",
                "max_size": None,
                "additional_headers": None,
                "compression": None,
                "proxy": None,
            }
        ]

    async def test_defaults_are_the_websockets_defaults_and_100_mib(self, monkeypatch) -> None:
        seen = self._capture(monkeypatch)
        client = CDPClient("ws://x/devtools", additional_headers={"h": "v"})
        await client.start()
        await client.stop()
        assert seen[0]["max_size"] == DEFAULT_MAX_FRAME_SIZE == 100 * 1024 * 1024
        assert seen[0]["compression"] == "deflate"
        assert seen[0]["proxy"] is True
        assert seen[0]["additional_headers"] == {"h": "v"}


class TestTransportAgainstARealPeer:
    async def test_compression_none_offers_no_extension(self, server: _Server) -> None:
        plain = CDPClient(server.url, compression=None, proxy=None)
        await plain.start()
        assert await plain.send("Test.echo", {"a": 1}) == {"a": 1}
        await plain.stop()

        deflate = CDPClient(server.url, proxy=None)
        await deflate.start()
        await deflate.send("Test.echo", {})
        await deflate.stop()

        assert server.extension_headers[0] is None
        assert "permessage-deflate" in (server.extension_headers[1] or "")

    async def test_proxy_none_ignores_the_environment_proxy(
        self, server: _Server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``proxy=True`` (websockets' default) tunnels loopback through ``*_proxy``.

        The environment points the ``ws`` scheme at a closed port with no
        ``no_proxy`` bypass, so a client that honours the environment cannot
        connect at all, and one built with ``proxy=None`` connects directly.
        """
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ws_proxy", f"http://127.0.0.1:{_closed_port()}")

        via_env = CDPClient(server.url)
        with pytest.raises(OSError):
            await via_env.start()

        direct = CDPClient(server.url, proxy=None)
        await direct.start()
        assert await direct.send("Test.echo", {"ok": True}) == {"ok": True}
        await direct.stop()

    async def test_stop_closes_the_socket(self, server: _Server) -> None:
        client = CDPClient(server.url, proxy=None)
        await client.start()
        await client.send("Test.echo", {})
        await client.stop()
        await asyncio.wait_for(server.closed.wait(), 2.0)
        assert client.is_connected is False


class TestFrameLimit:
    async def test_oversize_message_closes_a_small_limit_client(self, server: _Server) -> None:
        reasons: list[BaseException | None] = []
        client = CDPClient(server.url, max_frame_size=1024, proxy=None)
        client.add_close_callback(reasons.append)
        await client.start()

        waiting = asyncio.create_task(client.send("Test.never"))
        await asyncio.sleep(0)
        with pytest.raises(ConnectionError, match="WebSocket connection lost"):
            await client.send("Test.big", {"size": 10_000})
        with pytest.raises(ConnectionError, match="WebSocket connection lost"):
            await waiting

        assert client.is_connected is False
        assert len(reasons) == 1
        reason = reasons[0]
        assert isinstance(reason, websockets.ConnectionClosed)
        assert reason.rcvd is None or reason.rcvd.code != 1000
        assert reason.sent is not None and reason.sent.code == 1009
        await client.stop()
        assert len(reasons) == 1, "stop() after a loss must not report a second close"

    async def test_unlimited_client_survives_a_large_reply(self, server: _Server) -> None:
        client = CDPClient(server.url, max_frame_size=None, compression=None, proxy=None)
        await client.start()
        reply = await client.send("Test.big", {"size": 2_000_000})
        assert len(reply["data"]) == 2_000_000
        assert await client.send("Test.echo", {"after": 1}) == {"after": 1}
        assert client.is_connected is True
        await client.stop()


class TestCloseCallback:
    async def test_stop_reports_none(self, server: _Server) -> None:
        reasons: list[BaseException | None] = []
        client = CDPClient(server.url, proxy=None)
        client.add_close_callback(reasons.append)
        await client.start()
        await client.stop()
        assert reasons == [None]

    async def test_peer_close_reports_the_reason(self, server: _Server) -> None:
        reasons: list[BaseException | None] = []
        lost = asyncio.Event()

        def _on_close(reason: BaseException | None) -> None:
            reasons.append(reason)
            lost.set()

        client = CDPClient(server.url, proxy=None)
        client.add_close_callback(_on_close)
        await client.start()
        waiting = asyncio.create_task(client.send("Test.never"))
        await asyncio.sleep(0)
        with pytest.raises(ConnectionError):
            await client.send("Test.close")
        await asyncio.wait_for(lost.wait(), 2.0)
        with pytest.raises(ConnectionError):
            await waiting
        assert len(reasons) == 1 and reasons[0] is not None
        assert "1000" in str(reasons[0])
        await client.stop()
        assert len(reasons) == 1

    async def test_a_raising_callback_does_not_stop_the_others(self, server: _Server) -> None:
        reasons: list[BaseException | None] = []

        def _boom(_reason: BaseException | None) -> None:
            raise RuntimeError("callback failure")

        client = CDPClient(server.url, proxy=None)
        client.add_close_callback(_boom)
        client.add_close_callback(reasons.append)
        await client.start()
        await client.stop()
        assert reasons == [None]


class TestTimeoutAndFailPending:
    async def test_timeout_names_the_method_and_drops_the_entry(
        self, server: _Server, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = CDPClient(server.url, proxy=None)
        await client.start()
        with pytest.raises(TimeoutError, match="Test.late"):
            await client.send("Test.late", {"delay": 0.3}, timeout=0.05)
        assert client._pending == {}

        # The late reply arrives after the caller gave up: dropped silently.
        with caplog.at_level(logging.WARNING, logger=cdp_client_module.__name__):
            await asyncio.sleep(0.4)
            assert await client.send("Test.echo", {"still": "alive"}) == {"still": "alive"}
        assert not [r for r in caplog.records if "unknown id" in r.getMessage()]
        assert client._abandoned == set()
        await client.stop()

    async def test_reply_within_the_timeout_is_returned(self, server: _Server) -> None:
        client = CDPClient(server.url, proxy=None)
        await client.start()
        assert await client.send("Test.echo", {"x": 1}, timeout=2.0) == {"x": 1}
        await client.stop()

    async def test_fail_pending_fails_every_waiter_with_the_given_error(
        self, server: _Server
    ) -> None:
        class _Lost(RuntimeError):
            pass

        client = CDPClient(server.url, proxy=None)
        await client.start()
        first = asyncio.create_task(client.send("Test.never"))
        second = asyncio.create_task(client.send("Test.never"))
        await asyncio.sleep(0.05)
        client.fail_pending(_Lost("recorder connection lost"))
        for task in (first, second):
            with pytest.raises(_Lost, match="recorder"):
                await task
        assert client._pending == {}
        # The connection itself is untouched.
        assert await client.send("Test.echo", {"y": 2}) == {"y": 2}
        await client.stop()


class TestPost:
    """``post`` writes now and waits later: the order on the wire is the order of the posts."""

    async def test_posted_commands_reach_the_peer_in_order_before_any_reply_is_awaited(
        self, server: _Server
    ) -> None:
        client = CDPClient(server.url, proxy=None)
        await client.start()
        never = await client.post("Test.never")
        echo = await client.post("Test.echo", {"z": 3})
        # The second reply arrives although the first command never answers.
        assert await echo.wait(2.0) == {"z": 3}
        assert server.methods == ["Test.never", "Test.echo"]
        with pytest.raises(TimeoutError, match="Test.never"):
            await never.wait(0.05)
        assert client._pending == {}
        await client.stop()

    async def test_an_abandoned_reply_is_dropped_quietly(
        self, server: _Server, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = CDPClient(server.url, proxy=None)
        await client.start()
        late = await client.post("Test.late", {"delay": 0.2})
        late.abandon()
        late.abandon()  # idempotent
        assert client._pending == {}
        with caplog.at_level(logging.WARNING, logger=cdp_client_module.__name__):
            await asyncio.sleep(0.3)
            assert await client.send("Test.echo", {"after": 1}) == {"after": 1}
        assert not [r for r in caplog.records if "unknown id" in r.getMessage()]
        await client.stop()

    async def test_post_on_a_stopped_client_refuses(self, server: _Server) -> None:
        client = CDPClient(server.url, proxy=None)
        with pytest.raises(RuntimeError, match="not connected"):
            await client.post("Test.echo")


class TestControlTransportOptions:
    """Loopback is direct and uncompressed; a remote endpoint keeps the defaults."""

    @pytest.mark.parametrize(
        "url",
        [
            "ws://localhost:9222/devtools/browser/x",
            "ws://127.0.0.1:9222/devtools/browser/x",
            "ws://127.8.9.10:1/x",
            "ws://[::1]:9222/devtools/browser/x",
        ],
    )
    def test_loopback(self, url: str) -> None:
        from spectragent.core.capabilities.browser.cdp_client import transport_options

        assert transport_options(url) == {"proxy": None, "compression": None}

    @pytest.mark.parametrize(
        "url",
        [
            "wss://connect.browser.example/devtools?token=abc",
            "ws://10.0.0.5:9222/x",
            "ws://localhost.example.com/x",
        ],
    )
    def test_remote(self, url: str) -> None:
        from spectragent.core.capabilities.browser.cdp_client import transport_options

        assert transport_options(url) == {}


class TestALocalReceiveErrorClosesTheSocket:
    async def test_the_peer_sees_the_client_go(self, server: _Server, monkeypatch) -> None:
        """A client that stops reading must not stay connected (§4.14).

        Chrome detaches a closed client's sessions, but a connected client
        that no longer answers — holding ``waitForDebuggerOnStart`` — wedges
        every new target in the browser.
        """
        reasons: list[BaseException | None] = []
        client = CDPClient(server.url, proxy=None)
        client.add_close_callback(reasons.append)
        await client.start()

        def _broken(_msg: dict[str, Any]) -> None:
            raise KeyError("malformed reply")

        monkeypatch.setattr(client, "_handle_response", _broken)
        with pytest.raises(ConnectionError, match="WebSocket connection lost"):
            await client.send("Test.echo", {})
        await asyncio.wait_for(server.closed.wait(), 2.0)
        assert len(reasons) == 1 and isinstance(reasons[0], KeyError)
        await client.stop()
