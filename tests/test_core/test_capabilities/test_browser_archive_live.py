"""The browser archivers against a real headless Chrome and the fixture site (§4.14).

The M29 acceptance criteria that only a real browser can show:

- **Completeness** — every request the fixture's access log saw has an index
  line (an exchange, or a last ``pending`` line, which reads as an explicit
  ``incomplete``), per scenario; the nested worker's script is flagged, not
  complete.
- **Observation identity** — at the same replayability tier, an agent observes
  the same ``serialized_dom`` and screenshot bytes with archiving on and off,
  for a DOM agent and for a vision agent.
- **Overhead** — p50 / p95 per step, bytes per step before and after
  deduplication (every scenario visited twice) and the vision self-capture's
  duration, written as a machine-readable report (``ArchiveOverheadReport``)
  for M33.
- **The control connection carries no body** — with a 2 MiB frame limit it
  survives a 6 MB response the archive records.
- **Privacy** — credentials planted in request headers, a cookie, a POST
  password and a script-set password value occur zero times under the trace
  directory (gzip-aware); the positive controls are found.
- **Sensitivity parity** — the Python rule and the Skyvern page script judge
  the same table of controls the same way in Chrome.

Fixture site only, short timeouts. Each test drives its own Chrome; the full
recording is shared by the completeness tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive import ArchiveBatch
from spectragent.core.capabilities.browser.archive import (
    DomRawArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.archive.dom_raw import DomRawSession
from spectragent.core.capabilities.browser.dom.js.skyvern_extraction import EXTRACTION_SCRIPT
from spectragent.core.capabilities.browser.dom.sensitivity import is_sensitive_control
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.negotiation import negotiate_archive
from spectragent.core.trace.secret_patterns import iter_readable
from spectragent.flywheel.metrics import percentile
from tests._helpers.archive_bench import (
    DOM_FRAGMENTS,
    VISION_FRAGMENTS,
    ArchiveOverheadReport,
    LiveRecording,
    captured_paths,
    median,
    served_but_not_captured,
    write_report,
)
from tests._helpers.fixture_site import FIXTURE_PASSWORD, SCENARIOS, FixtureSite
from tests._helpers.trace_builder import agent_result, step_record
from tests.test_core.test_capabilities.test_browser_sensitivity import CONTROL_TABLE

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180


def _all_archivers(**network: Any) -> tuple[Any, ...]:
    return (
        NetworkArchiver(require_replayable=False, **network),
        StorageArchiver(),
        DomRawArchiver(),
    )


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, _TIMEOUT_S))


# ── completeness, against the access log ─────────────────────────────


@pytest.fixture(scope="module")
def full_recording() -> dict[str, Any]:
    """Every fixture scenario recorded once, at ``off`` (service workers run)."""

    async def record() -> dict[str, Any]:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(site, archivers=_all_archivers()) as rec:
                for scenario in SCENARIOS:
                    await rec.step(
                        site.url(scenario), scenario=scenario, wait_for=site.expected(scenario)
                    )
                await rec.final_collect()
                return {"site": site, "lines": rec.index_lines(), "files": rec.files()}

    return _run(record())


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s != "nested_worker"])
def test_every_served_request_is_archived(full_recording: dict[str, Any], scenario: str) -> None:
    site: FixtureSite = full_recording["site"]
    served = {r.bare_path for r in site.requests_for(scenario)}
    assert served >= set(site.expected(scenario)), f"{scenario} did not run to completion"
    missing = served_but_not_captured(site, [scenario], full_recording["lines"])
    assert missing == [], f"served but not captured: {missing}"


def test_nested_worker_is_flagged(full_recording: dict[str, Any]) -> None:
    """Its script bypasses Fetch at both stages: on record as incomplete, never as complete."""
    site: FixtureSite = full_recording["site"]
    assert served_but_not_captured(site, ["nested_worker"], full_recording["lines"]) == []
    statuses = captured_paths(full_recording["lines"])
    assert statuses["/nested_worker/inner.js"] == ["incomplete"]


def test_what_the_scenarios_recorded(full_recording: dict[str, Any]) -> None:
    """The body tiers, redirects, Range and WebSocket as the archive states them."""
    statuses = captured_paths(full_recording["lines"])
    assert statuses["/redirect/hop1"] == ["redirect"]
    assert statuses["/redirect/hop2"] == ["redirect"]
    assert statuses["/large/static.js"] == ["streamed_later"]
    assert statuses["/large/data.bin"] == ["too_large"]
    assert statuses["/websocket/echo"] == ["websocket"]
    lines = full_recording["lines"]
    (video,) = [
        line for line in lines if line["kind"] == "exchange" and line["url"].endswith("clip.webm")
    ][:1]
    assert video["status"] == 206 and video["content_range"]["unit"] == "bytes"
    bodies = [line for line in lines if line["kind"] == "body"]
    assert bodies and bodies[0]["body_status"] == "ok" and bodies[0]["payload_length"] > 6_000_000
    form = next(line for line in lines if line.get("url", "").endswith("/post_form/submit"))
    assert form["method"] == "POST" and form["request"]["post"]["length"] > 0
    # The form's password reached the server; in the archive the request
    # record holds it redacted. (The page's own markup carries it too — a
    # response body, recorded as served.)
    warc = next(
        item.data
        for item in full_recording["files"]
        if item.name == "exchanges.warc.gz"
        and form["request_record_id"].encode() in _gunzip(item.data)
    )
    where = form["warc"]["request"]
    request_record = _gunzip(warc[where["offset"] : where["offset"] + where["length"]])
    assert b"password=<redacted>" in request_record
    assert FIXTURE_PASSWORD.encode() not in request_record


def _gunzip(data: bytes) -> bytes:
    import gzip

    return gzip.decompress(data)


# ── the agent sees the same page with archiving on and off ────────────

_NODE_MARKER = re.compile(r"\[(\d+)\]")


def _renumbered(observation: dict[str, Any]) -> dict[str, Any]:
    """The observation with element markers numbered by first appearance.

    ``serialized_dom`` marks elements with Chrome's backend node ids, which
    differ between two launches of the same page by load timing alone —
    measured with archiving *off* on both sides: the fixture's out-of-process
    iframe element is ``[13]`` in some launches and ``[14]`` in others. Two
    separate browsers are compared here, so the ids are renumbered; every
    other byte — structure, text, attributes, the screenshot — must match.
    """
    text = observation.get("serialized_dom")
    if text is None:
        return observation
    order: dict[str, int] = {}

    def renumber(match: re.Match[str]) -> str:
        return f"[{order.setdefault(match.group(1), len(order))}]"

    return {**observation, "serialized_dom": _NODE_MARKER.sub(renumber, text)}


@pytest.mark.parametrize(
    ("tier", "kind"),
    [("off", "dom"), ("off", "vision"), ("pinned", "dom"), ("pinned", "vision")],
    ids=["off-dom", "off-vision", "pinned-dom", "pinned-vision"],
)
def test_archive_does_not_change_observations(tier: str, kind: str) -> None:
    fragments = DOM_FRAGMENTS if kind == "dom" else VISION_FRAGMENTS

    async def observe(archived: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(
                site,
                archivers=_all_archivers() if archived else (),
                fragments=fragments,
                replayability=PinnedReplayability(random_seed=7) if tier == "pinned" else None,
            ) as rec:
                await rec.step(site.url("oopif"), wait_for=site.expected("oopif"), collect=archived)
                return rec.observations[-1], rec.dom_documents()

    off, _ = _run(observe(False))
    on, documents = _run(observe(True))
    assert _renumbered(on) == _renumbered(off), (
        f"{tier}/{kind}: archiving changed what the agent observed"
    )
    assert documents, "the archived run recorded no raw DOM"
    source = "observation" if kind == "dom" else "archive"
    assert {d["source"] for d in documents} == {source}


def test_the_skyvern_capture_is_archived_redacted() -> None:
    """With the Skyvern cleaner the archive holds the page script's raw output, redacted."""
    from spectragent.core.capabilities.browser.dom.cleaning.skyvern import SkyvernCleaner

    secret = "planted-skyvern-password-Ty6"

    async def record() -> list[dict[str, Any]]:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(
                site,
                archivers=(DomRawArchiver(),),
                dom_cleaner=SkyvernCleaner(),
                profile="skyvern",
            ) as rec:
                await rec.step(
                    site.url("redirect"), wait_for=site.expected("redirect"), collect=False
                )
                cap = rec.capability
                await cap.cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "document.body.insertAdjacentHTML('beforeend', "
                            f"'<input type=password id=pw value=\\'{secret}\\'>"
                            "<button id=go>Go</button>');"
                        )
                    },
                    session_id=cap.default_session_id,
                )
                await rec.observe()
                await rec.final_collect()
                return rec.dom_documents()

    documents = _run(record())
    assert documents and {d["cleaner"] for d in documents} == {"skyvern"}
    raw_dom = documents[-1]["capture"]["raw_dom"]
    assert isinstance(raw_dom.get("skyvern_tree"), dict)
    assert raw_dom["skyvern_nodes"], "the input and the button resolved to DOM nodes"
    entries = documents[-1]["redaction"]["entries"]
    assert any(e["rule"] == "input_value" and "skyvern_nodes" in e["location"] for e in entries), (
        "the describeNode copy of the password's markup value was withheld"
    )
    assert secret not in json.dumps(documents)


