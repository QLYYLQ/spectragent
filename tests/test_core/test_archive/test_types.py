"""What a collection hands over: validated at construction, frozen after.

``archiver_id`` and ``name`` become path segments of ``env/``, so a value
that would escape the run directory, make one path both a file and a
directory, or be longer than a filesystem allows, is refused where it is
built — at the capability — rather than half way through a WAL commit that
the close-out would then retry and fail again. So is any string the trace's
JSON index cannot hold (a lone surrogate has no UTF-8 encoding).
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest

from spectragent.core.archive import (
    MAX_SEGMENT_BYTES,
    RUN_RECORD_MEDIA_TYPE,
    ArchiveBatch,
    ArchiveContractError,
    ArchiveFile,
    ArchiveProvenance,
    segment_problem,
)

_META = ArchiveProvenance(captured_at=1_760_000_000.5, cycle=3, ids={"frame": "main"})


def _file(**overrides: Any) -> ArchiveFile:
    fields: dict[str, Any] = {
        "archiver_id": "cap.steps",
        "name": "page.json",
        "media_type": "application/json",
        "data": b"{}",
        "meta": _META,
    }
    fields.update(overrides)
    return ArchiveFile(**fields)


class TestProvenance:
    def test_defaults(self) -> None:
        meta = ArchiveProvenance(captured_at=1.0)
        assert meta.cycle is None
        assert dict(meta.ids) == {}

    def test_ids_are_copied_and_read_only(self) -> None:
        source = {"session": "s1"}
        meta = ArchiveProvenance(captured_at=1.0, ids=source)
        source["session"] = "changed"
        assert meta.ids["session"] == "s1"
        with pytest.raises(TypeError):
            meta.ids["session"] = "x"  # type: ignore[index]

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            _META.cycle = 4  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"captured_at": "now"}, TypeError),
            ({"captured_at": True}, TypeError),
            ({"captured_at": math.nan}, ValueError),
            ({"captured_at": math.inf}, ValueError),
            ({"captured_at": 1.0, "cycle": 1.5}, TypeError),
            ({"captured_at": 1.0, "cycle": True}, TypeError),
            ({"captured_at": 1.0, "ids": [("a", "b")]}, TypeError),
            ({"captured_at": 1.0, "ids": {"a": 1}}, TypeError),
            ({"captured_at": 1.0, "ids": {1: "a"}}, TypeError),
        ],
    )
    def test_refuses(self, kwargs: dict[str, Any], error: type[Exception]) -> None:
        with pytest.raises(error):
            ArchiveProvenance(**kwargs)

    def test_an_int_timestamp_is_a_number(self) -> None:
        assert ArchiveProvenance(captured_at=1_760_000_000).captured_at == 1_760_000_000

    @pytest.mark.parametrize("ids", [{"frame": "\udc80"}, {"\ud800": "main"}])
    def test_ids_must_be_utf8(self, ids: dict[str, str]) -> None:
        """They land in the JSON index; a lone surrogate would fail every commit carrying it."""
        with pytest.raises(ValueError, match="not encodable as UTF-8"):
            ArchiveProvenance(captured_at=1.0, ids=ids)


class TestArchiveFile:
    def test_a_step_file(self) -> None:
        f = _file()
        assert f.scope == "step"
        assert f.content_addressed is False
        assert f.capability_id == "cap"
        assert f.meta is _META

    def test_a_content_addressed_step_file(self) -> None:
        assert _file(content_addressed=True, name="body.bin").content_addressed

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            _file().name = "other"  # type: ignore[misc]

    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "nul\x00"])
    @pytest.mark.parametrize("field", ["archiver_id", "name"])
    def test_names_are_single_path_segments(self, field: str, bad: str) -> None:
        overrides = {field: bad}
        if field == "archiver_id":
            # Even a run record, which may use a bare capability id.
            overrides["scope"] = "run"
        with pytest.raises(ValueError, match="single path segment"):
            _file(**overrides)

    @pytest.mark.parametrize(
        ("field", "fits", "too_long"),
        [
            ("name", "n" * MAX_SEGMENT_BYTES, "n" * (MAX_SEGMENT_BYTES + 1)),
            # Bytes of UTF-8, not characters: 127 two-byte letters and one more byte fit.
            ("name", "é" * 127 + "n", "é" * 128),
            (
                "archiver_id",
                "cap." + "s" * (MAX_SEGMENT_BYTES - 4),
                "cap." + "s" * MAX_SEGMENT_BYTES,
            ),
        ],
    )
    def test_a_name_fits_a_file_name(self, field: str, fits: str, too_long: str) -> None:
        """At most 255 bytes of UTF-8 — what ext4 accepts, wherever the trace was written."""
        assert getattr(_file(**{field: fits}), field) == fits
        with pytest.raises(ValueError, match="longer than the 255"):
            _file(**{field: too_long})

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("name", "x\udc80y.json"),
            ("archiver_id", "cap.\ud800"),
            ("media_type", "application/\udc80"),
        ],
    )
    def test_text_must_be_utf8(self, field: str, bad: str) -> None:
        with pytest.raises(ValueError, match="not encodable as UTF-8"):
            _file(**{field: bad})

    def test_the_segment_rule_is_the_one_the_consumer_applies_again(self) -> None:
        assert segment_problem("page.json") is None
        for bad in ("a/b", "..", "n" * (MAX_SEGMENT_BYTES + 1), "x\udc80"):
            assert segment_problem(bad) is not None

    @pytest.mark.parametrize("field", ["archiver_id", "name", "media_type"])
    def test_text_fields_must_be_str(self, field: str) -> None:
        with pytest.raises(TypeError):
            _file(**{field: 7})

    @pytest.mark.parametrize("bad", ["cap", "cap.", ".steps"])
    def test_a_step_file_needs_a_capability_qualified_key(self, bad: str) -> None:
        with pytest.raises(ValueError, match="<capability>.<id>"):
            _file(archiver_id=bad)

    @pytest.mark.parametrize("media_type", ["json", "", "/json", "application/"])
    def test_media_type_is_type_slash_subtype(self, media_type: str) -> None:
        with pytest.raises(ValueError, match="media type"):
            _file(media_type=media_type)

    @pytest.mark.parametrize("data", ["text", bytearray(b"x"), memoryview(b"x"), None])
    def test_data_is_bytes(self, data: Any) -> None:
        with pytest.raises(TypeError, match="bytes"):
            _file(data=data)

    def test_meta_is_a_provenance(self) -> None:
        with pytest.raises(TypeError, match="ArchiveProvenance"):
            _file(meta={"captured_at": 1.0})

    def test_content_addressed_is_a_bool(self) -> None:
        with pytest.raises(TypeError):
            _file(content_addressed="yes")

    def test_scope_is_one_of_the_two(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            _file(scope="trailing")


class TestRunRecord:
    def test_owned_by_the_bare_capability_id(self) -> None:
        f = _file(archiver_id="cap", scope="run", name="fingerprint.json")
        assert f.capability_id == "cap"

    def test_or_by_one_of_its_archiver_keys(self) -> None:
        assert _file(archiver_id="cap.steps", scope="run").capability_id == "cap"

    def test_never_content_addressed(self) -> None:
        with pytest.raises(ValueError, match="content-addressed"):
            _file(archiver_id="cap", scope="run", content_addressed=True)

    def test_always_json(self) -> None:
        assert RUN_RECORD_MEDIA_TYPE == "application/json"
        with pytest.raises(ValueError, match="application/json"):
            _file(archiver_id="cap", scope="run", media_type="text/plain")

    @pytest.mark.parametrize("bad", ["cap.", ".steps"])
    def test_a_dotted_owner_is_a_whole_key(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _file(archiver_id=bad, scope="run")


class TestArchiveBatch:
    def test_empty(self) -> None:
        batch = ArchiveBatch()
        assert batch.files == ()
        assert batch.is_empty

    def test_a_list_is_stored_as_a_tuple(self) -> None:
        a, b = _file(name="a.json"), _file(name="b.json")
        batch = ArchiveBatch([a, b])  # type: ignore[arg-type]
        assert batch.files == (a, b)
        assert isinstance(batch.files, tuple)
        assert not batch.is_empty

    def test_only_archive_files(self) -> None:
        with pytest.raises(TypeError, match="ArchiveFile"):
            ArchiveBatch((b"raw",))  # type: ignore[arg-type]

    def test_merge_keeps_the_order_given(self) -> None:
        a, b, c = (_file(name=f"{n}.json") for n in "abc")
        merged = ArchiveBatch.merge([ArchiveBatch((a,)), ArchiveBatch(), ArchiveBatch((b, c))])
        assert merged.files == (a, b, c)

    def test_merge_of_nothing_is_empty(self) -> None:
        assert ArchiveBatch.merge([]).is_empty

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            ArchiveBatch().files = ()  # type: ignore[misc]


def test_a_contract_violation_is_a_value_error() -> None:
    """Callers that already handle bad values handle this one."""
    assert issubclass(ArchiveContractError, ValueError)
