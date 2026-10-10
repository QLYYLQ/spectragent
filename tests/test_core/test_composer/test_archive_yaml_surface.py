"""The archive's YAML surface: ``trace_writer.archive`` (§4.2.1, §5.4, §4.14).

``TraceWriter.archive`` is a receptor wired from the archiver registry, so the
configuration decides what a run records — each archiver with its own knobs,
validated against the generated union. Pinned here, against the real
Composer and the real registrars:

- the three cases of an optional list receptor: ``archive`` omitted with no
  top-level ``archiver:`` section records nothing; ``archive: []`` records
  nothing whatever else the file says; ``archive`` omitted beside a top-level
  ``archiver:`` section adopts every instance of it;
- an inline ``archive:`` list builds its archivers in place, knobs reaching the
  instances;
- a mistyped ``archiver_id`` and an unknown knob are refused by validation;
- a capability-level ``archiver:`` block is refused at compose time with the
  negotiation-only message — the capability *provides* archivers, it does not
  choose them;
- every archiver knob reaches the generated config.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.archive import ArchiverRegistrar
from spectragent.core.capabilities.browser.archive import (
    DomRawArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.composer import ConfigComposer, UnresolvedWiringError
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.scripts.generate_all import _collect_registrars

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "ui_tars_local.yaml"


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars=_collect_registrars())


def _base() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text("utf-8"))


def _writer(composer: ConfigComposer, raw: dict[str, Any]) -> TraceWriter:
    run = composer.compose(raw)
    (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]
    return writer


def _trace_entry(raw: dict[str, Any]) -> dict[str, Any]:
    (entry,) = [hook for hook in raw["hook"] if hook["hook_id"] == "trace_writer"]
    return entry


class TestWhatARunRecords:
    def test_omitted_without_a_top_level_section_records_nothing(
        self, composer: ConfigComposer
    ) -> None:
        assert _writer(composer, _base()).archive == []

    def test_an_empty_list_records_nothing_even_beside_a_top_level_section(
        self, composer: ConfigComposer
    ) -> None:
        raw = _base()
        raw["archiver"] = [{"archiver_id": "browser.storage"}]
        _trace_entry(raw)["archive"] = []
        assert _writer(composer, raw).archive == []

    def test_omitted_adopts_every_top_level_archiver(self, composer: ConfigComposer) -> None:
        raw = _base()
        raw["archiver"] = [
            {"archiver_id": "browser.network", "require_replayable": False, "drain_timeout_s": 0.5},
            {"archiver_id": "browser.storage", "allow_session_state": True},
        ]
        archive = _writer(composer, raw).archive
        assert [type(a) for a in archive] == [NetworkArchiver, StorageArchiver]
        assert archive[0].drain_timeout_s == 0.5  # type: ignore[attr-defined]
        assert archive[1].allow_session_state is True  # type: ignore[attr-defined]

    def test_an_inline_list_builds_its_archivers_in_place(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [
            {"archiver_id": "browser.dom_raw", "self_capture_timeout_s": 3.0},
            {"archiver_id": "browser.network", "strip_request_headers": ["X-Api-Key"]},
        ]
        archive = _writer(composer, raw).archive
        assert [a.archiver_id for a in archive] == ["browser.dom_raw", "browser.network"]
        assert archive[0].self_capture_timeout_s == 3.0  # type: ignore[attr-defined]
        assert archive[1].strip_request_headers == ["X-Api-Key"]  # type: ignore[attr-defined]


class TestWhatIsRefused:
    def test_a_mistyped_archiver_id(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [{"archiver_id": "browser.netwrok"}]
        with pytest.raises(ValidationError, match="archiver_id"):
            composer.compose(raw)

    def test_an_unknown_knob(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [{"archiver_id": "browser.storage", "allow_cookies": True}]
        with pytest.raises(ValidationError, match="allow_cookies"):
            composer.compose(raw)

    def test_an_out_of_range_inline_tier(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [
            {"archiver_id": "browser.network", "max_inline_body_bytes": 100 * 1024 * 1024}
        ]
        with pytest.raises(ValidationError, match="max_inline_body_bytes"):
            composer.compose(raw)

    def test_a_capability_level_archiver_block(self, composer: ConfigComposer) -> None:
        raw = _base()
        raw["environment"][0]["capabilities"][0]["archiver"] = {"archiver_id": "browser.network"}
        with pytest.raises(UnresolvedWiringError, match="negotiation-only entry"):
            composer.compose(copy.deepcopy(raw))


def test_every_archiver_knob_reaches_the_generated_config() -> None:
    from spectragent.generated import _composed

    for cls in (NetworkArchiver, StorageArchiver, DomRawArchiver):
        knobs = {name for name in inspect.signature(cls.__init__).parameters if name != "self"}
        model = next(
            getattr(_composed, attr)
            for attr in dir(_composed)
            if attr.endswith("ArchiverConfig")
            and getattr(getattr(_composed, attr), "model_fields", None)
            and getattr(_composed, attr).model_fields.get("archiver_id") is not None
            and getattr(_composed, attr).model_fields["archiver_id"].default == cls.__registry_key__
        )
        assert set(model.model_fields) - {"archiver_id"} == knobs, cls.__registry_key__
    assert set(ArchiverRegistrar.keys()) >= {
        "browser.network",
        "browser.storage",
        "browser.dom_raw",
    }
