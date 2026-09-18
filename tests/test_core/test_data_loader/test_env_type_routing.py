"""The loader's ``env_type`` drives both data path and setup variant.

Regression cover for the BLOCKER where every loader produced a
``BrowserSetup`` regardless of ``env_type``, so a SWE-bench record's
image / checkout commit / scoring test node-ids never reached the
capability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.schemas.task import BrowserSetup, DockerSetup, VMSetup

# ── Loaders registered once at import (conscribe rejects duplicate keys) ──


@dataclass
class _DockerLoader(TaskLoaderBase):
    """Stand-in for a future SWE-bench loader."""

    __registry_key__ = "_test_docker_loader"

    env_type: str = "docker"

    def _load(self) -> None:  # type: ignore[override]
        return

    def seed(self, raws: list[dict[str, Any]]) -> None:
        for raw in raws:
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task


@dataclass
class _VMLoader(TaskLoaderBase):
    """Stand-in for a future OSWorld loader."""

    __registry_key__ = "_test_vm_loader"

    env_type: str = "desktop"

    def _load(self) -> None:  # type: ignore[override]
        return

    def seed(self, raws: list[dict[str, Any]]) -> None:
        for raw in raws:
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task


@dataclass
class _BrowserLoader(TaskLoaderBase):
    __registry_key__ = "_test_browser_loader"

    def _load(self) -> None:  # type: ignore[override]
        return

    def seed(self, raws: list[dict[str, Any]]) -> None:
        for raw in raws:
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task


@dataclass
class _DiskLoader(TaskLoaderBase):
    """Loads for real, so the failure paths are actually exercised."""

    __registry_key__ = "_test_disk_loader"


# A real SWE-bench_Verified record, trimmed. ``FAIL_TO_PASS`` /
# ``PASS_TO_PASS`` ship JSON-encoded as strings in the HF dataset.
SWEBENCH_RECORD: dict[str, Any] = {
    "instance_id": "django__django-11333",
    "repo": "django/django",
    "base_commit": "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc",
    "environment_setup_commit": "419a78300f7cd27611196e1e464d50fd0385ff27",
    "version": "3.0",
    "problem_statement": "Optimize URLResolver.",
    "FAIL_TO_PASS": json.dumps(["test_a (tests.X)", "test_b (tests.X)"]),
    "PASS_TO_PASS": json.dumps(["test_c (tests.X)"]),
    "patch": "diff --git a/x b/x",
    "test_patch": "diff --git a/y b/y",
}

# A real OSWorld task record, trimmed.
OSWORLD_RECORD: dict[str, Any] = {
    "id": "94d95f96-9699-4208-98ba-3c3119edf9c2",
    "snapshot": "chrome",
    "task_text": "Make Bing the default search engine",
    "config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}],
    "related_apps": ["chrome"],
}


class TestDockerRouting:
    def test_docker_env_yields_docker_setup(self) -> None:
        loader = _DockerLoader()
        loader.seed([SWEBENCH_RECORD])
        task = loader["django__django-11333"]
        assert task.env_type == "docker"
        assert isinstance(task.setup, DockerSetup)
        assert task.setup.setup_type == "docker"

    def test_scoring_fields_land_typed_not_in_extras(self) -> None:
        loader = _DockerLoader()
        loader.seed([SWEBENCH_RECORD])
        setup = loader["django__django-11333"].setup
        assert isinstance(setup, DockerSetup)
        assert setup.repo == "django/django"
        assert setup.base_commit == "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc"
        assert setup.environment_setup_commit == ("419a78300f7cd27611196e1e464d50fd0385ff27")
        assert setup.version == "3.0"
        assert setup.instance_id == "django__django-11333"

    def test_uppercase_json_encoded_test_lists_decode(self) -> None:
        loader = _DockerLoader()
        loader.seed([SWEBENCH_RECORD])
        setup = loader["django__django-11333"].setup
        assert isinstance(setup, DockerSetup)
        assert setup.fail_to_pass == ["test_a (tests.X)", "test_b (tests.X)"]
        assert setup.pass_to_pass == ["test_c (tests.X)"]

    def test_plain_list_test_lists_also_accepted(self) -> None:
        loader = _DockerLoader()
        loader.seed([{"instance_id": "x", "FAIL_TO_PASS": ["a"], "PASS_TO_PASS": ["b"]}])
        setup = loader["x"].setup
        assert isinstance(setup, DockerSetup)
        assert setup.fail_to_pass == ["a"]
        assert setup.pass_to_pass == ["b"]

    def test_get_task_context_carries_the_container_facts(self) -> None:
        loader = _DockerLoader()
        loader.seed([SWEBENCH_RECORD])
        ctx = loader.get_task_context("django__django-11333")
        assert ctx["repo"] == "django/django"
        assert ctx["base_commit"] == "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc"
        assert ctx["fail_to_pass"] == ["test_a (tests.X)", "test_b (tests.X)"]
        assert ctx["pass_to_pass"] == ["test_c (tests.X)"]

    def test_instance_id_is_the_task_identity(self) -> None:
        loader = _DockerLoader()
        loader.seed([SWEBENCH_RECORD])
        assert loader.task_ids() == ["django__django-11333"]


class TestVMRouting:
    def test_desktop_env_yields_vm_setup(self) -> None:
        loader = _VMLoader()
        loader.seed([OSWORLD_RECORD])
        task = loader["94d95f96-9699-4208-98ba-3c3119edf9c2"]
        assert task.env_type == "desktop"
        assert isinstance(task.setup, VMSetup)
        assert task.setup.snapshot == "chrome"
        assert task.setup.related_apps == ["chrome"]
        assert len(task.setup.config) == 1

    def test_get_task_context_carries_the_vm_facts(self) -> None:
        loader = _VMLoader()
        loader.seed([OSWORLD_RECORD])
        ctx = loader.get_task_context("94d95f96-9699-4208-98ba-3c3119edf9c2")
        assert ctx["snapshot"] == "chrome"
        assert ctx["related_apps"] == ["chrome"]
        assert ctx["config"][0]["type"] == "launch"


class TestBrowserRoutingUnchanged:
    def test_browser_default_still_produces_browser_setup(self) -> None:
        loader = _BrowserLoader()
        loader.seed([{"task_id": "t1", "start_url": "https://example.com"}])
        task = loader["t1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://example.com"
        assert loader.get_task_context("t1")["start_url"] == "https://example.com"


class TestSetupTypeDerivation:
    def test_records_never_need_to_declare_setup_type(self) -> None:
        # No shipped data file carries ``setup_type``; it is derived.
        loader = _DockerLoader()
        loader.seed([{"instance_id": "x"}])
        assert loader["x"].setup.setup_type == "docker"

    def test_conflicting_declared_setup_type_raises(self) -> None:
        loader = _DockerLoader()
        with pytest.raises(BenchmarkDataError, match="setup_type"):
            loader.seed([{"instance_id": "x", "setup": {"setup_type": "browser"}}])

    def test_unknown_env_type_raises_instead_of_defaulting_to_browser(self) -> None:
        loader = _BrowserLoader()
        loader.env_type = "quantum_annealer"
        with pytest.raises(ValueError, match="No TaskSetup variant"):
            loader.seed([{"task_id": "t1"}])


class TestHardErrorsOnEmptyLoad:
    def _write(self, root: Path, split: dict[str, str], **files: str) -> None:
        (root / "data_info.json").write_text(json.dumps({"split": split}))
        for name, body in files.items():
            (root / name.replace("__", ".")).write_text(body)

    def test_missing_data_info_raises_at_construction(self, tmp_path: Path) -> None:
        # Previously: warning + a silently zero-length loader.
        with pytest.raises(BenchmarkDataError, match="No data_info.json"):
            _DiskLoader(data_dir=str(tmp_path))

    def test_unknown_subset_raises_at_construction(self, tmp_path: Path) -> None:
        self._write(tmp_path, {"All": "tasks.jsonl"}, tasks__jsonl='{"task_id": "a"}')
        with pytest.raises(BenchmarkDataError, match="not in the split map"):
            _DiskLoader(subset="Hard", data_dir=str(tmp_path))

    def test_empty_data_file_raises_at_construction(self, tmp_path: Path) -> None:
        self._write(tmp_path, {"All": "tasks.jsonl"}, tasks__jsonl="")
        with pytest.raises(BenchmarkDataError, match="Parsed zero records"):
            _DiskLoader(data_dir=str(tmp_path))

    def test_valid_dir_loads(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            {"All": "tasks.jsonl"},
            tasks__jsonl='{"task_id": "a", "start_url": "https://e.com"}',
        )
        loader = _DiskLoader(data_dir=str(tmp_path))
        assert len(loader) == 1
