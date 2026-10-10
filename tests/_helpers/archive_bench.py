"""Shared helpers for the live archive tests and the overhead benchmark (M29, reused by M33).

- :class:`LiveRecording` — a real ``BrowserCapability`` on a headless local
  Chrome with the browser archivers bound, recording the fixture site
  (``tests/_helpers/fixture_site.py``): navigate, wait until the fixture
  served what a scenario needs during this step, observe one cycle, collect.
- :func:`served_but_not_captured` — the completeness oracle: every request
  the fixture's access log saw must have an index line in the archive (a
  record whose last line is ``pending`` counts as an explicit ``incomplete``).
- :func:`dedup_bytes` — what a recording's evidence weighs with and without
  the run's two deduplications (WARC revisit records, content addressing).
- :class:`ArchiveOverheadReport` — the machine-readable overhead artefact
  (p50 / p95 per-step overhead, bytes per step before and after
  deduplication, the vision self-capture cost) M33 reads.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from spectragent.core.archive import ArchiveBatch, ArchiveFile
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import BrowserArchiverBase
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.schemas.action import Action
from tests._helpers.fixture_site import FixtureSite

#: The fragments of a DOM agent and of a vision agent.
DOM_FRAGMENTS = frozenset({"browser.dom", "browser.screenshot"})
VISION_FRAGMENTS = frozenset({"browser.screenshot"})


@dataclass
class StepTiming:
    """One recorded step: navigate + observe + collect, in milliseconds."""

    scenario: str
    navigate_ms: float
    observe_ms: float
    collect_ms: float
    #: Every path the step waited for was served during the step (``False``:
    #: the wait timed out, and ``navigate_ms`` holds the timeout).
    complete: bool

    @property
    def total_ms(self) -> float:
        return self.navigate_ms + self.observe_ms + self.collect_ms


@dataclass
class LiveRecording:
    """A capability recording the fixture site, step by step."""

    site: FixtureSite
    archivers: Sequence[BrowserArchiverBase] = ()
    fragments: frozenset[str] = DOM_FRAGMENTS
    replayability: Any = None
    extra_args: Sequence[str] = ()
    control_max_frame_size: int | None = None
    #: A DOM cleaner instance to use instead of the default (BrowserUse).
    dom_cleaner: Any = None
    profile: str = "standard"
    capability: BrowserCapability = field(init=False)
    batches: list[ArchiveBatch] = field(default_factory=list)
    timings: list[StepTiming] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    _cycle: int = 0

    async def __aenter__(self) -> LiveRecording:
        cap = BrowserCapability(profile=self.profile)
        if self.dom_cleaner is not None:
            cap.dom_cleaner = self.dom_cleaner
        cap.backend = LocalChromeProvider(  # type: ignore[assignment]
            headless=True, extra_args=[*self.site.chrome_args(), *self.extra_args]
        )
        if self.replayability is not None:
            cap.replayability = self.replayability
        if self.control_max_frame_size is not None:
            from spectragent.core.capabilities.browser.cdp_client import CDPClient

            limit = self.control_max_frame_size

            def factory(url: str, **options: Any) -> CDPClient:
                if "max_frame_size" not in options:  # the control connection
                    options["max_frame_size"] = limit
                return CDPClient(url, **options)

            cap._inject_cdp_factory(factory)
        cap.bind_archivers(list(self.archivers))
        await cap.setup(negotiation=set(self.fragments))
        self.capability = cap
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.capability.teardown()

    async def front(self) -> None:
        """Bring the agent's tab to the front (a popup steals it; media waits)."""
        cap = self.capability
        await cap.cdp.send("Page.bringToFront", {}, session_id=cap.default_session_id)

    async def step(
        self,
        url: str,
        *,
        scenario: str = "",
        wait_for: Iterable[str] = (),
        settle_s: float = 0.3,
        collect: bool = True,
    ) -> None:
        """Navigate, wait for the fixture to serve *wait_for* in this step, observe once, collect."""
        await self.front()
        mark = len(self.site.requests)
        started = time.perf_counter()
        result = await self.capability.execute_action(
            Action(name="browser.navigate", params={"url": url})
        )
        assert result.success, result.error
        complete = await self.wait_served(wait_for, since=mark)
        await asyncio.sleep(settle_s)
        navigated = time.perf_counter()
        observation = await self.observe()
        observed = time.perf_counter()
        batch = await self.capability.collect_archive() if collect else ArchiveBatch()
        collected = time.perf_counter()
        self.batches.append(batch)
        self.observations.append(observation)
        self.timings.append(
            StepTiming(
                scenario=scenario,
                navigate_ms=(navigated - started) * 1000,
                observe_ms=(observed - navigated) * 1000,
                collect_ms=(collected - observed) * 1000,
                complete=complete,
            )
        )

    async def wait_served(
        self, paths: Iterable[str], *, since: int = 0, seconds: float = 8.0
    ) -> bool:
        """Wait until every path in *paths* was served after the first *since* logged requests.

        A step passes the log's length before it navigates, so a second visit
        waits for its own requests, not the first visit's. ``False`` when the
        wait timed out.
        """
        wanted = set(paths)
        deadline = time.monotonic() + seconds
        while True:
            if wanted <= {r.bare_path for r in self.site.requests[since:]}:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def observe(self) -> dict[str, Any]:
        """One observation cycle; the fragments the agent would have been served."""
        self._cycle += 1
        cap = self.capability
        await cap.begin_observation(self._cycle)
        observed: dict[str, Any] = {}
        for key in sorted(self.fragments):
            fragment = await cap.resolve_fragment(key)
            if key == "browser.dom":
                observed["serialized_dom"] = fragment.element_tree.serialized_dom
            elif key == "browser.screenshot":
                observed["screenshot_sha256"] = hashlib.sha256(fragment.image_bytes).hexdigest()
        return observed

    async def final_collect(self) -> None:
        self.batches.append(await self.capability.collect_archive())

    # ── what was recorded ─────────────────────────────────────────────

    def files(self) -> list[ArchiveFile]:
        return [item for batch in self.batches for item in batch.files]

    def index_lines(self) -> list[dict[str, Any]]:
        return _network_index_lines(self.files())

    def dom_documents(self) -> list[dict[str, Any]]:
        return [
            json.loads(gzip.decompress(item.data))
            for item in self.files()
            if item.archiver_id == "browser.dom_raw"
        ]

    def dedup_bytes(self) -> DedupBytes:
        """What everything this recording collected weighs, with and without deduplication."""
        return dedup_bytes(self.files())