def test_renumbering_keeps_every_other_byte() -> None:
    same = _renumbered({"serialized_dom": "a\n[14]<iframe />\n[20]<a>x</a>"})
    assert same == _renumbered({"serialized_dom": "a\n[13]<iframe />\n[19]<a>x</a>"})
    assert same != _renumbered({"serialized_dom": "a\n[13]<iframe />\n[19]<a>y</a>"})


# ── overhead ──────────────────────────────────────────────────────────

#: The PRD's eleven scenarios plus the beacon: every fixture scenario but the
#: nested worker, a known limitation whose inner script the archive can only
#: flag as incomplete. Each is visited twice in one browser, so the second
#: visit's payloads repeat the first's.
_BENCH_SCENARIOS = tuple(s for s in SCENARIOS if s != "nested_worker")
_BENCH_VISITS = 2


def test_overhead_benchmark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bound = float(os.environ.get("SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS", "300"))
    self_captures: list[float] = []
    on_cycle = DomRawSession.on_cycle

    async def timed_on_cycle(session: DomRawSession, cycle: Any) -> None:
        started = time.perf_counter()
        try:
            await on_cycle(session, cycle)
        finally:
            self_captures.append((time.perf_counter() - started) * 1000)

    monkeypatch.setattr(DomRawSession, "on_cycle", timed_on_cycle)

    async def bench(archived: bool, fragments: frozenset[str]) -> LiveRecording:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(
                site, archivers=_all_archivers() if archived else (), fragments=fragments
            ) as rec:
                for visit in range(_BENCH_VISITS):
                    for scenario in _BENCH_SCENARIOS:
                        await rec.step(
                            site.url(scenario),
                            scenario=scenario,
                            wait_for=(site.expected if visit == 0 else site.expected_again)(
                                scenario
                            ),
                            collect=archived,
                        )
                if archived:
                    await rec.final_collect()
                return rec

    off = _run(bench(False, DOM_FRAGMENTS))
    on = _run(bench(True, DOM_FRAGMENTS))
    vision = _run(bench(True, VISION_FRAGMENTS))
    for recording in (off, on, vision):
        timed_out = [t.scenario for t in recording.timings if not t.complete]
        assert not timed_out, f"steps that never saw their own traffic: {timed_out}"
    assert len(self_captures) >= len(vision.timings), "a vision cycle went without a self-capture"
    off_ms = [t.total_ms for t in off.timings]
    on_ms = [t.total_ms for t in on.timings]
    overhead = [b - a for a, b in zip(off_ms, on_ms, strict=True)]
    steps = len(on_ms)
    dedup = on.dedup_bytes()
    fingerprint = next(item for item in on.files() if item.scope == "run")
    report = ArchiveOverheadReport(
        chrome=str(json.loads(fingerprint.data)["chrome"]["product"]),
        tier="off",
        steps=steps,
        visits_per_scenario=_BENCH_VISITS,
        p50_off_ms=median(off_ms),
        p50_on_ms=median(on_ms),
        p95_off_ms=percentile(off_ms, 95),
        p95_on_ms=percentile(on_ms, 95),
        p50_overhead_ms=median(overhead),
        p95_overhead_ms=percentile(overhead, 95),
        p50_bound_ms=bound,
        bytes_per_step_before_dedup=dedup.before / steps,
        bytes_per_step_after_dedup=dedup.after / steps,
        bytes_per_step_saved_by_revisit=dedup.saved_by_revisit / steps,
        bytes_per_step_saved_by_content_addressing=dedup.saved_by_content_addressing / steps,
        self_capture_ms_p50=median(self_captures),
        self_capture_ms_p95=percentile(self_captures, 95),
        scenarios=list(_BENCH_SCENARIOS),
    )
    out = Path(os.environ.get("SPECTRAGENT_ARCHIVE_BENCH_OUT") or tmp_path)
    path = write_report(report, out)
    assert ArchiveOverheadReport.model_validate_json(path.read_text("utf-8")) == report
    # Both deduplications ran on this traffic, so the two figures differ for a
    # reason: second visits wrote revisit records, and the large static body
    # streamed on both visits was stored once.
    assert any(line.get("revisit_of") for line in on.index_lines()), "no revisit record written"
    assert dedup.saved_by_content_addressing > 6_000_000, "the large body was stored twice"
    assert report.bytes_per_step_after_dedup < report.bytes_per_step_before_dedup
    assert report.p50_overhead_ms <= bound, (
        f"archiving added {report.p50_overhead_ms:.0f} ms per step at p50 (bound {bound:.0f} ms; "
        f"override with SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS); report: {path}"
    )


