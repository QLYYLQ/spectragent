"""LocalChromeProvider launch path — executable discovery and CDP readiness.

Both areas were only ever exercised by hand, and both failed in ways that
blamed the wrong thing:

* ``find_chrome`` searched Linux names only, so every macOS run died with
  "Cannot find Chrome / Chromium. Install with: apt install ..." even though
  Chrome was installed — just inside an ``.app`` bundle.
* ``_wait_for_cdp`` polled through whatever ``http_proxy`` happened to be
  exported. A proxy accepts the connection while Chrome's port is still
  closed and then stalls, so the poll raised ``ReadTimeout`` rather than
  ``ConnectError`` — and ``ReadTimeout`` was not in the retry's except
  tuple, so a Chrome slower than one 2s poll aborted startup outright with
  ~28s of its own budget unspent.
* Chrome was launched with ``stdout``/``stderr`` on pipes that nothing ever
  read. A finished run printed ``RuntimeError: Event loop is closed`` from
  the transport's ``__del__``; the unreported half was that a long enough
  session fills the pipe buffer and Chrome blocks in ``write()``.
"""

from __future__ import annotations

import asyncio
import sys

import httpx
import pytest

from spectragent.core.capabilities.browser.backends import local_chrome
from spectragent.core.capabilities.browser.backends.local_chrome import (
    LocalChromeProvider,
    _search_paths,
    find_chrome,
)

# ── Executable discovery ──────────────────────────────────────────


