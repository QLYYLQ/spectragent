"""The overhead benchmark's artefact has a stable, machine-readable schema (M29 → M33).

Plus what the live benchmark's figures rest on, checked without a browser:
the byte accounting behind the before / after dedup figures (on files built by
hand — the live run cannot pin exact byte counts), and a step waiting for its
own requests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.flywheel.metrics import percentile
from tests._helpers.archive_bench import (
    ArchiveOverheadReport,
    LiveRecording,
    dedup_bytes,
    write_report,
)
from tests._helpers.fixture_site import LoggedRequest


def _report(**overrides: object) -> ArchiveOverheadReport:
    fields: dict[str, object] = {
        "chrome": "Chrome/155.0.8059.39",
        "tier": "none",
        "steps": 10,
        "visits_per_scenario": 2,
        "p50_off_ms": 400.0,
        "p50_on_ms": 450.0,
        "p95_off_ms": 600.0,
        "p95_on_ms": 700.0,
        "p50_overhead_ms": 50.0,
        "p95_overhead_ms": 100.0,
        "p50_bound_ms": 250.0,
        "bytes_per_step_before_dedup": 20000.0,
        "bytes_per_step_after_dedup": 12000.0,
        "bytes_per_step_saved_by_revisit": -100.0,
        "bytes_per_step_saved_by_content_addressing": 8100.0,
        "self_capture_ms_p50": 35.0,
        "self_capture_ms_p95": 60.0,
        "scenarios": ["redirect", "oopif"],
    }
    fields.update(overrides)
    return ArchiveOverheadReport(**fields)  # type: ignore[arg-type]


def test_report_schema(tmp_path: Path) -> None:
    report = _report()
    path = write_report(report, tmp_path / "bench")
    loaded = json.loads(path.read_text("utf-8"))
    assert loaded["schema_version"] == 1
    assert set(loaded) == set(ArchiveOverheadReport.model_fields)
    assert ArchiveOverheadReport.model_validate(loaded) == report
    schema = ArchiveOverheadReport.model_json_schema()
    assert {
        "p50_overhead_ms",
        "p95_overhead_ms",
        "bytes_per_step_before_dedup",
        "bytes_per_step_after_dedup",
        "self_capture_ms_p50",
    } <= set(schema["required"])


def test_a_report_without_steps_is_refused() -> None:
    with pytest.raises(ValidationError):
        _report(steps=0)


def test_a_self_capture_is_a_duration_not_a_difference() -> None:
    with pytest.raises(ValidationError):
        _report(self_capture_ms_p50=-1.5)


def test_percentile_nearest_rank() -> None:
    assert percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)  # nothing measured is not a measured 0


# ── the dedup accounting ──────────────────────────────────────────────


def _file(name: str, data: bytes, *, archiver: str = "browser.network", **kw: Any) -> ArchiveFile:
    return ArchiveFile(
        archiver_id=archiver,
        name=name,
        media_type="application/octet-stream",
        data=data,
        meta=ArchiveProvenance(captured_at=0.0),
        **kw,
    )


def _exchange(record: str, response: str, length: int, revisit_of: str | None = None) -> dict:
    return {
        "kind": "exchange",
        "record_id": record,
        "url": f"http://shop.example.test/{record}",
        "body_status": "ok",
        "response_record_id": response,
        "revisit_of": revisit_of,
        "warc": {
            "request": {"offset": 0, "length": 300},
            "response": {"offset": 0, "length": length},
        },
    }


def _index(*lines: dict) -> ArchiveFile:
    return _file("index.jsonl", "".join(json.dumps(line) + "\n" for line in lines).encode())


def test_dedup_accounts_revisits_and_content_addressing() -> None:
    body = _file("body-1.bin", b"x" * 1000, content_addressed=True)
    dom = _file("dom-1.json.gz", b"d" * 50, archiver="browser.dom_raw", content_addressed=True)
    first_step = [
        _file("exchanges.warc.gz", b"w" * 1500),
        _index(
            _exchange("r1", "a", 600),  # a payload written in full
            _exchange("r2", "b", 420),  # a tiny one
            {
                "kind": "exchange",
                "record_id": "r3",
                "url": "http://shop.example.test/r3",
                "body_status": "streamed_later",
                "response_record_id": None,
            },
            {"kind": "pending", "record_id": "r4", "url": "http://shop.example.test/r4"},
        ),
        body,
        dom,
    ]
    second_step = [
        _file("exchanges.warc.gz", b"w" * 1100),
        _index(
            _exchange("r5", "c", 520, revisit_of="a"),  # saves 80
            _exchange("r6", "d", 510, revisit_of="b"),  # costs 90
            _exchange("r7", "e", 700),  # new
            {"kind": "body", "record_id": "r3", "body_status": "ok"},
        ),
        _file("body-1.bin", b"x" * 1000, content_addressed=True),  # the same body again
        _file("dom-2.json.gz", b"e" * 60, archiver="browser.dom_raw", content_addressed=True),
        _file("storage.json", b"s" * 30, archiver="browser.storage"),
    ]
    files = first_step + second_step
    emitted = sum(len(item.data) for item in files)

    dedup = dedup_bytes(files)

    assert dedup.saved_by_content_addressing == 1000
    assert dedup.saved_by_revisit == (600 - 520) + (420 - 510)
    assert dedup.after == emitted - 1000
    assert dedup.before == emitted + (600 - 520) + (420 - 510)
    assert dedup.before == dedup.after + dedup.saved_by_revisit + dedup.saved_by_content_addressing


def test_a_file_that_is_not_content_addressed_is_never_deduplicated() -> None:
    same = b"z" * 100
    dedup = dedup_bytes([_file("storage.json", same), _file("storage.json", same)])
    assert (dedup.before, dedup.after, dedup.saved_by_content_addressing) == (200, 200, 0)


def test_a_revisit_of_a_record_the_recording_lacks_is_refused() -> None:
    with pytest.raises(ValueError, match="'gone'"):
        dedup_bytes([_index(_exchange("r1", "c", 520, revisit_of="gone"))])


# ── a step waits for its own traffic ──────────────────────────────────


def _served(path: str) -> LoggedRequest:
    return LoggedRequest("main", "shop.example.test", "GET", path, (), None, 0)


class _Log:
    """The request log of a fixture site, filled by the test."""

    def __init__(self, *paths: str) -> None:
        self.requests = [_served(path) for path in paths]


async def test_a_later_visit_waits_for_its_own_requests() -> None:
    wanted = ["/s/page.html", "/s/data.json"]
    log = _Log(*wanted)  # the first visit
    rec = LiveRecording(cast(Any, log))
    assert await rec.wait_served(wanted, since=0, seconds=0.05)

    mark = len(log.requests)
    log.requests.append(_served("/s/page.html"))  # the second visit, so far
    assert not await rec.wait_served(wanted, since=mark, seconds=0.1), (
        "the first visit's data.json answered for the second"
    )

    async def second_visit_completes() -> None:
        await asyncio.sleep(0.1)
        log.requests.append(_served("/s/data.json"))

    completing = asyncio.create_task(second_visit_completes())
    assert await rec.wait_served(wanted, since=mark, seconds=5.0)
    await completing
