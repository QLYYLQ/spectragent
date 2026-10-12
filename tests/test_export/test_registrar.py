"""``ExporterRegistrar``: an optional layer of the one run root, its job fields typed (arch §4.15).

af-regenerate-and-verify check 1 for the exporter: the generated per-key config
carries every job field and the nested ``translator`` union; an
``exporter:``-only YAML composes (no agent, no environment); a typo anywhere
in the block is refused rather than defaulted.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from spectragent.core.composer import ConfigComposer
from spectragent.core.data_loader.task.require_eval import RequireEval
from spectragent.export import (
    Exporter,
    ExporterBase,
    ExporterRegistrar,
    IdentityTranslator,
    IndexToCoordinateTranslator,
    OpenAIMessagesExporter,
)
from spectragent.export.settings import (
    BenchmarkRef,
    Decontamination,
    ExportFilters,
    LengthBudget,
    SplitSettings,
)
from spectragent.generated._composed import ComposedConfig
from spectragent.scripts.generate_all import _collect_registrars

#: The arch §4.15 YAML, as a dict.
ENTRY: dict[str, Any] = {
    "exporter_id": "openai_messages",
    "sample": "per_step",
    "image_store": "files",
    "sources_root": "traces",
    "sources": ["recorded/*"],
    "filters": {
        "require_status": "completed",
        "require_eval": None,
        "exclude_restoration_diverged": True,
        "max_document_misses": 0,
        "exclude_sites": [],
    },
    "split": {"by": "site", "validation_fraction": 0.1, "salt": "af-split-1"},
    "decontamination": {"against": [], "match": "either"},
    "translator": {
        "translator_id": "index_to_coordinate",
        "occlusion": "check",
        "occlusion_unknown": "raise",
        "untranslatable": "drop_step",
    },
    "student_run_config": "configs/flywheel/ui_tars_student.yaml",
    "thought_source": "teacher",
    "thought_index_refs": "drop",
    "length": {"max_tokens": 32768, "image_tokens": "qwen_vl"},
    "output_dir": "datasets/sft_v1",
}
#: Every job field of arch §4.15, on the base signature.
JOB_FIELDS = {
    "sample",
    "image_store",
    "sources_root",
    "sources",
    "filters",
    "split",
    "decontamination",
    "student_run_config",
    "thought_source",
    "thought_index_refs",
    "length",
    "output_dir",
}


def _compose(composer: ConfigComposer, entry: dict[str, Any]) -> Any:
    return composer.compose({"exporter": [copy.deepcopy(entry)]})


def test_registered_and_collected() -> None:
    registrars = _collect_registrars()
    assert registrars["exporter"] is ExporterRegistrar
    assert sorted(ExporterRegistrar.keys()) == ["openai_messages"]
    order = list(registrars)
    assert order.index("action_translator") < order.index("exporter"), (
        "the translator layer is collected first, so the exporter block nests its union"
    )


def test_the_generated_config_carries_the_job_and_the_translator_union() -> None:
    field = ComposedConfig.model_fields["exporter"]
    (layer,) = get_args(field.annotation)
    config_cls = layer if isinstance(layer, type) else get_args(layer)[0]
    fields = config_cls.model_fields
    assert JOB_FIELDS | {"translator", "exporter_id"} == set(fields)
    translators = {
        get_args(member.model_fields["translator_id"].annotation)[0]
        for member in get_args(fields["translator"].annotation)
    }
    assert translators == {"identity", "index_to_coordinate"}
    assert fields["student_run_config"].is_required() and fields["output_dir"].is_required()


def test_an_exporter_only_yaml_composes_and_every_knob_arrives(composer: ConfigComposer) -> None:
    run = _compose(composer, ENTRY)
    assert run.ids("agent") == [] and run.ids("environment") == []
    exporter = run.single("exporter")
    assert isinstance(exporter, OpenAIMessagesExporter) and isinstance(exporter, Exporter)
    assert exporter.translator is run.single("action_translator")
    assert isinstance(exporter.translator, IndexToCoordinateTranslator)
    assert exporter.sources == ("recorded/*",)
    assert exporter.filters == ExportFilters()
    assert exporter.split == SplitSettings()
    assert exporter.decontamination == Decontamination()
    assert exporter.length == LengthBudget(max_tokens=32768)
    assert (exporter.student_run_config, exporter.output_dir) == (
        "configs/flywheel/ui_tars_student.yaml",
        "datasets/sft_v1",
    )
    assert exporter.root() == (Path.cwd() / "traces").resolve()


def test_a_verdict_filter_still_arrives_when_written(composer: ConfigComposer) -> None:
    """The evaluator is dormant, not deleted: ``require_eval`` is optional, null by default."""
    entry = copy.deepcopy(ENTRY)
    entry["filters"]["require_eval"] = {"evaluator_id": "online_mind2web", "label": 1, "slug": None}
    exporter = _compose(composer, entry).single("exporter")
    assert exporter.filters.require_eval == RequireEval(evaluator_id="online_mind2web", label=1)


def test_nulls_are_the_documented_defaults(composer: ConfigComposer) -> None:
    entry = {
        "exporter_id": "openai_messages",
        "translator": {"translator_id": "identity"},
        "student_run_config": "s.yaml",
        "output_dir": "out",
    }
    exporter = _compose(composer, entry).single("exporter")
    assert exporter.sources == ("*",) and exporter.sources_root is None
    assert exporter.root() == (Path.cwd() / "traces").resolve()
    assert exporter.filters == ExportFilters() and exporter.filters.max_document_misses == 0
    assert exporter.split == SplitSettings(by="site", validation_fraction=0.1, salt="af-split-1")
    assert exporter.decontamination.against == ()
    assert (exporter.thought_source, exporter.thought_index_refs) == ("teacher", "drop")
    assert exporter.length is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("translator", "translator_id"), "index_to_coordinates"),
        (("filters", "require_statu"), "any"),
        (("split", "by"), "step"),
        (("thought_source",), "relabel"),
        (("decontamination", "against"), [{"benchmark": "x", "subst": "y"}]),
        (("decontamination", "against"), None),  # [] means none; null is no list
        (("length", "image_tokens"), "fixed"),  # fixed needs fixed_image_tokens
        (("filters", "max_document_misses"), -1),
        (("split", "validation_fraction"), 1.5),
    ],
)
def test_a_wrong_knob_is_refused(
    composer: ConfigComposer, path: tuple[str, ...], value: Any
) -> None:
    entry = copy.deepcopy(ENTRY)
    node: Any = entry
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises((ValidationError, ValueError)):
        _compose(composer, entry)


@pytest.mark.parametrize("missing", ["student_run_config", "output_dir", "translator"])
def test_a_required_field_is_required(composer: ConfigComposer, missing: str) -> None:
    entry = copy.deepcopy(ENTRY)
    del entry[missing]
    with pytest.raises(ValidationError):
        _compose(composer, entry)


@pytest.mark.parametrize("glob", ["/abs/*", "~/runs/*", "../elsewhere/*", ""])
def test_a_source_glob_must_stay_under_the_root(glob: str) -> None:
    with pytest.raises(ValueError, match="source glob"):
        OpenAIMessagesExporter(student_run_config="s.yaml", output_dir="out", sources=[glob])


def test_built_by_hand_the_translator_is_identity() -> None:
    exporter = OpenAIMessagesExporter(student_run_config="s.yaml", output_dir="out")
    assert isinstance(exporter.translator, IdentityTranslator)
    exporter = OpenAIMessagesExporter(
        student_run_config="s.yaml",
        output_dir="out",
        decontamination=Decontamination(against=(BenchmarkRef(benchmark="b"),)),
    )
    assert exporter.decontamination.against == (BenchmarkRef(benchmark="b"),)


def test_an_exporter_without_knobs_writes_no_init() -> None:
    """conscribe reads the base signature: one place declares the job fields."""
    assert "__init__" not in vars(OpenAIMessagesExporter)
    assert set(inspect.signature(ExporterBase.__init__).parameters) - {"self"} == JOB_FIELDS
