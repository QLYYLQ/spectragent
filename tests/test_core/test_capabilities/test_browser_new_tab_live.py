"""Tabs a page opens, in a real Chrome: the recorder's new-tab preparation (§4.14).

A tab that cannot reach its opener — ``<a target=_blank>`` without ``rel``,
``rel=noopener``, ``window.open(url, '_blank', 'noopener')`` — starts paused
with no page process: Chrome answers only the commands it handles itself
until the tab runs, and applies the rest, queued in order, before its first
document's first script (measured, Chrome 155). These tests pin that as
canaries, and pin what the recorder makes of it, against the local fixture
site only (``tests/_helpers/fixture_site.py``, scenario ``new_tab``):

- Chrome itself: a no-opener tab answers ``Fetch.enable`` and
  ``Target.setAutoAttach`` while it waits, and ``Page.enable`` only after it
  runs;
- ``replayability: pinned`` reaches a page-opened tab's first document,
  whatever opened it (and a tab ``create_tab`` opened): the shim before any
  page script, the clock at T0, the seeded randomness, time zone, locale,
  language, user agent, no brands, reduced motion — in both documents, the
  worker and the cross-site frame; the document requests carry the pinned
  ``Accept-Language`` and user agent and ``no-cache``, and are paused and
  archived. The one exception is a known limitation: a cross-site popup that
  keeps its opener reads the browser's default locale;
- the tab is not held by its preparation (its document is requested within
  300 ms of the click) and leaves no failed command behind, and a first
  response that takes 2 s is no failure either;
- ``Sec-CH-UA`` appears on a no-opener tab's first request only (a known
  limitation, the same at record and replay);
- a frame whose process lost the frame that set the overrides keeps the
  pinned time zone (``TZ`` at launch) and reads the browser's default locale,
  which the environment fingerprint records;
- what the OS says before launch is what Chrome falls back to.

The replay of such a tab is in ``test_browser_replay_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.archive import NetworkArchiver, StorageArchiver
from spectragent.core.capabilities.browser.backends.browser_defaults import (
    measure_browser_defaults,
)
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.recorder import (
    AUTO_ATTACH_PARAMS,
    RELEASE_AUTO_ATTACH_PARAMS,
)
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.schemas.action import Action
from tests._helpers.archive_bench import VISION_FRAGMENTS, LiveRecording
from tests._helpers.fixture_site import FixtureSite, LoggedRequest, new_tab_point

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180

#: Pinned values chosen apart from any likely host's, so each value shows where it came from.
PINNED_TZ = "America/Sao_Paulo"
PINNED_LOCALE = "fr-FR"
PINNED_UA = "PinnedAgent/1.0 (fixture)"
#: What Chrome sends for the pinned ``Accept-Language`` (``fr-FR,fr``: it adds the q-values).
PINNED_ACCEPT_LANGUAGE = "fr-FR,fr;q=0.9"

#: Ways of opening a tab the opener cannot reach: no page process until it runs.
NO_OPENER = ("blank", "blank_noopener", "blank_cross_site", "window_open_noopener")
#: Ways that keep the opener: the tab starts in the opener's process.
KEEPS_OPENER = ("blank_opener", "window_open", "window_open_cross_site")
#: Every way, and a tab the capability opens itself.
OPENERS = (*NO_OPENER, *KEEPS_OPENER, "create_tab")
#: Each opened tab reports twice per document (the document, its worker, its frame) x two documents.
REPORTS_PER_TAB = 6


# ── one capability, every way of opening a tab ────────────────────────


@dataclass
class Opened:
    """One way of opening a tab, as the fixture and the recorder saw it."""

    how: str
    clicked_at: float
    #: ``/new_tab/report`` requests of this tab: what each document, worker and frame saw.
    reports: list[LoggedRequest]
    #: Its ``target.html`` document requests, first first.
    documents: list[LoggedRequest]
    #: ``cacheable.js`` requests made while it was open.
    scripts: list[LoggedRequest]
    #: Recorder failures on its page sessions.
    failures: list[str]
    #: ``browser.network`` exchange lines of its documents.
    archived: list[dict[str, Any]]

    def probes(self) -> list[tuple[str, int, dict[str, Any]]]:
        return [
            (row.query["kind"], int(row.query["n"]), json.loads(row.query["d"]))
            for row in self.reports
        ]


@dataclass
class Session:
    """A pinned recording that opened a tab every way there is."""

    t0_ms: int
    #: What the opener page's own first script saw (the seeded stream's first values).
    opener_probe: dict[str, Any]
    #: The fingerprint's ``browser_defaults``: what a frame no override reaches reads.
    browser_defaults: dict[str, Any]
    opened: dict[str, Opened] = field(default_factory=dict)


def _rows(site: FixtureSite, mark: int, path: str, how: str) -> list[LoggedRequest]:
    return [
        row for row in site.requests[mark:] if row.bare_path == path and row.query.get("how") == how
    ]


def _run_record(batch: Any) -> dict[str, Any]:
    (record,) = [item for item in batch.files if item.scope == "run"]
    return json.loads(record.data)


def _exchanges(batch: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for item in batch.files:
        if item.archiver_id == "browser.network" and item.name == "index.jsonl":
            lines.extend(json.loads(line) for line in item.data.decode().splitlines())
    return [line for line in lines if line.get("kind") == "exchange"]


async def _until(predicate: Any, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


async def _open(rec: LiveRecording, how: str, *, reports: int) -> Opened:
    cap, site = rec.capability, rec.site
    recorder = cap._recorder
    assert recorder is not None
    await rec.front()
    loaded = await cap.execute_action(
        Action(name="browser.navigate", params={"url": site.url("new_tab")})
    )
    assert loaded.success, loaded.error
    known = set(recorder.targets())
    mark = len(site.requests)
    clicked_at = time.monotonic()
    created: str | None = None
    if how == "create_tab":
        url = f"{site.main_origin}/new_tab/target.html?how=create_tab&n=1&next=1&sub=1"
        created = await cap.create_tab(url)
    else:
        x, y = new_tab_point(how)
        clicked = await cap.execute_action(
            Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y})
        )
        assert clicked.success, clicked.error
    await _until(lambda: len(_rows(site, mark, "/new_tab/report", how)) >= reports, 15)
    await asyncio.sleep(0.5)  # the last replies, and any failure, land
    pages = {
        sid: target
        for sid, target in recorder.targets().items()
        if sid not in known and target.type == "page"
    }
    batch = await cap.collect_archive()
    opened = Opened(
        how=how,
        clicked_at=clicked_at,
        reports=_rows(site, mark, "/new_tab/report", how),
        documents=sorted(
            _rows(site, mark, "/new_tab/target.html", how), key=lambda row: row.received_at
        ),
        scripts=[row for row in site.requests[mark:] if row.bare_path == "/new_tab/cacheable.js"],
        failures=[failure for sid in pages for failure in recorder.failures.get(sid, [])],
        archived=[
            line for line in _exchanges(batch) if f"/new_tab/target.html?how={how}&" in line["url"]
        ],
    )
    if created is not None:
        await cap.close_tab(created)
    else:
        for target in {t.target_id for t in pages.values()}:
            await cap.cdp.send("Target.closeTarget", {"targetId": target}, timeout=5)
    await asyncio.sleep(0.2)
    return opened


@pytest.fixture(scope="module")
def session() -> Session:
    async def _all() -> Session:
        pinned = PinnedReplayability(
            random_seed=7, timezone=PINNED_TZ, locale=PINNED_LOCALE, user_agent=PINNED_UA
        )
        with FixtureSite() as site:
            archiver = NetworkArchiver(drain_timeout_s=1.0)
            async with LiveRecording(
                site, archivers=(archiver,), replayability=pinned, fragments=VISION_FRAGMENTS
            ) as rec:
                cap = rec.capability
                assert cap._replay_plan is not None and cap._replay_plan.t0_ms is not None
                defaults = _run_record(await cap.collect_archive())["browser_defaults"]
                loaded = await cap.execute_action(
                    Action(name="browser.navigate", params={"url": site.url("new_tab")})
                )
                assert loaded.success, loaded.error
                reply = await cap.cdp.send(
                    "Runtime.evaluate",
                    {"expression": "JSON.stringify(window.__probe)", "returnByValue": True},
                    session_id=cap.default_session_id,
                    timeout=5,
                )
                result = Session(
                    t0_ms=cap._replay_plan.t0_ms,
                    opener_probe=json.loads(reply["result"]["value"]),
                    browser_defaults=defaults,
                )
                for how in OPENERS:
                    result.opened[how] = await _open(rec, how, reports=REPORTS_PER_TAB)
                result.opened["slow"] = await _open(rec, "slow", reports=1)
                return result

    return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))


def _expected_locale(session: Session, how: str, kind: str) -> str:
    """The pinned locale — except where a frame reads the browser's default (a known limitation).

    A cross-site popup that keeps its opener is prepared while it still
    shares its opener's process, where the opener owns the locale, and its
    own document then starts in a new process no locale override reached
    (measured, Chrome 155). Its frame, on the opener's site, is pinned.
    """
    if how == "window_open_cross_site" and kind != "iframe":
        return str(session.browser_defaults["locale"])
    return PINNED_LOCALE


@pytest.mark.parametrize("how", OPENERS)
def test_pinned_reaches_a_page_opened_tabs_first_document(session: Session, how: str) -> None:
    opened = session.opened[how]
    probes = opened.probes()
    assert sorted((kind, n) for kind, n, _ in probes) == [
        ("document", 1),
        ("document", 2),
        ("iframe", 1),
        ("iframe", 2),
        ("worker", 1),
        ("worker", 2),
    ], f"{how}: not every document, worker and frame reported"
    for kind, n, probe in probes:
        where = f"{how}: {kind} of document {n}"
        assert probe["shim"] is True, where
        if kind != "worker":
            assert probe["scripts"] == 1, f"{where}: a page script ran before the first one"
            assert probe["reduced"] is True, where
        assert probe["dates"][0] == session.t0_ms, where
        assert probe["random"] == session.opener_probe["random"], where
        assert probe["tz"] == PINNED_TZ, where
        assert probe["locale"] == _expected_locale(session, how, kind), where
        assert probe["language"] == PINNED_LOCALE, where
        assert probe["ua"] == PINNED_UA, where
        assert probe["brands"] == [], where
    assert [row.query["n"] for row in opened.documents] == ["1", "2"]
    for document in opened.documents:
        where = f"{how}: document {document.query['n']}"
        assert document.header("accept-language") == PINNED_ACCEPT_LANGUAGE, where
        assert document.header("user-agent") == PINNED_UA, where
        assert document.header("cache-control") == "no-cache", where
    assert opened.scripts and all(s.header("cache-control") == "no-cache" for s in opened.scripts)
    first = [line for line in opened.archived if "&n=1" in line["url"]]
    assert first and first[0]["body_status"] == "ok", f"{how}: first document not archived"


@pytest.mark.parametrize("how", OPENERS)
def test_a_page_opened_tab_is_not_held_by_its_preparation(session: Session, how: str) -> None:
    opened = session.opened[how]
    waited = opened.documents[0].received_at - opened.clicked_at
    assert waited < 0.3, f"{how}: the first document was requested {waited:.3f}s after the click"
    assert not [f for f in opened.failures if "no reply" in f], opened.failures
    if how in KEEPS_OPENER:
        # Genuine: the tab starts in its opener's process, whose locale the opener owns.
        assert all(
            f.startswith("Emulation.setLocaleOverride:") and "already in effect" in f
            for f in opened.failures
        ), opened.failures
    else:
        assert opened.failures == []


def test_a_slow_first_response_is_not_a_preparation_failure(session: Session) -> None:
    """The document answers after 2 s: the tab's replies come after it, and none is a failure."""
    opened = session.opened["slow"]
    assert opened.failures == []
    ((kind, n, probe),) = opened.probes()
    assert (kind, n) == ("document", 1)
    assert probe["shim"] is True and probe["dates"][0] == session.t0_ms
    assert (probe["tz"], probe["locale"], probe["ua"]) == (PINNED_TZ, PINNED_LOCALE, PINNED_UA)


