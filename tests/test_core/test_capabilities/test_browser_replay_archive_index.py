"""Loading a recorded run's network archive for replay (§4.14 "加载", ``archive_index.py``).

Every archive here is recorded by M29's own ``browser.network`` session and
committed by the real WAL (``tests/_helpers/replay_archive.py``), then read
back only through ``open_archive``. Covered: each ``body_status``, a revisit
across steps, a late streamed body, records that match but cannot be served,
keys recomputed at load, the byte-range map, the recording order, and what
makes a load fail — a malformed line (the trace layer's schema error), a line
whose WARC record is not what it says (``ReplayArchiveError``) — and that
files the manifest never listed are never loaded.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.archive.network import INDEX_NAME, WARC_NAME
from spectragent.core.capabilities.browser.replay.archive_index import (
    BlobPayload,
    IndexedRecord,
    RecordedArchive,
    ReplayArchiveError,
    WarcPayload,
    load_archive,
)
from spectragent.core.capabilities.browser.replay.canonical import request_keys
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule, SiteRules
from spectragent.core.trace.encoding import archive_step_path
from spectragent.core.trace.errors import TraceSchemaError
from tests._helpers.replay_archive import RECORDED_T0_MS, RecordingScript
from tests._helpers.trace_builder import archive_file, new_wal, step_record

SHOP = "http://shop.test"


def by_url(recorded: RecordedArchive) -> dict[str, IndexedRecord]:
    return {record.url: record for record in recorded.index.records}


@pytest.fixture()
async def recorded(tmp_path: Path) -> RecordedArchive:
    recording = RecordingScript(tmp_path)
    await recording.exchange(
        f"{SHOP}/a?x=1", body=b"shared payload", headers={"Set-Cookie": "s=1\nt=2"}
    )
    await recording.redirect(f"{SHOP}/old", f"{SHOP}/new")
    await recording.exchange(
        f"{SHOP}/form",
        method="POST",
        post=b"user=bob&password=hunter2",
        request_headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=b"posted",
    )
    await recording.too_large(f"{SHOP}/huge")
    recording.failed(f"{SHOP}/broken")
    recording.websocket("ws://shop.test/socket")
    await recording.exchange(f"{SHOP}/cached", status=304, body=b"")
    await recording.exchange(
        f"{SHOP}/video.mp4",
        status=206,
        body=b"0123",
        resource_type="Media",
        headers={"Content-Range": "bytes 0-3/10"},
    )
    await recording.streamed_later(f"{SHOP}/now.js", b"n" * 5000)
    await recording.commit(0)
    await recording.exchange(f"{SHOP}/b", body=b"shared payload")  # a revisit of /a
    await recording.exchange(
        f"{SHOP}/video.mp4",
        status=206,
        body=b"456",
        resource_type="Media",
        headers={"Content-Range": "bytes 4-6/10"},
    )
    stalled = await recording.streamed_later(f"{SHOP}/late.js", b"l" * 6000, hold=True)
    recording.in_flight(f"{SHOP}/never")
    await recording.commit(1)
    assert stalled is not None
    stalled.set()
    await recording.commit_trailing()
    return load_archive(open_archive(recording.finish()))


class TestEachKindOfRecord:
    async def test_ok_is_served_from_its_response_record(self, recorded: RecordedArchive) -> None:
        record = by_url(recorded)[f"{SHOP}/a?x=1"]
        assert (record.status, record.body_status, record.miss_reason) == (200, "ok", None)
        assert isinstance(record.payload, WarcPayload)
        assert recorded.body(record) == b"shared payload"
        assert ("Set-Cookie", "s=1") in record.response_headers
        assert ("Set-Cookie", "t=2") in record.response_headers, "one entry per Set-Cookie line"
        assert record.run_id == "run-recorded"

    async def test_a_redirect_is_its_3xx_and_the_page_it_lands_on(
        self, recorded: RecordedArchive
    ) -> None:
        hop, landing = by_url(recorded)[f"{SHOP}/old"], by_url(recorded)[f"{SHOP}/new"]
        assert (hop.status, hop.body_status) == (302, "redirect")
        assert ("Location", f"{SHOP}/new") in hop.response_headers
        assert recorded.body(hop) == b""
        assert (landing.status, recorded.body(landing)) == (200, b"landed")
        assert hop.order < landing.order

    async def test_a_post_carries_its_redacted_body(self, recorded: RecordedArchive) -> None:
        record = by_url(recorded)[f"{SHOP}/form"]
        assert record.body == b"user=bob&password=<redacted>"
        assert record.content_type == "application/x-www-form-urlencoded"
        assert record.keys.body_key is not None and "%3Credacted%3E" in record.keys.body_key

    async def test_a_revisit_serves_the_first_payload_from_an_earlier_step(
        self, recorded: RecordedArchive
    ) -> None:
        first, revisit = by_url(recorded)[f"{SHOP}/a?x=1"], by_url(recorded)[f"{SHOP}/b"]
        assert isinstance(revisit.payload, WarcPayload) and isinstance(first.payload, WarcPayload)
        assert revisit.payload == first.payload, "the payload is the first response's record"
        assert "/000/" in first.payload.path
        assert recorded.body(revisit) == b"shared payload"
        assert ("Set-Cookie", "s=1") not in revisit.response_headers, "its own HTTP head"

    async def test_streamed_later_is_served_from_its_blob_same_commit_or_later(
        self, recorded: RecordedArchive
    ) -> None:
        for url, body in ((f"{SHOP}/now.js", b"n" * 5000), (f"{SHOP}/late.js", b"l" * 6000)):
            record = by_url(recorded)[url]
            assert (record.body_status, record.miss_reason) == ("streamed_later", None), url
            assert isinstance(record.payload, BlobPayload)
            assert recorded.body(record) == body
            assert record.payload_length == len(body)

    @pytest.mark.parametrize(
        ("path", "status", "reason"),
        [
            ("/huge", "too_large", "too_large"),
            ("/broken", "incomplete", "body_unavailable"),
            ("/never", "pending", "body_unavailable"),
        ],
    )
    async def test_recorded_but_unservable(
        self, recorded: RecordedArchive, path: str, status: str, reason: str
    ) -> None:
        record = by_url(recorded)[f"{SHOP}{path}"]
        assert (record.body_status, record.miss_reason, record.servable) == (status, reason, False)
        assert record.payload is None
        with pytest.raises(ReplayArchiveError, match="no response body"):
            recorded.body(record)

    async def test_a_websocket_is_never_a_candidate(self, recorded: RecordedArchive) -> None:
        assert not [r for r in recorded.index.records if r.url.startswith("ws")]

    async def test_a_304_is_kept_for_the_matcher_to_restrict(
        self, recorded: RecordedArchive
    ) -> None:
        assert by_url(recorded)[f"{SHOP}/cached"].status == 304

    async def test_a_206_lives_only_in_the_range_map(self, recorded: RecordedArchive) -> None:
        assert f"{SHOP}/video.mp4" not in by_url(recorded)
        key = recorded.index.ranges
        normalized = _normalized(f"{SHOP}/video.mp4")
        assert key.knows(normalized)
        cover = key.cover(normalized, 0, None)
        assert cover is not None and (cover.start, cover.end, cover.total) == (0, 6, 10)
        assert recorded.range_bytes(cover) == b"0123456"
        assert key.cover(normalized, 2, 5) is not None
        assert recorded.range_bytes(key.cover(normalized, 2, 5)) == b"2345"  # type: ignore[arg-type]
        assert key.cover(normalized, 7, None) is None, "bytes 7-9 were never recorded"


class TestTheIndexAsAWhole:
    async def test_the_fingerprint_and_t0(self, recorded: RecordedArchive) -> None:
        assert recorded.fingerprint is not None
        assert recorded.fingerprint["replayability"]["tier"] == "pinned"
        assert recorded.t0_ms == RECORDED_T0_MS
        assert recorded.fingerprint_sha256 is not None and len(recorded.fingerprint_sha256) == 64
        assert recorded.network_files == 3

    async def test_recording_order_is_request_start_order(self, recorded: RecordedArchive) -> None:
        records = list(recorded.index.records)
        assert [r.order for r in records] == sorted(r.order for r in records)
        assert records[0].url == f"{SHOP}/a?x=1"
        assert by_url(recorded)[f"{SHOP}/never"].order > by_url(recorded)[f"{SHOP}/a?x=1"].order

    async def test_keys_are_recomputed_with_the_rules_of_the_load(self, tmp_path: Path) -> None:
        recording = RecordingScript(tmp_path / "runs")
        await recording.exchange(f"{SHOP}/p?session=1&q=2")
        await recording.commit(0)
        reader = open_archive(recording.finish())
        plain = load_archive(reader).index.records[0]
        ruled = load_archive(
            reader, SiteRules({"shop.test": SiteRule(ignore_query=["session"])})
        ).index.records[0]
        assert plain.keys.exact == ruled.keys.exact
        assert plain.keys.normalized != ruled.keys.normalized
        assert "session" not in ruled.keys.normalized
        index = reader.archive_files(0, "browser.network")
        line = json.loads(reader.read_extra(next(e.path for e in index if e.name == INDEX_NAME)))
        assert not [key for key in line if "key" in key], "M29 stores raw facts, never a key"

    async def test_a_run_without_network_evidence_loads_empty(self, tmp_path: Path) -> None:
        recording = RecordingScript(tmp_path, fingerprint=False)
        await recording.commit(0)
        recorded = load_archive(open_archive(recording.finish()))
        assert (len(recorded.index), recorded.network_files, recorded.fingerprint) == (0, 0, None)
        assert (recorded.t0_ms, recorded.fingerprint_sha256) == (None, None)

    async def test_a_cut_or_misplaced_record_never_serves_other_bytes(
        self, recorded: RecordedArchive
    ) -> None:
        """Untrusted spans: the recorded bytes come back exactly, or a named ReplayArchiveError.

        A span cut inside the gzip trailer still decodes the whole payload
        (warcio does not check the CRC); its sha256 is checked, so those are
        the recorded bytes. Nothing else — no other exception, no other bytes.
        """
        record = by_url(recorded)[f"{SHOP}/a?x=1"]
        payload = record.payload
        assert isinstance(payload, WarcPayload)
        spans = [dataclasses.replace(payload, length=n) for n in range(1, payload.length)]
        spans += [dataclasses.replace(payload, offset=payload.offset + n) for n in range(1, 40)]
        refused = 0
        for span in spans:
            try:
                body = recorded.body(dataclasses.replace(record, payload=span))
            except ReplayArchiveError as exc:
                assert exc.record_id == record.record_id, span
                refused += 1
            else:
                assert body == b"shared payload", span
        assert refused >= len(spans) // 2, "most cuts lose the record outright"

    async def test_a_body_that_does_not_hash_to_its_index_entry_is_refused(
        self, recorded: RecordedArchive
    ) -> None:
        record = by_url(recorded)[f"{SHOP}/a?x=1"]
        forged = dataclasses.replace(record, payload_sha256="0" * 64)
        with pytest.raises(ReplayArchiveError, match="does not hash"):
            recorded.body(forged)


# ── archives that cannot be served ─────────────────────────────────────


async def _real_step_files(base: Path) -> tuple[list[dict[str, Any]], bytes]:
    """One real recorded commit: its index lines and its WARC bytes."""
    recording = RecordingScript(base / "src")
    await recording.exchange(f"{SHOP}/a", body=b"alpha")
    await recording.exchange(f"{SHOP}/b", body=b"alpha")  # a revisit within the step
    await recording.commit(0)
    reader = open_archive(recording.finish())
    files = {entry.name: entry for entry in reader.archive_files(0, "browser.network")}
    lines = [json.loads(line) for line in reader.read_extra(files[INDEX_NAME].path).splitlines()]
    return lines, reader.read_extra(files[WARC_NAME].path)


def _recommit(
    base: Path, lines: list[dict[str, Any]], warc: bytes | None, *raw_index: bytes
) -> Path:
    """A run whose step 0 holds *lines* (or raw index bytes) and *warc*."""
    wal = new_wal(base / "forged", "run-forged")
    index = (
        raw_index[0] if raw_index else b"".join(json.dumps(line).encode() + b"\n" for line in lines)
    )
    files = [
        archive_file(
            "browser.network", INDEX_NAME, index, step=0, media_type="application/x-ndjson"
        )
    ]
    if warc is not None:
        files.append(
            archive_file("browser.network", WARC_NAME, warc, step=0, media_type="application/warc")
        )
    wal.commit_step(0, step_record(0), extra_files=files)
    return wal.run_dir


class TestWhatFailsTheLoad:
    async def test_a_malformed_line_is_the_trace_layer_s_schema_error(self, tmp_path: Path) -> None:
        lines, warc = await _real_step_files(tmp_path)
        broken = json.dumps(lines[0]).encode() + b"\n{not json\n"
        run = _recommit(tmp_path, lines, warc, broken)
        with pytest.raises(TraceSchemaError, match="line 2 does not parse as JSON"):
            load_archive(open_archive(run))

    async def test_a_line_of_an_unknown_kind_is_a_schema_error(self, tmp_path: Path) -> None:
        lines, warc = await _real_step_files(tmp_path)
        run = _recommit(tmp_path, [{**lines[0], "kind": "mystery"}], warc)
        with pytest.raises(TraceSchemaError, match="line 1 does not parse as a IndexLine"):
            load_archive(open_archive(run))

    @pytest.mark.parametrize(
        ("mutate", "complaint"),
        [
            (
                lambda line: line["warc"].update(response=dict(line["warc"]["request"])),
                "is a 'request' record",
            ),
            (
                # Seven bytes of a gzip member: never a whole record, whatever its size.
                lambda line: line["warc"].update(response={"offset": 0, "length": 7}),
                r"no WARC record at offset 0 \(\+7\)",
            ),
            (
                lambda line: line.update(response_record_id="urn:uuid:not-this-one"),
                "not the response/revisit",
            ),
            (lambda line: line.update(record_id="urn:uuid:someone-else"), "not the request record"),
            # Three bytes into a gzip member. warcio usually finds no record there,
            # but about 3% of such cuts (the compressed bytes vary with the random
            # record ids) parse leniently as an id-less 'response' record, which the
            # type / id check refuses instead (measured: 63 of 2000). Either way the
            # load fails naming the offset.
            (
                lambda line: line["warc"]["request"].update(offset=3),
                r"(no WARC record|the WARC record) at offset 3",
            ),
            (lambda line: line["warc"].update(response=None), "has no response record"),
        ],
    )
    async def test_a_line_whose_warc_record_is_not_what_it_says(
        self, tmp_path: Path, mutate: Any, complaint: str
    ) -> None:
        lines, warc = await _real_step_files(tmp_path)
        first = json.loads(json.dumps(lines[0]))
        mutate(first)
        run = _recommit(tmp_path, [first], warc)
        with pytest.raises(ReplayArchiveError, match=complaint) as caught:
            load_archive(open_archive(run))
        message = str(caught.value)
        assert "source run 'run-forged'" in message and "exchanges.warc.gz" in message
        assert caught.value.record_id == first["record_id"]

    async def test_a_request_body_that_is_not_the_one_indexed(self, tmp_path: Path) -> None:
        recording = RecordingScript(tmp_path / "src")
        await recording.exchange(
            f"{SHOP}/form",
            method="POST",
            post=b"a=1",
            request_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        await recording.commit(0)
        reader = open_archive(recording.finish())
        files = {entry.name: entry for entry in reader.archive_files(0, "browser.network")}
        (line,) = [json.loads(x) for x in reader.read_extra(files[INDEX_NAME].path).splitlines()]
        line["request"]["post"]["sha256"] = "0" * 64
        run = _recommit(tmp_path, [line], reader.read_extra(files[WARC_NAME].path))
        with pytest.raises(ReplayArchiveError, match="request body in the WARC does not hash"):
            load_archive(open_archive(run))

    async def test_a_revisit_of_a_record_the_archive_does_not_hold(self, tmp_path: Path) -> None:
        lines, warc = await _real_step_files(tmp_path)
        revisit = next(line for line in lines if line["revisit_of"])
        run = _recommit(tmp_path, [revisit], warc)  # the first response's line is gone
        with pytest.raises(ReplayArchiveError, match="revisit refers to"):
            load_archive(open_archive(run))

    async def test_an_index_whose_commit_holds_no_warc(self, tmp_path: Path) -> None:
        lines, _ = await _real_step_files(tmp_path)
        run = _recommit(tmp_path, lines[:1], None)
        with pytest.raises(ReplayArchiveError, match="WARC its commit does not hold"):
            load_archive(open_archive(run))

    async def test_a_late_body_naming_a_blob_the_archive_does_not_hold(
        self, tmp_path: Path
    ) -> None:
        recording = RecordingScript(tmp_path / "src")
        await recording.streamed_later(f"{SHOP}/app.js", b"z" * 5000)
        await recording.commit(0)
        reader = open_archive(recording.finish())
        files = {entry.name: entry for entry in reader.archive_files(0, "browser.network")}
        lines = [json.loads(x) for x in reader.read_extra(files[INDEX_NAME].path).splitlines()]
        body = next(line for line in lines if line["kind"] == "body")
        body["payload_sha256"] = "f" * 64
        run = _recommit(tmp_path, lines, reader.read_extra(files[WARC_NAME].path))
        with pytest.raises(ReplayArchiveError, match="names blob"):
            load_archive(open_archive(run))

    async def test_an_orphan_index_is_never_loaded(self, tmp_path: Path) -> None:
        recording = RecordingScript(tmp_path)
        await recording.exchange(f"{SHOP}/committed")
        await recording.commit(0)
        run = recording.finish()
        lines, warc = await _real_step_files(tmp_path / "other")
        for name, data in (
            (INDEX_NAME, b"".join(json.dumps(x).encode() + b"\n" for x in lines)),
            (WARC_NAME, warc),
        ):
            orphan = run / archive_step_path(5, "browser.network", name)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(data)
        recorded = load_archive(open_archive(run))
        assert [r.url for r in recorded.index.records] == [f"{SHOP}/committed"]


def _normalized(url: str) -> str:
    return request_keys("GET", url, None, None).normalized