# ── the control connection never carries a body ───────────────────────


def test_control_connection_never_carries_bodies() -> None:
    async def record() -> tuple[bool, list[dict[str, Any]], list[Any]]:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(
                site,
                archivers=(NetworkArchiver(require_replayable=False),),
                control_max_frame_size=2 * 1024 * 1024,
            ) as rec:
                await rec.step(site.url("large"), wait_for=site.expected("large"))
                await asyncio.sleep(0.5)
                await rec.final_collect()
                await rec.observe()  # the control connection still works
                return rec.capability.cdp.is_connected, rec.index_lines(), rec.files()

    connected, lines, files = _run(record())
    assert connected, "a body reached the control connection and closed it"
    blobs = [item for item in files if item.content_addressed]
    assert any(len(item.data) > 6_000_000 for item in blobs), "the 6 MB body is in the archive"
    statuses = captured_paths(lines)
    assert statuses["/large/static.js"] == ["streamed_later"]


# ── privacy: planted credentials never reach the trace ────────────────

_PLANTED = {
    "authorization": "planted-authorization-Hq3",
    "api_key": "planted-api-key-Lw7",
    "cookie": "planted-cookie-Rt5",
    "dom password": "planted-dom-password-Vn8",
    "post password": "planted-post-password-Kc4",
}
_MARKER = "fixture-marker-body"


