"""Per-loader normalization tests for the three browser benchmarks.

These tests use the real loader classes but bypass disk loading by
monkey-patching ``_load`` on each instance, then directly seed test
records via ``_normalize_raw``. This exercises the inherited
normalization logic without subclassing (subclassing would create
duplicate conscribe registry entries).
"""

from __future__ import annotations

from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.browsecomp import BrowseCompTaskLoader
from spectragent.core.data_loader.task.lexbench_browser import (
    LexBenchBrowserTaskLoader,
)
from spectragent.core.data_loader.task.online_mind2web import (
    OnlineMind2WebTaskLoader,
)
from spectragent.schemas.task import BrowserSetup, TaskContext, TaskDefinition


def _build_stub(cls: type[TaskLoaderBase], raws: list[dict]) -> TaskLoaderBase:
    """Instantiate a loader without disk I/O and seed it with raw records.

    Bypasses ``__post_init__``-driven loading by constructing the
    instance via ``__new__``, then manually initializing the dataclass
    fields and populating ``_tasks`` from ``_normalize_raw``.
    """
    loader = cls.__new__(cls)
    # Initialize the dataclass fields by hand (we skip __init__ so
    # __post_init__ doesn't trigger _load).
    loader.subset = "All"
    loader.data_dir = None
    loader._tasks = {}
    for raw in raws:
        task = loader._normalize_raw(raw)
        loader._tasks[task.task_id] = task
    return loader


# ── Online-Mind2Web ────────────────────────────────────────────────────


class TestOnlineMind2WebNormalization:
    def test_basic_record(self) -> None:
        loader = _build_stub(
            OnlineMind2WebTaskLoader,
            [
                {
                    "task_id": "m2w_001",
                    "task_text": "Find a flight from NYC to SFO",
                    "start_url": "https://flights.google.com",
                    "domain": "travel",
                    "difficulty": "medium",
                }
            ],
        )
        task = loader["m2w_001"]
        assert isinstance(task, TaskDefinition)
        assert task.task_id == "m2w_001"
        assert task.benchmark == "online_mind2web"
        assert task.env_type == "browser"
        assert task.task_text == "Find a flight from NYC to SFO"
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://flights.google.com"
        assert task.domain == "travel"
        assert task.difficulty == "medium"

    def test_get_task_context(self) -> None:
        loader = _build_stub(
            OnlineMind2WebTaskLoader,
            [
                {
                    "task_id": "m2w_001",
                    "start_url": "https://example.com",
                }
            ],
        )
        context = loader.get_task_context("m2w_001")
        assert context.env == {"start_url": "https://example.com"}
        # A dataset task feeds the environment only.
        assert context.loop == {}
        assert context.branch is None


# ── LexBench-Browser ───────────────────────────────────────────────────


class TestLexBenchBrowserNormalization:
    def test_basic_record_with_tags(self) -> None:
        loader = _build_stub(
            LexBenchBrowserTaskLoader,
            [
                {
                    "task_id": "lex_l2_001",
                    "task_text": "Search for property listings",
                    "start_url": "https://realestate.example.com",
                    "tags": ["L2", "search", "T1"],
                    "domain": "real_estate",
                }
            ],
        )
        task = loader["lex_l2_001"]
        assert task.benchmark == "lexbench_browser"
        assert task.tags == ["L2", "search", "T1"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://realestate.example.com"

    def test_record_with_login_setup(self) -> None:
        loader = _build_stub(
            LexBenchBrowserTaskLoader,
            [
                {
                    "task_id": "lex_login_001",
                    "task_text": "Login required task",
                    "start_url": "https://secure.example.com",
                    "setup": {
                        "login_required": True,
                        "login_type": "account_password",
                        "credentials_ref": "lexbench_account_a",
                    },
                }
            ],
        )
        task = loader["lex_login_001"]
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://secure.example.com"
        assert task.setup.login_required is True
        assert task.setup.login_type == "account_password"
        assert task.setup.credentials_ref == "lexbench_account_a"


# ── BrowseComp ─────────────────────────────────────────────────────────


class TestBrowseCompNormalization:
    def test_encrypted_record(self) -> None:
        loader = _build_stub(
            BrowseCompTaskLoader,
            [
                {
                    "task_id": "bc_001",
                    "task_text": "(encrypted)",
                    "reference": {
                        "encrypted_question": "ENC_Q_001",
                        "encrypted_answer": "ENC_A_001",
                    },
                }
            ],
        )
        task = loader["bc_001"]
        assert task.benchmark == "browsecomp"
        assert task.reference is not None
        assert task.reference.encrypted_question == "ENC_Q_001"
        assert task.reference.encrypted_answer == "ENC_A_001"

    def test_no_start_url_means_empty_context(self) -> None:
        loader = _build_stub(
            BrowseCompTaskLoader,
            [
                {
                    "task_id": "bc_001",
                    "task_text": "(encrypted)",
                }
            ],
        )
        # BrowseComp has no start_url so context is empty
        assert loader.get_task_context("bc_001") == TaskContext()