def test_sec_ch_ua_only_on_a_no_opener_tabs_first_request(session: Session) -> None:
    """A known limitation (§4.14): computed before the tab is attached, the same at record and replay."""
    for how in OPENERS:
        first, second = session.opened[how].documents
        assert second.header("sec-ch-ua") is None, how
        if how in NO_OPENER:
            assert first.header("sec-ch-ua") is not None, how
        else:
            assert first.header("sec-ch-ua") is None, how


def test_the_browser_defaults_are_recorded(session: Session) -> None:
    """``TZ`` at launch makes the pinned zone the browser's default; the locale is the host's."""
    assert session.browser_defaults["timezone"] == PINNED_TZ
    assert session.browser_defaults["locale"]


# ── Chrome itself: what a no-opener tab answers while it waits ────────


@asynccontextmanager
async def _chrome() -> AsyncIterator[str]:
    backend = LocalChromeProvider(headless=True)
    launched = await backend.open()
    try:
        assert launched.cdp_url is not None
        yield launched.cdp_url
    finally:
        await backend.close(launched)


async def test_a_tab_its_opener_cannot_reach_answers_only_after_resume() -> None:
    """Chrome canary: browser-side commands answer while the tab waits; page-side ones after it runs."""

    async def _run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                control = CDPClient(url, proxy=None, compression=None)
                waiter = CDPClient(url, proxy=None, compression=None, max_frame_size=None)
                await control.start()
                await waiter.start()
                popup: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                others: set[asyncio.Task[Any]] = set()

                def _attached(params: dict[str, Any], _sid: str | None) -> None:
                    if not params.get("waitingForDebugger"):
                        return
                    info = params.get("targetInfo") or {}
                    if info.get("type") == "page" and not popup.done():
                        popup.set_result(params)
                        return
                    # Anything else must not stay waiting on this test's account.
                    task = asyncio.ensure_future(
                        waiter.send(
                            "Runtime.runIfWaitingForDebugger", None, params["sessionId"], timeout=5
                        )
                    )
                    others.add(task)

                waiter.on_event("Target.attachedToTarget", _attached)
                try:
                    targets = await control.send("Target.getTargets", {}, timeout=5)
                    page = next(t for t in targets["targetInfos"] if t["type"] == "page")
                    tab = (
                        await control.send(
                            "Target.attachToTarget",
                            {"targetId": page["targetId"], "flatten": True},
                            timeout=5,
                        )
                    )["sessionId"]
                    loads: list[str] = []
                    control.on_event(
                        "Page.loadEventFired",
                        lambda _p, sid: loads.append(sid) if sid == tab else None,
                    )
                    await control.send("Page.enable", {}, tab, timeout=5)
                    await control.send(
                        "Page.navigate", {"url": site.url("new_tab")}, tab, timeout=5
                    )
                    assert await _until(lambda: bool(loads), 10)
                    await waiter.send("Target.setAutoAttach", dict(AUTO_ATTACH_PARAMS), timeout=5)
                    x, y = new_tab_point("blank")
                    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                        await control.send(
                            "Input.dispatchMouseEvent",
                            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                            tab,
                            timeout=5,
                        )
                    params = await asyncio.wait_for(popup, 10)
                    sid = params["sessionId"]
                    assert params["targetInfo"].get("canAccessOpener") is False

                    nothing = {"patterns": [{"urlPattern": "*://never.invalid/*"}]}
                    fetch = await waiter.post("Fetch.enable", nothing, sid)
                    child = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
                    auto = await waiter.post("Target.setAutoAttach", child, sid)
                    page_enable = asyncio.ensure_future(
                        (await waiter.post("Page.enable", {}, sid)).wait(None)
                    )
                    assert await fetch.wait(2.0) == {}, "Fetch.enable answers while the tab waits"
                    assert await auto.wait(2.0) == {}, "Target.setAutoAttach too"
                    done, _ = await asyncio.wait({page_enable}, timeout=2.0)
                    assert not done, "Page.enable answered before the tab ran"
                    resume = await waiter.post("Runtime.runIfWaitingForDebugger", None, sid)
                    assert await asyncio.wait_for(page_enable, 10) == {}
                    assert await resume.wait(10) == {}
                finally:
                    try:
                        await waiter.send(
                            "Target.setAutoAttach", dict(RELEASE_AUTO_ATTACH_PARAMS), timeout=5
                        )
                    finally:
                        for task in others:
                            task.cancel()
                        await waiter.stop()
                        await control.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


