"""Browser benchmark task schema — typed replacement for TaskDefinition in browser env."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserSetup(BaseModel):
    """Browser session requirements for a task.

    Consumed by agent runner to decide how to establish the browser session.
    """

    login_required: bool = False
    login_type: str | None = None  # "account_password", "sso", "oauth"
    credentials_ref: str | None = None  # key into credential store, never plaintext
    risk_control: bool = False
    risk_control_types: list[str] = Field(default_factory=list)


class ScoringItem(BaseModel):
    """Individual scoring criterion in a rubric."""

    name: str
    score: int
    description: str = ""


class ScoringRubric(BaseModel):
    """Structured scoring criteria for rubric-based evaluation."""

    total: int = 100
    threshold: int = 60
    items: list[ScoringItem] = Field(default_factory=list)


class TaskReference(BaseModel):
    """Rich reference answer for evaluation.

    Supports rubric-based (steps + scoring), simple answer, and encrypted modes.
    """

    steps: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    scoring: ScoringRubric | None = None
    # Encrypted benchmark support (BrowseComp)
    encrypted_question: str | None = None
    encrypted_answer: str | None = None


class BrowserTask(BaseModel):
    """Normalized browser benchmark task.

    All benchmark data files should conform to this schema after migration.
    Loader ``_normalize()`` handles any residual field mapping.
    """

    model_config = ConfigDict(extra="allow")

    # ── Identity ──
    task_id: str
    benchmark: str  # "lexbench_browser", "online_mind2web", "browsecomp"
    env_type: Literal["browser"] = "browser"

    # ── Instruction ──
    task_text: str = ""
    language: str = "en"

    # ── Browser Setup ──
    start_url: str | None = None
    setup: BrowserSetup = Field(default_factory=BrowserSetup)

    # ── Evaluation ──
    expected_answer: str | None = None  # simple string answer (exact-match scenarios)
    reference: TaskReference | None = None  # rich reference (rubric / encrypted)

    # ── Classification ──
    difficulty: str | None = None  # "easy", "medium", "hard"
    domain: str | None = None  # "ecommerce", "social_lifestyle", ...
    tags: list[str] = Field(default_factory=list)  # free-form: ["L2", "multi_step", "T1"]

    # ── Catch-all for truly benchmark-specific fields ──
    metadata: dict[str, Any] = Field(default_factory=dict)
