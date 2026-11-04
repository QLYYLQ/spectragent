"""LexBench-Browser's own record shape, as its public dataset writes it, reaches both loaders normalized.

The records here are written for the test in the dataset's shape (integer
``id``, ``query``, ``target_website``, ``reference_answer`` with
``score_threshold`` beside it, flat login flags); no file of the dataset is
read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.data_loader._lexbench_legacy import (
    is_legacy_record,
    normalize_legacy_record,
)
from spectragent.core.data_loader.eval.lexbench_browser import LexBenchBrowserEvalLoader
from spectragent.core.data_loader.task.lexbench_browser import LexBenchBrowserTaskLoader

_RUBRIC = {"total": 100, "items": [{"name": "found it", "score": 100, "description": "d"}]}


def _record(task_id: int, *, domain: str = "general", **extra: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "query": f"task {task_id}",
        "task_type": "T1",
        "domain": domain,
        "difficulty": "easy",
        "login_required": False,
        "login_type": "",
        "risk_control": False,
        "risk_control_types": [],
        "target_website": "www.example.com",
        "reasoning_type": "single_step",
        "language": "en",
        "website_region": "en",
        "reference_answer": {"steps": ["open it"], "key_points": [], "scoring": _RUBRIC},
        "score_threshold": 60,
        "robustness_tags": ["cookie_consent"],
        **extra,
    }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A subset ``All`` of three tasks, the last a safety-refusal task."""
    records = [_record(1), _record(2), _record(2001, domain="safety")]
    (tmp_path / "data_info.json").write_text(json.dumps({"split": {"All": "task.jsonl"}}))
    (tmp_path / "task.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return tmp_path


def test_a_task_reaches_the_task_loader_normalized(data_dir: Path) -> None:
    task = LexBenchBrowserTaskLoader(data_dir=str(data_dir))["1"]
    assert task.task_text == "task 1"
    assert task.setup is not None
    assert task.setup.to_context() == {"start_url": "https://www.example.com"}
    assert task.tags == ["single_step", "T1"]
    assert task.metadata["website_region"] == "en"


def test_the_pass_mark_reaches_the_rubric(data_dir: Path) -> None:
    reference = LexBenchBrowserEvalLoader(data_dir=str(data_dir))["1"].reference
    assert reference is not None and reference.scoring is not None
    assert reference.scoring.threshold == 60


class TestSafetyTasksAreScoredInReverse:
    """The dataset's safety-refusal tasks reach the reverse rubric, where refusing is correct.

    The verifier selects it from ``metadata["scoring_type"]``, which the
    dataset never states; its signal is the domain ``safety``.
    """

    def test_only_the_safety_tasks_are_marked(self, data_dir: Path) -> None:
        loader = LexBenchBrowserEvalLoader(data_dir=str(data_dir))
        marked = [t for t in loader.task_ids() if loader[t].metadata.get("scoring_type")]
        assert marked == ["2001"]
        assert loader["2001"].metadata["scoring_type"] == "reverse"

    def test_an_explicit_scoring_type_wins(self, data_dir: Path) -> None:
        """Derivation must not overwrite a value the data actually stated."""
        ref = LexBenchBrowserEvalLoader(data_dir=str(data_dir)).normalize_raw(
            {
                "task_id": "explicit",
                "benchmark": "lexbench_browser",
                "domain": "safety",
                "metadata": {"scoring_type": "normal"},
            }
        )
        assert ref.metadata["scoring_type"] == "normal"


class TestTheRecordShape:
    def test_normalized_records_pass_through_untouched(self) -> None:
        raw = {"task_id": "1", "task_text": "already normalized"}
        assert not is_legacy_record(raw)
        assert normalize_legacy_record(raw) is raw

    def test_the_datasets_records_are_detected(self) -> None:
        assert is_legacy_record({"id": 1, "query": "do a thing"})

    def test_unmapped_keys_land_in_metadata(self) -> None:
        out = normalize_legacy_record({"id": 7, "query": "q", "only_use": True, "novel_key": 1})
        assert out["metadata"]["only_use"] is True
        assert out["metadata"]["novel_key"] == 1

    def test_empty_login_type_becomes_none(self) -> None:
        # The dataset writes "" where normalized data writes null.
        out = normalize_legacy_record({"id": 7, "query": "q", "login_type": ""})
        assert out["setup"]["login_type"] is None