# ── time zone and locale ownership ────────────────────────────────────


@dataclass
class OwnerLoss:
    """What frames read before and after the frame that set their process's overrides left."""

    browser_defaults: dict[str, Any]
    before: dict[str, LoggedRequest]
    after: dict[str, LoggedRequest]


def _latest(site: FixtureSite, tag: str, since: float) -> LoggedRequest | None:
    rows = [
        row
        for row in site.requests
        if row.bare_path == "/new_tab/owner_report"
        and row.query.get("tag") == tag
        and row.received_at >= since
    ]
    return rows[-1] if rows else None


@pytest.fixture(scope="module")
def owner_loss() -> OwnerLoss:
    async def _run() -> OwnerLoss:
        pinned = PinnedReplayability(random_seed=7, timezone=PINNED_TZ, locale=PINNED_LOCALE)
        with FixtureSite() as site:
            async with LiveRecording(
                site,
                archivers=(StorageArchiver(),),
                replayability=pinned,
                fragments=VISION_FRAGMENTS,
            ) as rec:
                cap = rec.capability
                defaults = _run_record(await cap.collect_archive())["browser_defaults"]
                loaded = await cap.execute_action(
                    Action(
                        name="browser.navigate",
                        params={"url": f"{site.main_origin}/new_tab/owners.html"},
                    )
                )
                assert loaded.success, loaded.error
                start = time.monotonic()
                # Two frames of one other site share a process; the first owns its overrides.
                assert await _until(lambda: _latest(site, "frame_b", start) is not None, 10)
                await asyncio.sleep(0.6)
                before = {"frame_b": _latest(site, "frame_b", start)}
                await cap.cdp.send(
                    "Runtime.evaluate",
                    {"expression": "document.getElementById('frame_a').remove(); 1"},
                    session_id=cap.default_session_id,
                    timeout=5,
                )
                removed = time.monotonic()
                await asyncio.sleep(1.5)
                after = {"frame_b": _latest(site, "frame_b", removed + 0.8)}

                # A same-site popup that keeps its opener shares the opener's process.
                clicked = await cap.execute_action(
                    Action(
                        name="browser.click_coordinate",
                        params={"coordinate_x": 170, "coordinate_y": 220},
                    )
                )
                assert clicked.success, clicked.error
                opened = time.monotonic()
                assert await _until(lambda: _latest(site, "popup", opened) is not None, 10)
                await asyncio.sleep(0.6)
                before["popup"] = _latest(site, "popup", opened)
                # The opener moves to the other site: its frame leaves the popup's process.
                await rec.front()
                moved = await cap.execute_action(
                    Action(
                        name="browser.navigate",
                        params={"url": f"{site.cross_origin}/new_tab/owner.html?tag=opener"},
                    )
                )
                assert moved.success, moved.error
                moved_at = time.monotonic()
                await asyncio.sleep(1.5)
                after["popup"] = _latest(site, "popup", moved_at + 0.8)
                missing = [k for k, v in {**before, **after}.items() if v is None]
                assert not missing, f"no report from {missing}"
                return OwnerLoss(
                    browser_defaults=defaults,
                    before={k: v for k, v in before.items() if v is not None},
                    after={k: v for k, v in after.items() if v is not None},
                )

    return asyncio.run(asyncio.wait_for(_run(), _TIMEOUT_S))


