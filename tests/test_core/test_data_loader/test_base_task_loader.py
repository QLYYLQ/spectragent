"""Tests for BaseTaskLoader Protocol, TaskLoaderBase, and TaskLoaderRegistrar."""

from __future__ import annotations

from dataclasses import dataclass

from spectragent.core.data_loader.task._registrar import (
    TaskLoaderBase,
    TaskLoaderRegistrar,
)
from spectragent.core.data_loader.task.base import BaseTaskLoader
from spectragent.schemas.task import BrowserSetup, TaskContext, TaskDefinition

# ── Hand-rolled in-memory loader for tests ─────────────────────────────


@dataclass
class _InMemoryLoader(TaskLoaderBase):
    """Loader that bypasses ``_load()`` so tests don't touch disk."""

    __registry_key__ = "_inmem_test_loader"

    def _load(self) -> None:  # type: ignore[override]
        # Override to skip disk loading; populate via _seed() in tests
        return

    def _seed(self, raws: list[dict]) -> None:
        for raw in raws:
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task


class TestProtocolConformance:
    def test_inmemory_loader_satisfies_protocol(self) -> None:
        loader = _InMemoryLoader()
        assert isinstance(loader, BaseTaskLoader)

    def test_existing_loaders_satisfy_protocol(self) -> None:
        # Import to trigger registration
        from spectragent.core.data_loader.task.browsecomp import (
            BrowseCompTaskLoader,
        )

        # We don't instantiate (would require data files) but we verify
        # the class structure conforms.
        # Use the in-memory loader to actually exercise the methods.
        loader = _InMemoryLoader()
        assert isinstance(loader, BaseTaskLoader)
        # And verify the real classes have the necessary class members
        assert hasattr(BrowseCompTaskLoader, "_normalize_raw")
        assert hasattr(BrowseCompTaskLoader, "get_task_context")


class TestRegistrar:
    def test_registrar_name(self) -> None:
        # Hard-cutover: registrar must be named "task_loader"
        # (build_config returns the layer name in its result)
        result = TaskLoaderRegistrar.build_config()
        assert result.layer_name == "task_loader"

    def test_three_browser_loaders_registered(self) -> None:
        # Triggers registration via __init__
        import spectragent.core.data_loader.task  # noqa: F401

        keys = TaskLoaderRegistrar.keys()
        assert "browsecomp" in keys
        assert "lexbench_browser" in keys
        assert "online_mind2web" in keys


class TestNormalization:
    def test_minimal_raw(self) -> None:
        loader = _InMemoryLoader()
        loader._seed([{"task_id": "t1"}])
        task = loader["t1"]
        assert isinstance(task, TaskDefinition)
        assert task.task_id == "t1"
        assert task.benchmark == loader.benchmark_name
        assert task.env_type == "browser"
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url is None

    def test_top_level_start_url_routes_to_setup(self) -> None:
        loader = _InMemoryLoader()
        loader._seed(
            [
                {
                    "task_id": "t1",
                    "start_url": "https://example.com",
                }
            ]
        )
        task = loader["t1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://example.com"

    def test_nested_setup_dict_preserved(self) -> None:
        loader = _InMemoryLoader()
        loader._seed(
            [
                {
                    "task_id": "t1",
                    "setup": {
                        "login_required": True,
                        "login_type": "sso",
                    },
                }
            ]
        )
        task = loader["t1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.login_required is True
        assert task.setup.login_type == "sso"

    def test_top_level_start_url_merges_with_nested_setup(self) -> None:
        loader = _InMemoryLoader()
        loader._seed(
            [
                {
                    "task_id": "t1",
                    "start_url": "https://flights.example.com",
                    "setup": {"login_required": True},
                }
            ]
        )
        task = loader["t1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://flights.example.com"
        assert task.setup.login_required is True

    def test_unknown_fields_preserved_via_extra_allow(self) -> None:
        loader = _InMemoryLoader()
        loader._seed(
            [
                {
                    "task_id": "t1",
                    "weird_extra": "preserved",
                }
            ]
        )
        task = loader["t1"]
        # extra="allow" surfaces unknown fields as attributes
        assert task.weird_extra == "preserved"  # type: ignore[attr-defined]


class TestGetTaskContext:
    def test_browser_task_returns_start_url(self) -> None:
        loader = _InMemoryLoader()
        loader._seed(
            [
                {
                    "task_id": "t1",
                    "start_url": "https://example.com",
                }
            ]
        )
        ctx = loader.get_task_context("t1")
        assert ctx == TaskContext(env={"start_url": "https://example.com"})

    def test_browser_task_without_start_url_returns_empty(self) -> None:
        loader = _InMemoryLoader()
        loader._seed([{"task_id": "t1"}])
        ctx = loader.get_task_context("t1")
        assert ctx == TaskContext()

    def test_unknown_task_id_returns_empty(self) -> None:
        loader = _InMemoryLoader()
        ctx = loader.get_task_context("nonexistent")
        assert ctx == TaskContext()


class TestQueryInterface:
    def test_len_and_task_ids(self) -> None:
        loader = _InMemoryLoader()
        loader._seed([{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}])
        assert len(loader) == 3
        assert set(loader.task_ids()) == {"a", "b", "c"}

    def test_get_returns_none_for_missing(self) -> None:
        loader = _InMemoryLoader()
        assert loader.get("nonexistent") is None

    def test_getitem_raises_for_missing(self) -> None:
        loader = _InMemoryLoader()
        try:
            _ = loader["nonexistent"]
        except KeyError:
            pass
        else:
            raise AssertionError("Expected KeyError")
