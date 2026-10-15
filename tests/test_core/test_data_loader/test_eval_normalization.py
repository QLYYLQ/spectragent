"""``EvalLoaderBase`` has the same overridable ``normalize_raw`` hook.

Before this, ``_load`` hardcoded ``raw["task_id"]`` with no hook, while
the task side deliberately shipped the opposite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.eval._registrar import EvalLoaderBase
from spectragent.schemas.eval_reference import EvalReference


@dataclass
class _EvalLoader(EvalLoaderBase):
    __registry_key__ = "_test_eval_loader"


@dataclass
class _OverridingEvalLoader(EvalLoaderBase):
    """Proves the hook is a real extension point."""

    __registry_key__ = "_test_eval_override_loader"

    def normalize_raw(self, raw: dict[str, Any]) -> EvalReference:
        ref = super().normalize_raw(raw)
        ref.tags = [*ref.tags, "rewritten"]
        return ref


def _write(root: Path, body: str) -> None:
    (root / "data_info.json").write_text(json.dumps({"split": {"All": "e.jsonl"}}))
    (root / "e.jsonl").write_text(body)


class TestEvalNormalizeHook:
    def test_hook_exists_and_is_used_by_load(self, tmp_path: Path) -> None:
        _write(tmp_path, json.dumps({"task_id": "a", "tags": ["x"]}))
        loader = _OverridingEvalLoader(data_dir=str(tmp_path))
        assert loader["a"].tags == ["x", "rewritten"]

    def test_default_maps_the_standard_fields(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            json.dumps(
                {
                    "task_id": "a",
                    "difficulty": "hard",
                    "tags": ["L2"],
                    "metadata": {"canary": "c"},
                    "reference": {"steps": ["s1"], "scoring": {"threshold": 70}},
                }
            ),
        )
        ref = _EvalLoader(data_dir=str(tmp_path))["a"]
        assert ref.benchmark == "_test_eval_loader"
        assert ref.difficulty == "hard"
        assert ref.tags == ["L2"]
        assert ref.metadata == {"canary": "c"}
        assert ref.reference is not None
        assert ref.reference.steps == ["s1"]
        assert ref.reference.scoring is not None
        assert ref.reference.scoring.threshold == 70


class TestEvalIdentityFallback:
    def test_id_keyed_record_no_longer_keyerrors(self, tmp_path: Path) -> None:
        # The live landmine: upstream files key on ``id``, not ``task_id``.
        _write(tmp_path, json.dumps({"id": 1}))
        loader = _EvalLoader(data_dir=str(tmp_path))
        assert loader.task_ids() == ["1"]

    def test_record_with_no_identity_raises_clearly(self, tmp_path: Path) -> None:
        _write(tmp_path, json.dumps({"query": "no identity here"}))
        with pytest.raises(BenchmarkDataError, match="no task identity"):
            _EvalLoader(data_dir=str(tmp_path))


class TestEvalHardErrors:
    def test_missing_data_info_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BenchmarkDataError, match="No data_info.json"):
            _EvalLoader(data_dir=str(tmp_path))

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        _write(tmp_path, "")
        with pytest.raises(BenchmarkDataError, match="Parsed zero records"):
            _EvalLoader(data_dir=str(tmp_path))
