"""``lexbench_browser/tasks_private.jsonl`` loads — against the real file.

That file is the live landmine called out in the audit: it ships in the
upstream LexBench shape (integer ``id``, ``query``, ``target_website``,
flat login flags, ``score_threshold`` at the top level), so both loaders
used to raise before it was even reachable.

The strongest available check is a golden one: ``tasks.jsonl`` in the same
directory is the already-normalized form of a superset of these records,
so normalizing the upstream file must reproduce it field for field.
"""

from __future__ import annotations

from spectragent.core.data_loader._lexbench_legacy import (
    is_legacy_record,
    normalize_legacy_record,
)
from spectragent.core.data_loader.eval.lexbench_browser import (
    LexBenchBrowserEvalLoader,
)
from spectragent.core.data_loader.task.lexbench_browser import (
    LexBenchBrowserTaskLoader,
)
from spectragent.schemas.task import BrowserSetup

#: Fields that must agree between the upstream and normalized shapes.
_GOLDEN_FIELDS = (
    "task_id",
    "benchmark",
    "env_type",
    "task_text",
    "language",
    "setup",
    "difficulty",
    "domain",
    "tags",
    "reference",
)


class TestPrivateSplitLoads:
    def test_task_loader_loads_the_private_split(self) -> None:
        loader = LexBenchBrowserTaskLoader(subset="private")
        assert len(loader) == 198

    def test_eval_loader_loads_the_private_split(self) -> None:
        loader = LexBenchBrowserEvalLoader(subset="private")
        assert len(loader) == 198

    def test_integer_ids_become_string_task_ids(self) -> None:
        loader = LexBenchBrowserTaskLoader(subset="private")
        assert all(isinstance(tid, str) for tid in loader.task_ids())
        assert "1" in loader.task_ids()

    def test_upstream_fields_are_mapped_not_dropped(self) -> None:
        task = LexBenchBrowserTaskLoader(subset="private")["1"]
        assert task.task_text  # from ``query``
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url  # from ``target_website``
        assert task.tags  # from scenario_tier/reasoning_type/task_type
        assert task.reference is not None  # from ``reference_answer``
        assert task.reference.scoring is not None
        # ``score_threshold`` moves into the rubric, where graders read it.
        assert task.reference.scoring.threshold == task.metadata["score_threshold"]


class TestGoldenEquivalenceWithNormalizedFile:
    def test_every_shared_task_normalizes_identically(self) -> None:
        private = LexBenchBrowserTaskLoader(subset="private")
        public = LexBenchBrowserTaskLoader(subset="All")

        shared = set(private.task_ids()) & set(public.task_ids())
        assert len(shared) == 198, "expected the private split to be a subset"

        mismatches = [
            (task_id, name)
            for task_id in sorted(shared)
            for name in _GOLDEN_FIELDS
            if getattr(private[task_id], name) != getattr(public[task_id], name)
        ]
        assert mismatches == []

    def test_eval_references_match_too(self) -> None:
        private = LexBenchBrowserEvalLoader(subset="private")
        public = LexBenchBrowserEvalLoader(subset="All")
        shared = set(private.task_ids()) & set(public.task_ids())
        assert len(shared) == 198
        for task_id in sorted(shared):
            assert private[task_id].reference == public[task_id].reference
            assert private[task_id].tags == public[task_id].tags


class TestLegacyDetection:
    def test_normalized_records_pass_through_untouched(self) -> None:
        raw = {"task_id": "1", "task_text": "already normalized"}
        assert not is_legacy_record(raw)
        assert normalize_legacy_record(raw) is raw

    def test_upstream_records_are_detected(self) -> None:
        assert is_legacy_record({"id": 1, "query": "do a thing"})

    def test_unmapped_upstream_keys_land_in_metadata(self) -> None:
        out = normalize_legacy_record(
            {"id": 7, "query": "q", "website_region": "zh", "novel_key": 1}
        )
        assert out["metadata"]["website_region"] == "zh"
        assert out["metadata"]["novel_key"] == 1

    def test_empty_login_type_becomes_none(self) -> None:
        # Upstream writes "" where normalized data writes null.
        out = normalize_legacy_record({"id": 7, "query": "q", "login_type": ""})
        assert out["setup"]["login_type"] is None
