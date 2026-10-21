"""The archive's YAML surface: ``trace_writer.archive`` (§4.2.1, §5.4, §4.14).

``TraceWriter.archive`` is a receptor wired from the archiver registry, so the
configuration decides what a run records — each archiver with its own knobs,
validated against the generated union. Pinned here, against the real
Composer and the real registrars:

- archiving has one switch: ``archive`` omitted, ``null`` or ``[]`` records
  nothing, and a top-level ``archiver:`` section is refused with a message
  naming ``trace_writer.archive`` — beside an omitted archive, an empty one or
  an inline list alike (``ArchiverRegistrar.inline_only``);
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
    FramesArchiver,
    NetworkArchiver,
    ReplayLogArchiver,
    StorageArchiver,
)
from spectragent.core.composer import (
    ConfigComposer,
    UnresolvedWiringError,
)
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.registries import collect_registrars

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "ui_tars_local.yaml"


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars=collect_registrars())


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
    def test_omitted_records_nothing(self, composer: ConfigComposer) -> None:
        assert "archive" not in _trace_entry(_base())
        assert _writer(composer, _base()).archive == []

    @pytest.mark.parametrize("value", [None, []], ids=["null", "empty-list"])
    def test_null_and_an_empty_list_record_nothing(
        self, composer: ConfigComposer, value: list[Any] | None
    ) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = value
        assert _writer(composer, raw).archive == []

    def test_a_writer_that_omits_it_never_takes_another_writers_archivers(
        self, composer: ConfigComposer
    ) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [{"archiver_id": "browser.storage"}]
        raw["hook"].append({"hook_id": "trace_writer", "trace_dir": "elsewhere"})
        run = composer.compose(raw)
        writers = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]
        assert [[a.archiver_id for a in w.archive] for w in writers] == [["browser.storage"], []]

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

    def test_the_frames_knobs_reach_the_instance(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [
            {
                "archiver_id": "browser.frames",
                "before_action": False,
                "after_observation_delay_s": 0.5,
                "capture_timeout_s": 2.0,
            }
        ]
        (frames,) = _writer(composer, raw).archive
        assert isinstance(frames, FramesArchiver)
        assert (frames.before_action, frames.after_observation_delay_s) == (False, 0.5)
        assert (frames.capture_timeout_s, frames.allow_session_state) == (2.0, False)

    def test_null_turns_the_after_observation_frame_off(self, composer: ConfigComposer) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [
            {"archiver_id": "browser.frames", "after_observation_delay_s": None}
        ]
        (frames,) = _writer(composer, raw).archive
        assert frames.after_observation_delay_s is None  # type: ignore[attr-defined]
        assert frames.before_action is True  # type: ignore[attr-defined]


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "inline",
        ["omitted", None, [], [{"archiver_id": "browser.dom_raw"}]],
        ids=["archive-omitted", "archive-null", "archive-empty", "archive-inline"],
    )
    def test_a_top_level_archiver_section(self, composer: ConfigComposer, inline: Any) -> None:
        raw = _base()
        raw["archiver"] = [
            {"archiver_id": "browser.network", "require_replayable": False},
            {"archiver_id": "browser.storage"},
        ]
        if inline != "omitted":
            _trace_entry(raw)["archive"] = inline
        with pytest.raises(ValidationError) as refused:
            composer.compose(raw)
        message = str(refused.value)
        assert "inline-only" in message
        assert "hook[trace_writer].archive" in message
        assert refused.value.errors()[0]["type"] == "inline_only_layer"

    @pytest.mark.parametrize("value", [[], None])
    def test_an_empty_top_level_section_is_refused(self, composer: ConfigComposer, value) -> None:
        raw = _base()
        raw["archiver"] = value
        with pytest.raises(ValidationError, match="inline-only"):
            composer.compose(raw)

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

    @pytest.mark.parametrize(
        "knob", [{"after_observation_delay_s": 0}, {"capture_timeout_s": -1.0}], ids=str
    )
    def test_a_non_positive_frames_time(
        self, composer: ConfigComposer, knob: dict[str, float]
    ) -> None:
        raw = _base()
        _trace_entry(raw)["archive"] = [{"archiver_id": "browser.frames", **knob}]
        with pytest.raises(ValidationError, match=next(iter(knob))):
            composer.compose(raw)

    def test_a_capability_level_archiver_block(self, composer: ConfigComposer) -> None:
        raw = _base()
        raw["environment"][0]["capabilities"][0]["archiver"] = "browser.network"
        with pytest.raises(UnresolvedWiringError, match="negotiation-only entry"):
            composer.compose(copy.deepcopy(raw))


def test_every_archiver_knob_reaches_the_generated_config() -> None:
    from spectragent.generated import _composed

    for cls in (
        NetworkArchiver,
        StorageArchiver,
        DomRawArchiver,
        FramesArchiver,
        ReplayLogArchiver,
    ):
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
        "browser.frames",
        "browser.replay_log",
    }