@pytest.mark.parametrize("who", ["frame_b", "popup"])
def test_the_time_zone_stays_pinned_when_the_owner_frame_leaves(
    owner_loss: OwnerLoss, who: str
) -> None:
    """``TZ`` at launch: the zone a frame falls back to is the pinned one (§4.14)."""
    before, after = owner_loss.before[who].query, owner_loss.after[who].query
    assert (before["tz"], before["locale"]) == (PINNED_TZ, PINNED_LOCALE), "the owner was present"
    assert after["tz"] == PINNED_TZ
    assert after["language"] == PINNED_LOCALE, "navigator.language is set per session"


@pytest.mark.parametrize("who", ["frame_b", "popup"])
def test_the_locale_falls_back_to_the_recorded_browser_default(
    owner_loss: OwnerLoss, who: str
) -> None:
    """A known limitation, covered by the fingerprint: the fallback is exactly what it records."""
    assert owner_loss.after[who].query["locale"] == owner_loss.browser_defaults["locale"]
    assert owner_loss.browser_defaults["timezone"] == PINNED_TZ


# ── the OS facts read before launch are Chrome's fallback ─────────────


async def test_the_os_facts_are_what_chrome_falls_back_to() -> None:
    """Chrome canary for the replay's check before launch (``browser_defaults.py``)."""

    async def _run() -> None:
        for env in ({}, {"TZ": PINNED_TZ}):
            provider = LocalChromeProvider(headless=True)
            expected = provider.planned_browser_defaults(env)
            launched = await provider.open(launch_env=env)
            try:
                assert launched.cdp_url is not None
                client = CDPClient(launched.cdp_url, proxy=None, compression=None)
                await client.start()
                try:
                    measured = await measure_browser_defaults(client, timeout=5)
                finally:
                    await client.stop()
            finally:
                await provider.close(launched)
            assert expected.timezone is not None, env
            assert measured.timezone == expected.timezone, env
            if sys.platform == "darwin":
                assert expected.locale is not None
            if expected.locale is not None:
                assert measured.locale == expected.locale, env

    await asyncio.wait_for(_run(), _TIMEOUT_S)