async def _record_trace(
    trace_root: Path, *, allow_session_state: bool, plant: bool = True
) -> tuple[Path, list[Any]]:
    with FixtureSite(mapped=True) as site:
        archivers = [
            NetworkArchiver(require_replayable=False, strip_request_headers=["X-Api-Key"]),
            StorageArchiver(allow_session_state=allow_session_state),
            DomRawArchiver(),
        ]
        writer = TraceWriter(trace_dir=str(trace_root), archive=list(archivers))
        rec = LiveRecording(site, archivers=archivers, fragments=VISION_FRAGMENTS)
        async with rec:
            cap = rec.capability
            env = BrowserEnvironment(capabilities={"browser": cap})
            negotiation = negotiate_archive([writer], env)
            # What ``AgentRuntime.prepare`` does last: the environment's binding
            # table (the capability already holds the same instances).
            env.bind_archivers(writer.archive)
            await writer.on_run_start(
                env=env,
                task="privacy guard",
                config={},
                archive_negotiation=negotiation,
                env_evidence_recorded=True,
            )
            sid = cap.default_session_id
            if plant:
                # Extra headers take effect only with Network enabled on the
                # session. ``Proxy-Authorization`` cannot be planted this way:
                # Chrome 155 fails every request carrying it as an extra header
                # (net::ERR_INVALID_ARGUMENT); its stripping is covered by the
                # fake-CDP tests (``test_browser_archive_network.py``).
                await cap.cdp.send("Network.enable", {}, session_id=sid)
                await cap.cdp.send(
                    "Network.setExtraHTTPHeaders",
                    {
                        "headers": {
                            "Authorization": f"Bearer {_PLANTED['authorization']}",
                            "X-Api-Key": _PLANTED["api_key"],
                        }
                    },
                    session_id=sid,
                )
                await cap.cdp.send(
                    "Network.setCookie",
                    {"name": "sid", "value": _PLANTED["cookie"], "url": site.main_origin + "/"},
                    session_id=sid,
                )
            # Step 0: a page whose password input holds a value a script set, and
            # an urlencoded POST carrying a password; plus a marker request.
            await rec.step(site.url("redirect"), wait_for=site.expected("redirect"), collect=False)
            await cap.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        "document.body.insertAdjacentHTML('beforeend', "
                        "'<input type=password id=pw><p id=m>shown</p>');"
                        f"document.getElementById('pw').value = '{_PLANTED['dom password']}';"
                        "fetch('/json_post/api', {method: 'POST', headers: "
                        "{'Content-Type': 'application/x-www-form-urlencoded'}, "
                        f"body: 'user=alice&password={_PLANTED['post password']}'}});"
                        f"fetch('/redirect/landing.html?marker={_MARKER}');"
                    )
                },
                session_id=sid,
            )
            await asyncio.sleep(0.5)
            await rec.observe()
            await writer.after_record_step(None, args=(step_record(0),), kwargs={})
            # Step 1: another page, so step 0's traffic and storage settle.
            await rec.step(site.url("beacon"), wait_for=site.expected("beacon"), collect=False)
            await writer.after_record_step(None, args=(step_record(1),), kwargs={})
            await writer.on_run_end(result=agent_result())
        assert writer.run_dir is not None
        return writer.run_dir, site.requests