def _network_index_lines(files: Iterable[ArchiveFile]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for item in files:
        if item.archiver_id == "browser.network" and item.name == "index.jsonl":
            lines.extend(json.loads(line) for line in item.data.decode().splitlines())
    return lines


@dataclass(frozen=True)
class DedupBytes:
    """Archive bytes before and after the run's two deduplications.

    - **Revisit records** (``browser.network``): a payload already written
      earlier in the run is written as a WARC ``revisit`` record instead of a
      full response record. What a revisit replaced is counted as the stored
      length of the response record it points at: the same payload and the
      same compression, so the two records differ only in a few header bytes
      (URI, date, record id). A revisit record of a tiny payload outweighs the
      record it replaces, so this saving can be negative.
    - **Content addressing** (the WAL, §5.2): a ``content_addressed`` file whose
      bytes the run already stored is written once; every later copy is saved.

    ``before == after + saved_by_revisit + saved_by_content_addressing``.
    """

    #: Bytes with neither deduplication: every full response record, every copy.
    before: int
    #: Bytes the run stores.
    after: int
    saved_by_revisit: int
    saved_by_content_addressing: int


def dedup_bytes(files: Iterable[ArchiveFile]) -> DedupBytes:
    """Account the bytes of *files* — everything a run collected — before and after dedup.

    Raises:
        ValueError: a revisit points at a response record that is not among
            *files* (the recording is incomplete, so nothing can be accounted).
    """
    items = list(files)
    emitted = sum(len(item.data) for item in items)
    stored: set[str] = set()
    repeated = 0
    for item in items:
        if not item.content_addressed:
            continue
        digest = hashlib.sha256(item.data).hexdigest()
        if digest in stored:
            repeated += len(item.data)
        stored.add(digest)
    responses = [
        line
        for line in _network_index_lines(items)
        if line.get("kind") == "exchange" and line.get("response_record_id")
    ]
    full_length = {
        line["response_record_id"]: line["warc"]["response"]["length"]
        for line in responses
        if not line.get("revisit_of")
    }
    saved_by_revisit = 0
    for line in responses:
        first = line.get("revisit_of")
        if not first:
            continue
        if first not in full_length:
            raise ValueError(f"revisit of {first!r}, a response record this recording lacks")
        saved_by_revisit += full_length[first] - line["warc"]["response"]["length"]
    after = emitted - repeated
    return DedupBytes(
        before=after + saved_by_revisit + repeated,
        after=after,
        saved_by_revisit=saved_by_revisit,
        saved_by_content_addressing=repeated,
    )


def _path(url: str) -> str:
    rest = url.split("://", 1)[-1]
    path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return path.split("?", 1)[0].split("#", 1)[0]


def last_lines(lines: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each record's last index line (``exchange`` or ``pending``)."""
    last: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.get("kind") in ("exchange", "pending"):
            last[line["record_id"]] = line
    return last


def captured_paths(lines: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Path → the body status of each record for it (``pending`` last: ``incomplete``)."""
    out: dict[str, list[str]] = {}
    for line in last_lines(lines).values():
        status = line["body_status"] if line["kind"] == "exchange" else "incomplete"
        out.setdefault(_path(line["url"]), []).append(status)
    return out


def served_but_not_captured(
    site: FixtureSite, scenarios: Iterable[str], lines: Sequence[dict[str, Any]]
) -> list[str]:
    """Paths the fixture served for *scenarios* that have no record in the archive.

    Set semantics on (path): a refetch of a large static body, a repeat of a
    request, or a preconnect never makes a served path "uncaptured".
    """
    wanted = set(scenarios)
    served = {r.bare_path for r in site.requests if r.scenario in wanted}
    return sorted(served - set(captured_paths(lines)))


class ArchiveOverheadReport(BaseModel):
    """What archiving costs per step — written for M33's budget, read by machines.

    Times are milliseconds of one step (navigate + observe + collect on the
    fixture site), compared between the same steps with archiving off and on
    at the same replayability tier, for a DOM agent. Every scenario is visited
    ``visits_per_scenario`` times in one browser, so later visits repeat the
    first visit's payloads — what deduplication is for.

    Bytes per step are everything the archived run collected (its closing
    collection included) over ``steps``; :class:`DedupBytes` defines the
    before / after split and its two savings
    (``before == after + saved_by_revisit + saved_by_content_addressing``).

    The self-capture is a vision agent's: ``browser.dom_raw`` capturing the
    tab on the recorder connection inside each observation cycle (no DOM is
    negotiated, so the cycle captures none). Its own duration per cycle,
    timed around the session's ``on_cycle``; it runs alongside the screenshot.
    """

    schema_version: int = 1
    chrome: str
    tier: str
    steps: int = Field(ge=1)
    visits_per_scenario: int = Field(ge=1)
    p50_off_ms: float
    p50_on_ms: float
    p95_off_ms: float
    p95_on_ms: float
    p50_overhead_ms: float
    p95_overhead_ms: float
    p50_bound_ms: float
    bytes_per_step_before_dedup: float = Field(ge=0)
    bytes_per_step_after_dedup: float = Field(ge=0)
    #: Negative when the revisit records outweigh the records they replaced.
    bytes_per_step_saved_by_revisit: float
    bytes_per_step_saved_by_content_addressing: float = Field(ge=0)
    self_capture_ms_p50: float = Field(ge=0)
    self_capture_ms_p95: float = Field(ge=0)
    scenarios: list[str]


def percentile(values: Sequence[float], q: float) -> float:
    """The *q*-th percentile (0–100), nearest-rank on a sorted copy."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1))))
    return float(ordered[rank])


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def write_report(report: ArchiveOverheadReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "archive_overhead.json"
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