class TestFindChrome:
    def test_each_platform_gets_its_own_candidates(self, monkeypatch) -> None:
        """The Linux names are unreachable on a mac — nothing on PATH matches."""
        monkeypatch.setattr(local_chrome.sys, "platform", "darwin")
        assert _search_paths() is local_chrome._MACOS_PATHS

        monkeypatch.setattr(local_chrome.sys, "platform", "win32")
        assert _search_paths() is local_chrome._WINDOWS_PATHS

        monkeypatch.setattr(local_chrome.sys, "platform", "linux")
        assert _search_paths() is local_chrome._LINUX_PATHS

    def test_macos_app_bundle_is_discovered_without_an_env_override(
        self, monkeypatch
    ) -> None:
        bundle = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        monkeypatch.delenv("CHROME_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(local_chrome.sys, "platform", "darwin")
        monkeypatch.setattr(
            local_chrome.shutil, "which", lambda p: p if p == bundle else None
        )

        assert find_chrome() == bundle

    def test_a_broken_env_override_names_itself(self, monkeypatch) -> None:
        """Falling through to the search would report the generic message and
        hide the actual mistake — the path the user set."""
        monkeypatch.setenv("CHROME_EXECUTABLE_PATH", "/nope/chrome")
        monkeypatch.setattr(local_chrome.shutil, "which", lambda _p: None)

        with pytest.raises(FileNotFoundError, match=r"/nope/chrome"):
            find_chrome()

    def test_install_hint_matches_the_running_platform(self, monkeypatch) -> None:
        monkeypatch.delenv("CHROME_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(local_chrome.sys, "platform", "darwin")
        monkeypatch.setattr(local_chrome.shutil, "which", lambda _p: None)

        with pytest.raises(FileNotFoundError, match=r"brew install --cask"):
            find_chrome()


# ── CDP readiness poll ────────────────────────────────────────────


class _ScriptedClient:
    """Stands in for ``httpx.AsyncClient``; replays a scripted outcome list."""

    def __init__(self, outcomes: list, record: dict) -> None:
        self._outcomes = list(outcomes)
        self._record = record

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def get(self, _url, **_kw):
        self._record["polls"] = self._record.get("polls", 0) + 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _OkResponse:
    status_code = 200

    def json(self) -> dict:
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}


# Bound before the patch below replaces the module attribute — calling
# ``asyncio.sleep`` from inside the replacement would recurse into itself.
_REAL_SLEEP = asyncio.sleep


async def _no_sleep(_seconds: float) -> None:
    """Collapse the 0.2s inter-poll wait so retries cost no wall clock."""
    await _REAL_SLEEP(0)


class TestWaitForCdp:
    def _provider(self) -> LocalChromeProvider:
        p = LocalChromeProvider()
        p.cdp_wait_timeout = 5.0
        return p

    def _install(self, monkeypatch, outcomes: list) -> dict:
        record: dict = {}

        def _factory(**kwargs):
            record["kwargs"] = kwargs
            return _ScriptedClient(outcomes, record)

        monkeypatch.setattr(local_chrome.httpx, "AsyncClient", _factory)
        monkeypatch.setattr(local_chrome.asyncio, "sleep", _no_sleep)
        return record

    async def test_poll_never_traverses_an_ambient_proxy(self, monkeypatch) -> None:
        """Loopback through a proxy is never what this wants — it is the
        mechanism that turned a closed port into a stall."""
        record = self._install(monkeypatch, [_OkResponse()])

        await self._provider()._wait_for_cdp(9222)

        assert record["kwargs"].get("trust_env") is False

    async def test_read_timeout_keeps_polling_to_the_deadline(
        self, monkeypatch
    ) -> None:
        """The regression: a Chrome slower than one poll used to abort here."""
        record = self._install(
            monkeypatch,
            [
                httpx.ReadTimeout("proxy stalled while the port was closed"),
                httpx.ReadTimeout("still booting"),
                _OkResponse(),
            ],
        )

        ws = await self._provider()._wait_for_cdp(9222)

        assert ws.startswith("ws://127.0.0.1:9222/")
        assert record["polls"] == 3

    async def test_connect_error_still_retries(self, monkeypatch) -> None:
        """The direct-connection shape of "not ready yet"."""
        self._install(
            monkeypatch,
            [httpx.ConnectError("connection refused"), _OkResponse()],
        )

        assert await self._provider()._wait_for_cdp(9222)

    async def test_torn_json_body_is_treated_as_not_ready(self, monkeypatch) -> None:
        """Chrome can answer /json/version mid-write while it boots."""

        class _TornResponse:
            status_code = 200

            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        self._install(monkeypatch, [_TornResponse(), _OkResponse()])

        assert await self._provider()._wait_for_cdp(9222)

    async def test_exhausting_the_budget_still_raises(self, monkeypatch) -> None:
        """Retrying forever would hang a benchmark run instead of failing it."""
        self._install(monkeypatch, [httpx.ConnectError("refused")] * 500)
        provider = self._provider()
        provider.cdp_wait_timeout = 0.0

        with pytest.raises(TimeoutError, match="did not become ready"):
            await provider._wait_for_cdp(9222)


# ── Child stdio ───────────────────────────────────────────────────


class _FakeProcess:
    """Stands in for the Chrome the launch path never actually needs."""

    pid = 4321
    returncode = None


class TestLaunchStdio:
    async def test_chrome_stdio_is_discarded_rather_than_piped(
        self, monkeypatch, tmp_path
    ) -> None:
        """A pipe nobody reads is a stall waiting to happen — see the sibling
        test below for the mechanism. Asserts on what ``open()`` passes, not on
        anything the test itself wired up."""
        record: dict = {}

        async def _fake_exec(*cmd, **kwargs):
            record["cmd"] = cmd
            record["kwargs"] = kwargs
            return _FakeProcess()

        async def _fake_wait(_self, _port):
            return "ws://127.0.0.1:9222/devtools/browser/x"

        monkeypatch.setattr(
            local_chrome.asyncio, "create_subprocess_exec", _fake_exec
        )
        monkeypatch.setattr(LocalChromeProvider, "_wait_for_cdp", _fake_wait)

        provider = LocalChromeProvider()
        provider.executable_path = "/fake/chrome"
        provider.user_data_dir = str(tmp_path)

        session = await provider.open()

        assert session.cdp_url is not None
        assert session.cdp_url.startswith("ws://")
        assert record["kwargs"]["stdout"] is asyncio.subprocess.DEVNULL
        assert record["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL

    async def test_an_undrained_pipe_stalls_a_chatty_child(self) -> None:
        """The reason the assertion above is not cargo cult.

        1 MiB is comfortably past the ~64 KiB kernel pipe buffer, so a child
        writing that much to an unread pipe blocks partway through and never
        exits. Chrome is exactly that chatty on stderr over a long session.
        """
        chatty = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * (1 << 20)); sys.stderr.flush()",
        ]

        piped = await asyncio.create_subprocess_exec(
            *chatty,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(piped.wait(), timeout=1.0)
        # Drain so the child can finish and the transport closes on EOF —
        # abandoning it here would reproduce the very teardown noise this
        # module's fix removes.
        assert piped.stderr is not None
        await piped.stderr.read()
        await piped.wait()

        discarded = await asyncio.create_subprocess_exec(
            *chatty,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(discarded.wait(), timeout=30.0)
        assert discarded.returncode == 0
