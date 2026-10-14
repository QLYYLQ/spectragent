"""A pinned Chrome identifier and the browser's own client hints, in a real Chrome (§4.14).

With ``replayability: pinned`` and a normal Chrome identifier of the
installed Chrome's version, a page reads ``navigator.userAgentData`` brands
of that version and every request carries the matching ``Sec-CH-UA`` — at
record and at replay, the first request of a tab that cannot reach its opener
included. Before, the override emptied ``navigator.userAgentData`` and sent
no ``Sec-CH-UA`` but on that one first request (``client_hints.py``).

Against the local fixture site only, served from ``127.0.0.1``: Chrome
exposes ``navigator.userAgentData`` and sends ``Sec-CH-UA`` to a secure
context only.

- **Record**: the real runtime with a scripted model, in the recording shape
  (``replay_driver.record_config``: pinned, the three archivers), opening the
  ``client_hints`` page, then a plain ``target=_blank`` link (a tab that
  cannot reach its opener), then done. The fixture logs each request's
  ``Sec-CH-UA*`` headers, the pages report what they read, and the
  fingerprint records the hints the override carried.
- **Replay**: the recording's own configuration with the archive network
  source, at the capability, with a ``Network`` observer on the agent's tab:
  the page is served from the archive, reads the same brands, its requests
  carry the same headers, and the replay's fingerprint records the same hints.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from spectragent.core.capabilities.browser.archive import ReplayLogArchiver
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.client_hints import ClientHints
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import FixtureSite, LoggedRequest, new_tab_point
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
TASK = "Read the page."
#: The identifier's platform token per host, and what Chrome reports as its platform there.
PLATFORMS = {
    "darwin": ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    "win32": ("Windows NT 10.0; Win64; x64", "Windows"),
}
PLATFORM_TOKEN, PLATFORM = PLATFORMS.get(sys.platform, ("X11; Linux x86_64", "Linux"))


def _identifier(major: str) -> str:
    return (
        f"Mozilla/5.0 ({PLATFORM_TOKEN}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


async def _installed_major() -> str:
    backend = LocalChromeProvider(headless=True)
    session = await backend.open()
    try:
        assert session.cdp_url is not None
        client = CDPClient(session.cdp_url, proxy=None, compression=None)
        await client.start()
        try:
            version = await client.send("Browser.getVersion", {}, timeout=10)
        finally:
            await client.stop()
    finally:
        await backend.close(session)
    return str(version["product"]).split("/", 1)[1].split(".", 1)[0]


@dataclass
class Replay:
    """What the replay's page read, its requests' headers and its fingerprint."""

    page: dict[str, Any]
    headers: dict[str, dict[str, str]]
    fingerprint: dict[str, Any]


@dataclass
class Flow:
    major: str
    identifier: str
    source: driver.Outcome
    requests: list[LoggedRequest]
    fingerprint: dict[str, Any]
    replay: Replay

    @property
    def hints(self) -> ClientHints:
        hints = ClientHints.from_record(self.fingerprint["replayability"]["client_hints"])
        assert hints is not None, "the recording's override carried no client hints"
        return hints

    def rows(self, path: str, **query: str) -> list[LoggedRequest]:
        return [
            row
            for row in self.requests
            if row.bare_path == path and all(row.query.get(k) == v for k, v in query.items())
        ]


def _fingerprint(run_dir: Path) -> dict[str, Any]:
    from spectragent.core.trace.reader import RunTrace

    trace = RunTrace(run_dir)
    record = trace.archive_run_file("browser")
    assert record is not None, "no environment fingerprint"
    return json.loads(trace.read_extra(record.path))


async def _replay(raw: dict[str, Any], site: FixtureSite, source: Path, llm: Any) -> Replay:
    """The recording's configuration replaying it, at the capability, observed from the side."""
    replay_raw = driver.to_replay_config(raw, llm=llm, trace_dir=source.parent / "unused")
    run = ConfigComposer(registrars=_collect_registrars()).compose(
        {"environment": replay_raw["environment"]}
    )
    env = run.single("environment")
    cap = next(iter(env.capabilities.values()))
    cap.bind_archivers([ReplayLogArchiver()])
    await env.setup({"replay_archive": str(source)}, negotiation={"browser.browser_meta"})
    try:
        headers: dict[str, dict[str, str]] = {}

        def sent(params: dict[str, Any], _session: str | None = None) -> None:
            request = params.get("request") or {}
            path = urlsplit(str(request.get("url", ""))).path
            headers.setdefault(
                path, {k.lower(): v for k, v in (request.get("headers") or {}).items()}
            )

        cap.cdp.on_event("Network.requestWillBeSent", sent)
        await cap.cdp.send("Network.enable", {}, session_id=cap.default_session_id, timeout=5)
        loaded = await cap.execute_action(
            Action(name="browser.navigate", params={"url": site.url("client_hints")})
        )
        assert loaded.success, loaded.error
        for _ in range(50):
            if "/client_hints/report" in headers:
                break
            await asyncio.sleep(0.1)
        reply = await cap.cdp.send(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({text: document.getElementById('hints').textContent,"
                " brands: navigator.userAgentData && navigator.userAgentData.brands,"
                " platform: navigator.userAgentData && navigator.userAgentData.platform})",
                "returnByValue": True,
            },
            session_id=cap.default_session_id,
            timeout=5,
        )
        batch = await cap.collect_archive()
        (record,) = [item for item in batch.files if item.scope == "run"]
        return Replay(
            page=json.loads(reply["result"]["value"]),
            headers=headers,
            fingerprint=json.loads(record.data),
        )
    finally:
        await env.teardown()


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted model is on loopback; the machine's proxy must not see it.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite() as fixture:
            yield fixture


@pytest.fixture(scope="module")
def flow(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Flow:
    base = tmp_path_factory.mktemp("client_hints")
    major = asyncio.run(asyncio.wait_for(_installed_major(), 60))
    identifier = _identifier(major)
    script = [
        driver.navigate(site.url("new_tab")),
        driver.click_at(*new_tab_point("blank")),
        driver.done("read"),
    ]
    mark = len(site.requests)
    with ScriptedLLMServer(script) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "traces")
        raw["environment"][0]["capabilities"][0]["replayability"]["user_agent"] = identifier
        source = asyncio.run(
            driver.run_task(raw, task=TASK, start_url=site.url("client_hints"), timeout=_TIMEOUT_S)
        )
        assert llm.errors == []
        source.require_finished(3, "read")
        requests = list(site.requests[mark:])
        replay = asyncio.run(asyncio.wait_for(_replay(raw, site, source.run_dir, llm), _TIMEOUT_S))
    return Flow(
        major=major,
        identifier=identifier,
        source=source,
        requests=requests,
        fingerprint=_fingerprint(source.run_dir),
        replay=replay,
    )


def _reported(row: LoggedRequest) -> dict[str, Any]:
    return json.loads(parse_qs(urlsplit(row.path).query)["d"][0])


def test_the_hints_are_the_browser_s_own_of_the_identifier_s_version(flow: Flow) -> None:
    hints = flow.hints
    assert hints.major == flow.major
    assert (hints.platform, hints.mobile) == (PLATFORM, False)
    assert f'"Chromium";v="{flow.major}"' in hints.header()
    assert flow.fingerprint["replayability"]["params"]["user_agent"] == flow.identifier
    assert flow.fingerprint["page"]["userAgentData"] == hints.low_entropy()


def test_at_record_the_page_reads_them_and_its_requests_carry_them(flow: Flow) -> None:
    hints = flow.hints
    (document,) = flow.rows("/client_hints/page.html")
    (report,) = flow.rows("/client_hints/report")
    for row in (document, report):
        assert row.header("user-agent") == flow.identifier
        assert row.header("sec-ch-ua") == hints.header(), row.path
        assert row.header("sec-ch-ua-mobile") == "?0"
        assert row.header("sec-ch-ua-platform") == f'"{PLATFORM}"'
    assert _reported(report) == {
        "brands": [f"{brand}/{version}" for brand, version in hints.brands],
        "mobile": False,
        "platform": PLATFORM,
    }


def test_a_tab_that_cannot_reach_its_opener_carries_them_from_its_first_request(
    flow: Flow,
) -> None:
    """Chrome sends its own hints on that request before the tab is attached; they are the same."""
    hints = flow.hints
    documents = flow.rows("/new_tab/target.html", how="blank")
    assert [row.query["n"] for row in documents] == ["1", "2"]
    for row in documents:
        assert row.header("sec-ch-ua") == hints.header(), row.path
        assert row.header("user-agent") == flow.identifier
    reports = flow.rows("/new_tab/report", how="blank")
    assert reports, "the new tab's pages reported nothing"
    for row in reports:
        probe = json.loads(row.query["d"])
        assert probe["brands"] == [brand for brand, _version in hints.brands], probe


def test_at_replay_the_page_reads_the_same_and_its_requests_carry_the_same(flow: Flow) -> None:
    hints = flow.hints
    replay = flow.replay
    assert replay.page["brands"] == [
        {"brand": brand, "version": version} for brand, version in hints.brands
    ]
    assert replay.page["platform"] == PLATFORM
    assert replay.page["text"] == (
        "brands: "
        + ", ".join(f"{brand}/{version}" for brand, version in hints.brands)
        + f"; platform: {PLATFORM}; mobile: false"
    )
    for path in ("/client_hints/page.html", "/client_hints/report"):
        sent = replay.headers[path]
        assert sent["sec-ch-ua"] == hints.header(), path
        assert sent["sec-ch-ua-mobile"] == "?0"
        assert sent["sec-ch-ua-platform"] == f'"{PLATFORM}"'
        assert sent["user-agent"] == flow.identifier


def test_the_replay_records_the_same_hints(flow: Flow) -> None:
    replayed = flow.replay.fingerprint
    assert replayed["network_source"]["source_id"] == "archive"
    assert (
        replayed["replayability"]["client_hints"]
        == flow.fingerprint["replayability"]["client_hints"]
    )
    assert replayed["page"]["userAgentData"] == flow.fingerprint["page"]["userAgentData"]
