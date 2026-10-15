"""Measured themes survive replay on another startup theme, including new targets (§4.14).

Only a local HTTP fixture and owned headless Chrome processes. A test-only
provider launch hook uses Chrome switches to simulate the OS theme without
changing the user's global appearance. Those simulation switches are omitted
from this double's launch metadata, as a real OS appearance change has no
launch flag. Production flag consistency remains unchanged.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import NetworkArchiver, ReplayLogArchiver
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.color_scheme import measure_color_scheme
from spectragent.core.capabilities.browser.replay import ArchiveNetworkSource, PinnedReplayability
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.trace.encoding import encode_archive_file
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from tests._helpers.trace_builder import agent_result, new_wal, step_record

pytestmark = pytest.mark.requires_chrome

MEDIA = """({dark: matchMedia('(prefers-color-scheme: dark)').matches,
    light: matchMedia('(prefers-color-scheme: light)').matches,
    reduced: matchMedia('(prefers-reduced-motion: reduce)').matches})"""


class ThemeSite:
    def __init__(self) -> None:
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests += 1
                frame = (
                    f'<iframe src="{owner.url("/frame", frame=True)}"></iframe>'
                    if self.path == "/"
                    else ""
                )
                body = (
                    f"<!doctype html><html><head><script>window.firstMedia = {MEDIA};</script>"
                    "<style>body{background:white;color:black}"
                    "@media(prefers-color-scheme:dark){body{background:black;color:white}}"
                    f"</style></head><body>{self.path}{frame}</body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, path: str, *, frame: bool = False) -> str:
        return f"http://{'frame.test' if frame else 'theme.test'}:{self.server.server_port}{path}"

    def __enter__(self) -> ThemeSite:
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _host_browser(host_theme: str, *, replay: bool, observed: list[str]) -> LocalChromeProvider:
    args = [] if replay else ["--no-proxy-server", "--host-resolver-rules=MAP *.test 127.0.0.1"]
    provider = LocalChromeProvider(headless=True, extra_args=args)
    original = provider.open
    simulation = (
        "--force-dark-mode" if host_theme == "dark" else "--blink-settings=preferredColorScheme=1"
    )

    async def open_with_host_theme(**kwargs: Any) -> Any:
        kwargs["launch_args"] = (*kwargs.get("launch_args", ()), simulation)
        session = await original(**kwargs)
        # The startup fact is read from real Chrome before the capability's
        # own measurement or any target preparation, not mocked as a value.
        assert session.cdp_url is not None
        client = CDPClient(session.cdp_url, proxy=None, compression=None)
        try:
            await client.start()
            observed.append(await measure_color_scheme(client, timeout=5))
        except BaseException:
            await provider.close(session)
            raise
        finally:
            await client.stop()
        session.metadata["launch_flags"] = [
            flag for flag in session.metadata["launch_flags"] if flag != simulation
        ]
        return session

    provider.open = open_with_host_theme  # type: ignore[method-assign]
    return provider


async def _pages(cap: BrowserCapability, expected: set[str]) -> dict[str, dict[str, Any]]:
    assert cap._recorder is not None
    deadline = asyncio.get_running_loop().time() + 10
    pages: dict[str, dict[str, Any]] = {}
    while asyncio.get_running_loop().time() < deadline:
        for sid, target in cap._recorder.targets().items():
            if target.type not in ("page", "iframe"):
                continue
            reply = await cap._recorder.send(
                "Runtime.evaluate",
                {
                    "expression": f"JSON.stringify({{url:location.href, first:window.firstMedia, now:{MEDIA}}})",
                    "returnByValue": True,
                },
                sid,
                timeout=3,
            )
            page = json.loads(reply["result"]["value"])
            if page.get("first") is not None and page["url"] in expected:
                pages[page["url"]] = {**page, "target_type": target.type}
        if set(pages) == expected:
            return pages
        await asyncio.sleep(0.05)
    raise AssertionError(f"missing loaded targets: {expected - set(pages)}")


async def _flow(tmp_path: Path, site: ThemeSite, source_theme: str, host_theme: str) -> None:
    observations: list[str] = []
    expected = {site.url("/"), site.url("/frame", frame=True), site.url("/new")}
    source_pages = None
    source = tmp_path / "recorded"
    requests_after_record = 0
    for replay in (False, True):
        cap = BrowserCapability()
        cap.backend = _host_browser(
            host_theme if replay else source_theme, replay=replay, observed=observations
        )
        cap.replayability = PinnedReplayability(base_time="from_archive" if replay else "run_start")
        if replay:
            cap.network_source = ArchiveNetworkSource(
                matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
            )
        cap.bind_archivers([ReplayLogArchiver()] if replay else [NetworkArchiver()])
        try:
            await cap.setup(start_url=site.url("/"), replay_archive=str(source) if replay else None)
            await cap.create_tab(site.url("/new"))
            pages = await _pages(cap, expected)
            wanted = {
                "dark": source_theme == "dark",
                "light": source_theme == "light",
                "reduced": True,
            }
            assert all(p["first"] == p["now"] == wanted for p in pages.values())
            assert pages[site.url("/frame", frame=True)]["target_type"] == "iframe", (
                "fixture must be OOPIF"
            )
            batch = await cap.collect_archive()
            fingerprint = next(f for f in batch.files if f.archiver_id == "browser")
            assert json.loads(fingerprint.data)["replayability"]["color_scheme"] == source_theme
            if not replay:
                wal = new_wal(tmp_path, "recorded")
                files = [
                    encode_archive_file(f, run_version=0)
                    if f.scope == "run"
                    else encode_archive_file(f, step=0)
                    for f in batch.files
                ]
                wal.commit_step(0, step_record(0), extra_files=files)
                wal.write_result(agent_result())
                wal.finalize(TraceStatus.COMPLETE)
                assert RunTrace(source).archive_run_file("browser") is not None
                source_pages = pages
                requests_after_record = site.requests
            else:
                assert pages == source_pages
                assert site.requests == requests_after_record, "offline replay reached live fixture"
        finally:
            await cap.teardown()
    assert observations == [source_theme, host_theme], "the two startup themes really differed"


@pytest.mark.parametrize("source_theme,host_theme", [("dark", "light"), ("light", "dark")])
async def test_recorded_theme_controls_replay_new_tabs_and_oopif(
    tmp_path: Path, source_theme: str, host_theme: str
) -> None:
    with ThemeSite() as site:
        await asyncio.wait_for(_flow(tmp_path, site, source_theme, host_theme), timeout=120)
