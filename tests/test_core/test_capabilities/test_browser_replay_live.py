"""Offline replay in a real Chrome, end to end (M30 acceptance; fixture only, no LLM).

Each run is the real Composer and runtime driving ``browser_use`` (``dom_vision``)
with a loopback scripted model — ``tests/_helpers/replay_driver.py``:

- **the flow** — recorded once against the mapped fixture site (a redirect
  chain, a form POST with a password, a JSON POST with a token, GraphQL, a
  cross-site iframe, a dedicated worker, a popup, Range video, a beacon, the
  pinned clock), then replayed from its archive twice with ``base_time:
  from_archive`` and once with ``run_start``;
- **the strict flow** — a page that asks hosts a hermetic recording cannot
  reach (a sub-frame document, a beacon, a subresource) and fetches a body
  over the 64 MiB limit (``too_large``), the canary, bodies over the inline
  tier (streamed: ``streamed_later``), a WebSocket and a main-frame
  navigation to an unreachable host — recorded, then replayed;
- **the egress page** — the fixture servers reached by IP literal: a
  ``<link rel=preconnect>``, a script and a WebSocket — recorded, replayed
  isolated, and replayed once more launched without the blackhole
  (``isolation: none_acknowledged``, the negative control);
- **the localhost page** — the same routes on exact ``localhost``, the name
  the blackhole once left out (``EXCLUDE localhost``): a recording made
  against a local site, replayed isolated with the fixture still up, and
  replayed once more without the blackhole (the negative control);
- **a tab the page opened** — a plain ``target=_blank`` link (the tab cannot
  reach its opener, so it starts without a page process), clicked by
  coordinates: its document and what its first script reports are served
  from the archive, and that script reads the recorded T0.

Every replay feeds the agent the source run's own recorded completions, so
the actions are the recording's step for step. The fixture servers count
TCP accepts and HTTP requests: an isolated replay must add neither (measured —
counting requests alone would miss preconnects). The pages live on mapped,
non-localhost names, and a replay drops the fixture's name mapping, so those
names have no route to the fixture with or without isolation: for them the
counters cannot move. The egress page's IP literals need no mapping — only
the isolation's blackhole (``MAP *`` covers IP literals, measured, arch §4.14) cuts them —
and the negative control shows the counters do move without it. The
localhost page's routes need none either: the blackhole cuts exact
``localhost`` too, while the CDP connection — which the DevTools server takes
in, not a connection the page makes — keeps driving the replay.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from warcio.archiveiterator import ArchiveIterator

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLogEntry,
    diverged_steps,
    iter_replay_log,
    replay_log,
)
from spectragent.core.trace.digest import TreeDigest, tree_digest
from tests._helpers import replay_driver as driver
from tests._helpers.fixture_site import (
    CANARY_HOST,
    FIXTURE_PASSWORD,
    FIXTURE_TOKEN,
    FixtureSite,
    new_tab_point,
)
from tests._helpers.scripted_llm import ScriptedLLMServer

pytestmark = pytest.mark.requires_chrome

#: The flow, in order: the first page is the task's start_url, each later
#: one is a step's navigation; the last step also clicks, then the run ends.
FLOW = (
    "redirect",
    "post_form",
    "json_post",
    "graphql",
    "oopif",
    "worker",
    "popup",
    "range_video",
    "beacon",
    "clock",
)
GONE = "http://gone.example.test/"


@dataclass
class Replayed:
    """One replay and what the fixture servers saw while it ran (per server)."""

    outcome: driver.Outcome
    accepts_before: dict[str, int]
    accepts_after: dict[str, int]
    requests_before: dict[str, int]
    requests_after: dict[str, int]

    def log(self) -> list[tuple[int | None, ReplayLogEntry]]:
        return list(iter_replay_log(open_archive(self.outcome.run_dir)))


@dataclass
class Runs:
    """A recording and its replays."""

    source: driver.Outcome
    source_tree_before: TreeDigest
    replays: dict[str, Replayed]


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with pytest.MonkeyPatch.context() as patch:
        # The scripted model is on loopback; the machine's proxy must not see it.
        patch.setenv("NO_PROXY", "127.0.0.1,localhost")
        patch.setenv("no_proxy", "127.0.0.1,localhost")
        with FixtureSite(mapped=True) as fixture:
            yield fixture


def _requests(site: FixtureSite) -> dict[str, int]:
    """HTTP requests each fixture server has answered so far."""
    counts = dict.fromkeys(site.accepts(), 0)
    for request in site.requests:
        counts[request.server] += 1
    return counts


def _replay(
    site: FixtureSite,
    raw: dict[str, Any],
    source: driver.Outcome,
    trace_dir: Path,
    *,
    start_url: str,
    base_time: str = "from_archive",
    isolation: str = "required",
) -> Replayed:
    accepts, requests = site.accepts(), _requests(site)
    with ScriptedLLMServer(driver.recorded_completions(source.trace)) as llm:
        config = driver.to_replay_config(raw, llm=llm, trace_dir=trace_dir, isolation=isolation)
        capability = config["environment"][0]["capabilities"][0]
        capability["replayability"]["base_time"] = base_time
        outcome = asyncio.run(
            driver.run_task(
                config, task="Visit the pages.", start_url=start_url, replay_archive=source.run_dir
            )
        )
    # The replay acts exactly as recorded, so it ends where the recording did.
    outcome.require_finished(source.trace.step_count, "visited")
    return Replayed(outcome, accepts, site.accepts(), requests, _requests(site))


@pytest.fixture(scope="module")
def flow(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    base = tmp_path_factory.mktemp("flow")
    script = [driver.navigate(site.url(page)) for page in FLOW[1:]]
    script += [driver.click_at(640, 360), driver.done("visited")]
    with ScriptedLLMServer(script) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(
            driver.run_task(raw, task="Visit the pages.", start_url=site.url(FLOW[0]))
        )
    source.require_finished(len(FLOW) + 1, "visited")
    before = tree_digest(source.run_dir)
    start = site.url(FLOW[0])
    replays = {
        "first": _replay(site, raw, source, base / "replay-1", start_url=start),
        "second": _replay(site, raw, source, base / "replay-2", start_url=start),
        "run_start": _replay(
            site, raw, source, base / "replay-run-start", start_url=start, base_time="run_start"
        ),
    }
    return Runs(source, before, replays)


@pytest.fixture(scope="module")
def strict(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    base = tmp_path_factory.mktemp("strict")
    script = [
        driver.navigate(site.url("canary")),
        driver.navigate(site.url("large")),
        driver.navigate(site.url("websocket")),
        driver.navigate(GONE),
        driver.done("visited"),
    ]
    with ScriptedLLMServer(script) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(
            driver.run_task(raw, task="Visit the pages.", start_url=site.url("strict"))
        )
    source.require_finished(len(script), "visited")
    before = tree_digest(source.run_dir)
    replay = _replay(site, raw, source, base / "replay", start_url=site.url("strict"))
    return Runs(source, before, {"strict": replay})


@pytest.fixture(scope="module")
def egress(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    """The egress page: recorded, replayed isolated, and replayed without the blackhole."""
    base = tmp_path_factory.mktemp("egress")
    start = site.url("egress")
    with ScriptedLLMServer([driver.done("visited")]) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(driver.run_task(raw, task="Visit the page.", start_url=start))
    source.require_finished(1, "visited")
    before = tree_digest(source.run_dir)
    replays = {
        "isolated": _replay(site, raw, source, base / "replay", start_url=start),
        "unisolated": _replay(
            site,
            raw,
            source,
            base / "replay-unisolated",
            start_url=start,
            isolation="none_acknowledged",
        ),
    }
    return Runs(source, before, replays)


@pytest.fixture(scope="module")
def local(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    """The localhost page: recorded on localhost, replayed isolated and without the blackhole."""
    base = tmp_path_factory.mktemp("localhost")
    start = site.url("localhost")
    with ScriptedLLMServer([driver.done("visited")]) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(driver.run_task(raw, task="Visit the page.", start_url=start))
    source.require_finished(1, "visited")
    before = tree_digest(source.run_dir)
    replays = {
        "isolated": _replay(site, raw, source, base / "replay", start_url=start),
        "unisolated": _replay(
            site,
            raw,
            source,
            base / "replay-unisolated",
            start_url=start,
            isolation="none_acknowledged",
        ),
    }
    return Runs(source, before, replays)


@pytest.fixture(scope="module")
def new_tab(site: FixtureSite, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    """A ``target=_blank`` tab the page opened: recorded, then replayed from the archive."""
    base = tmp_path_factory.mktemp("new_tab")
    start = site.url("new_tab")
    with ScriptedLLMServer(
        [driver.click_at(*new_tab_point("replay")), driver.done("visited")]
    ) as llm:
        raw = driver.record_config(site=site, llm=llm, trace_dir=base / "record")
        source = asyncio.run(driver.run_task(raw, task="Open the tab.", start_url=start))
    source.require_finished(2, "visited")
    before = tree_digest(source.run_dir)
    replay = _replay(site, raw, source, base / "replay", start_url=start)
    return Runs(source, before, {"replay": replay})


def _localhost_urls(site: FixtureSite) -> tuple[str, str, str]:
    """The localhost page, its script and its WebSocket — all on exact ``localhost``."""
    return (
        site.url("localhost"),
        f"{site.localhost_origin}/localhost/local.js",
        f"ws://localhost:{site.ws_port}/localhost/echo",
    )


def _egress_urls(site: FixtureSite) -> tuple[str, str, str]:
    """The egress page, its script and its WebSocket — all by IP literal."""
    return (
        site.url("egress"),
        f"{site.ip_origin}/egress/ip.js",
        f"ws://127.0.0.1:{site.ws_port}/egress/echo",
    )


def _misses(replayed: Replayed) -> list[tuple[int | None, ReplayLogEntry]]:
    return [(step, entry) for step, entry in replayed.log() if entry.kind == "miss"]


# ── the same path ─────────────────────────────────────────────────────


def test_same_path_replay_matches_the_recording(flow: Runs) -> None:
    recorded = flow.source.trace
    replayed = flow.replays["first"].outcome
    assert flow.source.result.answer == replayed.result.answer == "visited"
    assert recorded.step_count == replayed.trace.step_count == len(FLOW) + 1
    assert driver.step_actions(replayed.trace) == driver.step_actions(recorded)
    expected = driver.step_signatures(recorded)
    actual = driver.step_signatures(replayed.trace)
    for step, (want, got) in enumerate(zip(expected, actual, strict=True)):
        assert got == want, f"step {step} observed something else in the replay"
    assert _misses(flow.replays["first"]) == [], "the flow replays without a single miss"
    hits = [entry for _, entry in flow.replays["first"].log() if entry.kind == "hit"]
    assert {entry.run_id for entry in hits} == {recorded.run_id}
    assert any(entry.method == "POST" and "/post_form/submit" in entry.url for entry in hits)


def test_posts_with_credentials_replay_while_the_archive_holds_none(flow: Runs) -> None:
    """The form's password and the JSON token are redacted in the archive, and still hit.

    Only the *request* records are searched: the pages themselves carry the
    planted values in their HTML (that is how the browser comes to send them).
    """
    trace = flow.source.trace
    bodies: list[bytes] = []
    for _step, record in trace.iter_archive("browser.network", include_trailing=True):
        if record.name == "exchanges.warc.gz":
            stream = io.BytesIO(trace.read_extra(record.path))
            for warc_record in ArchiveIterator(stream):
                if warc_record.rec_type == "request":
                    bodies.append(warc_record.content_stream().read())
    posted = [body for body in bodies if body]
    assert len(posted) >= 2, "the form POST and the JSON POST were archived with their bodies"
    assert all(FIXTURE_PASSWORD.encode() not in body for body in posted)
    assert all(FIXTURE_TOKEN.encode() not in body for body in posted)
    assert any(b"redacted" in body for body in posted)
    posts = [
        entry
        for _, entry in flow.replays["first"].log()
        if entry.method == "POST" and entry.kind == "hit"
    ]
    assert {"/post_form/submit", "/json_post/api"} <= {e.url.split("test", 1)[1] for e in posts}


def test_a_tab_the_page_opened_is_replayed_from_the_archive(new_tab: Runs) -> None:
    """Its document and its first script's report are hits, and that script read the recorded T0.

    The report's URL carries what the script saw (the clock, the seeded
    randomness, time zone, locale, user agent): an exact hit means the
    replayed tab's first document saw exactly what the recorded one did.
    """
    trace = new_tab.source.trace
    record = trace.archive_run_file("browser")
    assert record is not None, "the recording has an environment fingerprint"
    recorded_t0 = json.loads(trace.read_extra(record.path))["replayability"]["t0_ms"]
    entries = [entry for _, entry in new_tab.replays["replay"].log()]
    documents = [e for e in entries if "/new_tab/target.html?how=replay&" in e.url]
    assert [(e.kind, e.frame) for e in documents] == [("hit", "main")]
    reports = [e for e in entries if "/new_tab/report?" in e.url]
    assert [(e.kind, e.level) for e in reports] == [("hit", "exact")]
    (probe,) = parse_qs(urlsplit(reports[0].url).query)["d"]
    assert json.loads(probe)["dates"][0] == recorded_t0
    assert not [e for e in entries if e.kind == "miss" and "/new_tab/" in e.url]


def test_two_replays_give_identical_screenshots(flow: Runs) -> None:
    first = driver.step_signatures(flow.replays["first"].outcome.trace)
    second = driver.step_signatures(flow.replays["second"].outcome.trace)
    assert [s.screenshot_sha256 for s in first] == [s.screenshot_sha256 for s in second]
    assert all(s.screenshot_sha256 for s in first)
    assert first == second


def test_replay_has_no_live_egress(flow: Runs, strict: Runs, egress: Runs, local: Runs) -> None:
    """Not one TCP connection, not one HTTP request, on any fixture server, during any isolated replay.

    The egress and localhost replays are the ones with a route to cut (the
    tests below); the others ask only for mapped names, which a replay cannot
    reach anyway.
    """
    isolated = [
        *flow.replays.items(),
        *strict.replays.items(),
        ("egress", egress.replays["isolated"]),
        ("localhost", local.replays["isolated"]),
    ]
    for name, replayed in isolated:
        assert replayed.accepts_after == replayed.accepts_before, name
        assert replayed.requests_after == replayed.requests_before, name


def test_the_egress_page_asks_for_routes_only_isolation_cuts(
    site: FixtureSite, egress: Runs
) -> None:
    """The zero above is not vacuous: the isolated replay asked for the fixture by IP literal.

    The page and its script — IP-literal URLs a forwarded request would
    reach — were answered from the archive (the page rendered what the script
    writes, as recorded), and the IP-literal WebSocket was attempted and
    logged as a miss; the preconnect to cross needs no log. The recording
    reached the fixture over the same routes.
    """
    page, script, websocket = _egress_urls(site)
    recorded = {(r.server, r.path) for r in site.requests_for("egress")}
    assert {("main", "/egress/page.html"), ("main", "/egress/ip.js")} <= recorded
    assert ("ws", "/egress/echo") in recorded
    isolated = egress.replays["isolated"]
    entries = [entry for _, entry in isolated.log()]
    assert [(e.url, e.kind) for e in entries if e.url in (page, script)] == [
        (page, "hit"),
        (script, "hit"),
    ]
    assert [(e.kind, e.reason, e.answer) for e in entries if e.url == websocket] == [
        ("miss", "websocket", "none")
    ]
    assert driver.step_signatures(isolated.outcome.trace) == driver.step_signatures(
        egress.source.trace
    )


def test_without_isolation_the_egress_page_reaches_the_fixture(
    site: FixtureSite, egress: Runs
) -> None:
    """The negative control: the same replay launched without the blackhole.

    ``isolation: none_acknowledged`` (still ``--no-proxy-server``): the
    preconnect reaches the cross server and the WebSocket the ws server — so
    the counters the isolated replays leave unchanged can move — while every
    request Chrome paused is still answered from the archive: not one HTTP
    request reaches the main server (the replay never forwards).
    """
    page, script, _websocket = _egress_urls(site)
    control = egress.replays["unisolated"]
    assert control.accepts_after["cross"] > control.accepts_before["cross"]
    assert control.accepts_after["ws"] > control.accepts_before["ws"]
    assert control.requests_after["main"] == control.requests_before["main"]
    hits = [entry.url for _, entry in control.log() if entry.kind == "hit"]
    assert page in hits and script in hits


def test_isolation_blocks_bypassing_connections_to_localhost(
    site: FixtureSite, local: Runs
) -> None:
    """A preconnect and a WebSocket to exact ``localhost`` connect nowhere in an isolated replay.

    Neither ever pauses in Fetch, so only the blackhole stands between them
    and a local server that is still up — the hole ``EXCLUDE localhost`` left
    open. The WebSocket was attempted (logged as a miss); the negative
    control, the same replay launched without the blackhole, shows both
    connections reaching the fixture, so the zero is not vacuous. In both,
    every request Chrome paused was answered from the archive: not one HTTP
    request reaches the main server.
    """
    _page, _script, websocket = _localhost_urls(site)
    isolated = local.replays["isolated"]
    assert isolated.accepts_after == isolated.accepts_before
    assert [(e.kind, e.reason, e.answer) for _, e in isolated.log() if e.url == websocket] == [
        ("miss", "websocket", "none")
    ]
    control = local.replays["unisolated"]
    assert control.accepts_after["cross"] > control.accepts_before["cross"]
    assert control.accepts_after["ws"] > control.accepts_before["ws"]
    assert control.requests_after["main"] == control.requests_before["main"]


def test_a_recording_against_a_localhost_site_replays_from_the_archive(
    site: FixtureSite, local: Runs
) -> None:
    """Recorded against ``localhost``, replayed from the archive alone, the server still up.

    The recording reached the fixture on ``localhost``; the isolated replay
    served the page and its script from the source run (the page shows what
    the script writes, as recorded) without one HTTP request to the fixture —
    and the CDP connection drove the whole replay through a browser whose
    resolver blackholes every name, ``localhost`` included.
    """
    page, script, _websocket = _localhost_urls(site)
    recorded = {(r.server, r.path, r.host) for r in site.requests_for("localhost")}
    host = f"localhost:{site.main_port}"
    assert {("main", "/localhost/page.html", host), ("main", "/localhost/local.js", host)} <= (
        recorded
    )
    isolated = local.replays["isolated"]
    hits = [(e.url, e.run_id) for _, e in isolated.log() if e.kind == "hit"]
    assert hits == [(page, local.source.trace.run_id), (script, local.source.trace.run_id)]
    assert isolated.requests_after == isolated.requests_before
    signatures = driver.step_signatures(isolated.outcome.trace)
    assert signatures == driver.step_signatures(local.source.trace)
    assert any("served on localhost" in element for element, _ in signatures[0].elements)


def test_replay_leaves_the_source_untouched(
    flow: Runs, strict: Runs, egress: Runs, local: Runs
) -> None:
    for runs in (flow, strict, egress, local):
        assert tree_digest(runs.source.run_dir) == runs.source_tree_before


def test_base_time_from_archive_replays_the_recorded_clock(flow: Runs) -> None:
    source = flow.source.trace
    fingerprint = json.loads(source.read_extra(source.archive_run_file("browser").path))  # type: ignore[union-attr]
    t0_ms = fingerprint["replayability"]["t0_ms"]
    # Step k observes what step k - 1 navigated to; the start page is step 0's.
    clock_step = FLOW.index("clock")
    recorded = driver.step_signatures(source)[clock_step]
    shown = [json.loads(element)[2] for element, _ in recorded.elements]
    expected = datetime.fromtimestamp(t0_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M")
    assert any(text.startswith(expected) for text in shown), (expected, recorded.elements)
    replayed = driver.step_signatures(flow.replays["first"].outcome.trace)[clock_step]
    assert replayed.elements == recorded.elements
    replay_fingerprint = flow.replays["first"].outcome.trace
    document = json.loads(
        replay_fingerprint.read_extra(replay_fingerprint.archive_run_file("browser").path)  # type: ignore[union-attr]
    )
    assert document["replayability"]["t0_ms"] == t0_ms
    assert document["network_source"]["source_run_id"] == source.run_id
    # The control: a replay on its own clock shows another time on the same page.
    own_clock = driver.step_signatures(flow.replays["run_start"].outcome.trace)[clock_step]
    assert own_clock.elements != recorded.elements


# ── misses ────────────────────────────────────────────────────────────


def test_strict_classes_end_to_end(strict: Runs) -> None:
    misses = {entry.url: entry for _, entry in _misses(strict.replays["strict"])}
    benign = misses["http://stats.example.test/collect"]
    assert (benign.request_class, benign.answer, benign.status) == ("benign", "fulfilled", 204)
    sub_frame = misses["http://frames.example.test/sub.html"]
    assert (sub_frame.request_class, sub_frame.frame, sub_frame.answer) == (
        "document",
        "sub",
        "failed",
    )
    assert not sub_frame.diverged
    main_frame = misses[GONE]
    assert (main_frame.frame, main_frame.answer, main_frame.diverged) == ("main", "failed", True)
    subresource = misses["http://api.example.test/data.json"]
    assert (subresource.request_class, subresource.status) == ("subresource", 404)
    too_large = misses["http://shop.example.test/strict/huge.bin"]
    assert (too_large.reason, too_large.status, too_large.record_id is not None) == (
        "too_large",
        404,
        True,
    )
    hits = {entry.url for _, entry in strict.replays["strict"].log() if entry.kind == "hit"}
    assert {
        "http://shop.example.test/large/static.js",
        "http://shop.example.test/large/data.bin",
    } <= hits, "streamed bodies are served, whatever their type"
    reader = open_archive(strict.replays["strict"].outcome.run_dir)
    gone_step = 3
    assert diverged_steps(reader) == [gone_step]


def test_unrecorded_requests_are_attributed(strict: Runs) -> None:
    """Step, frame and resourceType, in that step's ``browser.replay_log/replay.jsonl``."""
    trace = strict.replays["strict"].outcome.trace
    reader = open_archive(strict.replays["strict"].outcome.run_dir)
    found = [
        (step, entry)
        for step in range(trace.step_count)
        for entry in replay_log(reader, step)
        if CANARY_HOST in entry.url
    ]
    assert len(found) == 1
    step, canary = found[0]
    assert (canary.kind, canary.resource_type, canary.method) == ("miss", "XHR", "GET")
    assert canary.frame_id and canary.target_id and canary.session_id
    names = [record.name for record in trace.archive_files(step, "browser.replay_log")]
    assert names == ["replay.jsonl"]
    assert trace.archive_files(step, "browser.network") == [], "a replay records no traffic"


def test_websocket_is_a_miss_under_isolation(site: FixtureSite, strict: Runs, egress: Runs) -> None:
    """Logged as a miss whatever its host; blocked where a route exists (the IP literal).

    The control above shows the ws server's counter moving without isolation.
    """
    replayed = strict.replays["strict"]
    sockets = [entry for _, entry in _misses(replayed) if entry.url.startswith("ws://")]
    assert [(e.reason, e.answer) for e in sockets] == [("websocket", "none")]
    _page, _script, websocket = _egress_urls(site)
    isolated = egress.replays["isolated"]
    assert [e.reason for _, e in _misses(isolated) if e.url == websocket] == ["websocket"]
    assert isolated.accepts_after["ws"] == isolated.accepts_before["ws"]
