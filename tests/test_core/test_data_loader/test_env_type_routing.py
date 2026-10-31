"""The loader's ``env_type`` drives both data path and setup class.

Regression cover for the BLOCKER where every loader produced a
``BrowserSetup`` regardless of ``env_type``, so a container record's image
and checkout never reached the capability. The setup classes are the ones
capability packages register (``docs/architecture_design.md`` §4.10); a
second capability's setup is stood in for by a test-registered one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conscribe.testing import isolated_registries

from spectragent.core import registries
from spectragent.core.capabilities.browser.task_setup import BrowserSetup
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.registries import setup_class_for_env
from spectragent.schemas.task import SetupBase

# ── Loaders registered once at import (conscribe rejects duplicate keys) ──


@dataclass
class _ContainerLoader(TaskLoaderBase):
    """Stand-in for a loader of a capability that does not exist yet."""

    __registry_key__ = "_test_container_loader"

    env_type: str = "container_for_tests"

    def _load(self) -> None:  # type: ignore[override]
        return

    def seed(self, raws: list[dict[str, Any]]) -> None:
        for raw in raws:
            task = self.normalize_raw(raw)
            self._tasks[task.task_id] = task


@dataclass
class _BrowserLoader(TaskLoaderBase):
    __registry_key__ = "_test_browser_loader"

    def _load(self) -> None:  # type: ignore[override]
        return

    def seed(self, raws: list[dict[str, Any]]) -> None:
        for raw in raws:
            task = self.normalize_raw(raw)
            self._tasks[task.task_id] = task


@dataclass
class _DiskLoader(TaskLoaderBase):
    """Loads for real, so the failure paths are actually exercised."""

    __registry_key__ = "_test_disk_loader"


#: The setup a second capability package would register, activated per test.
FAKES = isolated_registries()

with FAKES:

    class _ContainerSetup(SetupBase):
        __registry_key__ = "container_for_tests"
        __env_types__: ClassVar[tuple[str, ...]] = ("container_for_tests",)
        __raw_keys__: ClassVar[tuple[str, ...]] = ("image", "base_commit")

        image: str | None = None
        base_commit: str | None = None

        def to_context(self) -> dict[str, Any]:
            return {key: value for key, value in self.model_dump().items() if key != "setup_type"}


@pytest.fixture
def container_setup() -> Iterator[None]:
    with FAKES.activate():
        yield


CONTAINER_RECORD: dict[str, Any] = {
    "instance_id": "django__django-11333",
    "image": "swebench/sweb.eval.x86_64.django_1776_django-11333",
    "base_commit": "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc",
    "problem_statement": "Optimize URLResolver.",
}


@pytest.mark.usefixtures("container_setup")
class TestRoutingToARegisteredSetup:
    def test_the_env_type_selects_the_registered_setup(self) -> None:
        loader = _ContainerLoader()
        loader.seed([CONTAINER_RECORD])
        task = loader["django__django-11333"]
        assert task.env_type == "container_for_tests"
        assert isinstance(task.setup, _ContainerSetup)
        assert task.setup.setup_type == "container_for_tests"

    def test_raw_keys_land_typed_not_in_extras(self) -> None:
        loader = _ContainerLoader()
        loader.seed([CONTAINER_RECORD])
        task = loader["django__django-11333"]
        assert isinstance(task.setup, _ContainerSetup)
        assert task.setup.base_commit == "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc"
        assert "base_commit" not in (task.model_extra or {})
        assert (task.model_extra or {})["problem_statement"] == "Optimize URLResolver."

    def test_get_task_context_carries_the_setup_context(self) -> None:
        loader = _ContainerLoader()
        loader.seed([CONTAINER_RECORD])
        context = loader.get_task_context("django__django-11333").env
        assert context["image"].startswith("swebench/")
        assert context["base_commit"] == "55b68de643b5c2d5f0a8ea7587ab3b2966021ccc"

    def test_records_never_need_to_declare_setup_type(self) -> None:
        # No shipped data file carries ``setup_type``; it is derived.
        loader = _ContainerLoader()
        loader.seed([{"instance_id": "x"}])
        assert loader["x"].setup is not None
        assert loader["x"].setup.setup_type == "container_for_tests"

    def test_conflicting_declared_setup_type_raises(self) -> None:
        loader = _ContainerLoader()
        with pytest.raises(BenchmarkDataError, match="setup_type"):
            loader.seed([{"instance_id": "x", "setup": {"setup_type": "browser"}}])


class TestBrowserRouting:
    def test_browser_default_still_produces_browser_setup(self) -> None:
        loader = _BrowserLoader()
        loader.seed([{"task_id": "t1", "start_url": "https://example.com"}])
        task = loader["t1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://example.com"
        assert loader.get_task_context("t1").env["start_url"] == "https://example.com"

    def test_unknown_env_type_raises_instead_of_defaulting_to_browser(self) -> None:
        loader = _BrowserLoader()
        loader.env_type = "quantum_annealer"
        with pytest.raises(ValueError, match="No registered task setup claims"):
            loader.seed([{"task_id": "t1"}])


class TestSetupClassForEnv:
    def test_the_browser_env_selects_the_browser_setup(self) -> None:
        assert setup_class_for_env("browser") is BrowserSetup

    def test_an_unknown_env_type_names_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match=r"env_type='mobile'.*known: browser"):
            setup_class_for_env("mobile")

    def test_a_registered_setup_is_found_without_reading_every_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A package still being written, whose declarations fail to import,
        # must not break the lookup of a setup that is already registered.
        def unreadable(*args: object, **kwargs: object) -> None:
            raise ImportError("a capability package's declarations failed to import")

        monkeypatch.setattr(registries, "capability_declarations", unreadable)
        assert setup_class_for_env("browser") is BrowserSetup

    def test_a_loader_finds_the_setup_without_importing_the_capability(self) -> None:
        # In a fresh interpreter nothing has imported the browser package: the
        # lookup reads the capabilities' declarations, which registers the setup.
        probe = """
import sys
from spectragent.core.registries import setup_class_for_env
assert "spectragent.core.capabilities.browser" not in sys.modules
setup = setup_class_for_env("browser")
assert setup.__module__ == "spectragent.core.capabilities.browser.task_setup", setup
"""
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


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