def test_privacy_guard(tmp_path: Path) -> None:
    run_dir, served = _run(_record_trace(tmp_path, allow_session_state=False))
    # The plants were real: the fixture received every one of them.
    header_names = {name for request in served for name in request.header_names}
    assert {"authorization", "x-api-key", "cookie"} <= header_names
    assert any(r.bare_path == "/json_post/api" and r.body_length > 0 for r in served)
    scanned = list(iter_readable(run_dir))
    assert any("/env/" in str(path) for path, _ in scanned), "the trace recorded no env/"
    ledgers = " ".join(text for path, text in scanned if "/env/" in str(path))
    for rule in ("request_header", "field_name", "input_value", "value_hash"):
        assert f'"rule":"{rule}"' in ledgers, f"no {rule} redaction was recorded"
    for label, secret in _PLANTED.items():
        hits = [str(path.relative_to(run_dir)) for path, text in scanned if secret in text]
        assert hits == [], f"planted {label} found in {hits}"


def test_privacy_guard_positive_controls(tmp_path: Path) -> None:
    """The scan can see: raw cookie values when allowed, and a marker the WARC holds."""
    run_dir, _served = _run(_record_trace(tmp_path, allow_session_state=True))
    scanned = list(iter_readable(run_dir))
    assert [p for p, text in scanned if _PLANTED["cookie"] in text], (
        "allow_session_state: true keeps raw cookie values — the scan must find one"
    )
    warcs = [p for p, text in scanned if p.name.endswith(".warc.gz") or "WARC/1.1" in text]
    assert any(_MARKER in text for p, text in scanned if p in warcs), (
        "the marker request is in the WARC, and the gzip-aware scan finds it"
    )


# ── the sensitivity rule: Python and the page script agree in Chrome ──


def test_sensitivity_parity_in_chrome() -> None:
    controls = "".join(
        f"<{tag} data-row='c{i}' "
        + " ".join(f"{name}='{value}'" for name, value in attrs.items())
        + (f"></{tag}>" if tag != "input" else ">")
        for i, (tag, attrs, _expected) in enumerate(CONTROL_TABLE)
    )

    async def judge() -> dict[str, bool]:
        with FixtureSite(mapped=True) as site:
            async with LiveRecording(site, fragments=VISION_FRAGMENTS) as rec:
                await rec.step(
                    site.url("redirect"), wait_for=site.expected("redirect"), collect=False
                )
                cap = rec.capability
                await cap.cdp.send(
                    "Runtime.evaluate",
                    {"expression": f"document.body.innerHTML = {json.dumps(controls)};"},
                    session_id=cap.default_session_id,
                )
                reply = await cap.cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": f"JSON.stringify(({EXTRACTION_SCRIPT}).tree)",
                        "returnByValue": True,
                    },
                    session_id=cap.default_session_id,
                )
                tree = json.loads(reply["result"]["value"])
                judged: dict[str, bool] = {}
                stack = [tree]
                while stack:
                    node = stack.pop()
                    attrs = node.get("attributes") or {}
                    if str(attrs.get("data-row", "")).startswith("c"):
                        judged[attrs["data-row"]] = attrs.get("data-af-redacted") == "true"
                    stack.extend(node.get("children") or [])
                return judged

    judged = _run(judge())
    compared = 0
    for i, (tag, attrs, expected) in enumerate(CONTROL_TABLE):
        key = f"c{i}"
        if key not in judged:  # never rendered (``type=hidden``): absent from the page tree
            assert attrs.get("type") == "hidden"
            continue
        compared += 1
        assert judged[key] is is_sensitive_control(tag, attrs) is expected, (tag, attrs)
    assert compared == len(CONTROL_TABLE) - 1


def test_an_empty_batch_is_still_an_archive_batch() -> None:
    """Sanity for the helpers: a step that collects nothing records an empty batch."""
    assert ArchiveBatch().is_empty
