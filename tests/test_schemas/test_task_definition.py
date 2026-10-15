"""Tests for the generic TaskDefinition + TaskSetup discriminated union."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from spectragent.schemas.task import (
    BrowserSetup,
    ScoringItem,
    ScoringRubric,
    TaskDefinition,
    TaskReference,
)


class TestBrowserSetup:
    def test_default_construction(self) -> None:
        setup = BrowserSetup()
        assert setup.setup_type == "browser"
        assert setup.start_url is None
        assert setup.login_required is False
        assert setup.risk_control_types == []

    def test_with_start_url(self) -> None:
        setup = BrowserSetup(start_url="https://example.com")
        assert setup.start_url == "https://example.com"
        assert setup.setup_type == "browser"

    def test_login_fields(self) -> None:
        setup = BrowserSetup(
            login_required=True,
            login_type="account_password",
        )
        assert setup.login_required is True
        assert setup.login_type == "account_password"


class TestTaskDefinition:
    def test_minimal_construction(self) -> None:
        task = TaskDefinition(task_id="t1", benchmark="bench_a")
        assert task.task_id == "t1"
        assert task.benchmark == "bench_a"
        assert task.env_type == "browser"  # default
        assert task.task_text == ""
        assert task.language == "en"
        assert isinstance(task.setup, BrowserSetup)
        assert task.tags == []
        assert task.metadata == {}

    def test_full_construction(self) -> None:
        task = TaskDefinition(
            task_id="t1",
            benchmark="lexbench_browser",
            env_type="browser",
            task_text="Find a cheap flight",
            language="en",
            setup=BrowserSetup(start_url="https://flights.example.com"),
            expected_answer="$199",
            difficulty="medium",
            domain="travel",
            tags=["L2", "search"],
            metadata={"raw_id": "abc"},
        )
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://flights.example.com"
        assert task.expected_answer == "$199"
        assert task.tags == ["L2", "search"]
        assert task.metadata == {"raw_id": "abc"}

    def test_extra_fields_allowed(self) -> None:
        # Pydantic extra='allow' means unknown raw fields are preserved
        task = TaskDefinition.model_validate(
            {
                "task_id": "t1",
                "benchmark": "bench",
                "setup": {"setup_type": "browser"},
                "weird_extra": 42,
            }
        )
        # extra="allow" surfaces unknown fields as model attributes
        assert task.weird_extra == 42  # type: ignore[attr-defined]

    def test_setup_discriminator_browser(self) -> None:
        task = TaskDefinition.model_validate(
            {
                "task_id": "t1",
                "benchmark": "bench",
                "setup": {
                    "setup_type": "browser",
                    "start_url": "https://x.example.com",
                },
            }
        )
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.start_url == "https://x.example.com"

    def test_setup_default_is_browser(self) -> None:
        task = TaskDefinition(task_id="t1", benchmark="bench")
        assert isinstance(task.setup, BrowserSetup)
        assert task.setup.setup_type == "browser"

    def test_invalid_setup_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskDefinition.model_validate(
                {
                    "task_id": "t1",
                    "benchmark": "bench",
                    "setup": {"setup_type": "unknown_env"},
                }
            )

    def test_reference_with_scoring_rubric(self) -> None:
        rubric = ScoringRubric(
            total=100,
            threshold=60,
            items=[ScoringItem(name="step1", score=50, description="Open page")],
        )
        ref = TaskReference(
            steps=["a", "b"],
            scoring=rubric,
        )
        task = TaskDefinition(
            task_id="t1",
            benchmark="bench",
            reference=ref,
        )
        assert task.reference is not None
        assert task.reference.scoring is not None
        assert task.reference.scoring.threshold == 60
        assert task.reference.scoring.items[0].name == "step1"

    def test_encrypted_reference(self) -> None:
        ref = TaskReference(
            encrypted_question="ENC_Q",
            encrypted_answer="ENC_A",
        )
        task = TaskDefinition(task_id="t1", benchmark="browsecomp", reference=ref)
        assert task.reference is not None
        assert task.reference.encrypted_answer == "ENC_A"


class TestSchemasInitExports:
    def test_browser_task_module_removed(self) -> None:
        # Hard-cutover assertion: the legacy schema file no longer exists.
        legacy = Path(__file__).resolve().parents[2] / ("spectragent/schemas/browser_task.py")
        assert not legacy.exists(), (
            f"Legacy browser_task.py should be deleted but exists at {legacy}"
        )

    def test_task_definition_exported_from_schemas(self) -> None:
        from spectragent import schemas

        assert hasattr(schemas, "TaskDefinition")
        assert hasattr(schemas, "TaskSetup")
        assert hasattr(schemas, "BrowserSetup")
        assert hasattr(schemas, "TaskReference")
        # Legacy symbol is gone
        assert not hasattr(schemas, "BrowserTask")
