"""Generalized task schema for benchmark data loaders.

``TaskDefinition`` is the generic, environment-agnostic task model used
by every benchmark data loader. Per-environment requirements (such as a
browser ``start_url`` or login flow) live inside the discriminated
``setup`` union; truly benchmark-specific fields go into ``metadata``.

The discriminator on ``TaskSetup`` is the ``setup_type`` field. For now,
only ``BrowserSetup`` exists; additional setup variants
(``DockerSetup`` / ``ToolSetup``) are added in later milestones (P2/P3)
when those capabilities land.

See docs/architecture_design.md §4.10 and docs/execution_dag.md M15.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ── Setup variants ────────────────────────────────────────────────────


class BrowserSetup(BaseModel):
    """Browser session requirements for a task.

    Browser-specific fields (start_url, login flow) live here so the
    top-level ``TaskDefinition`` stays environment-agnostic.
    """

    setup_type: Literal["browser"] = "browser"

    start_url: str | None = None
    login_required: bool = False
    login_type: str | None = None  # "account_password", "sso", "oauth"
    credentials_ref: str | None = None  # key into credential store, never plaintext
    risk_control: bool = False
    risk_control_types: list[str] = Field(default_factory=list)


# Discriminated union of all setup variants. Other variants
# (DockerSetup, ToolSetup, ...) are added by future milestones.
TaskSetup = Annotated[
    Union[BrowserSetup],
    Field(discriminator="setup_type"),
]


# ── Reference / scoring ───────────────────────────────────────────────


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

    Supports rubric-based (steps + scoring), simple answer, and encrypted
    modes (e.g. BrowseComp).
    """

    steps: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    scoring: ScoringRubric | None = None
    # Encrypted benchmark support (BrowseComp)
    encrypted_question: str | None = None
    encrypted_answer: str | None = None


# ── Top-level task definition ─────────────────────────────────────────


class TaskDefinition(BaseModel):
    """Generic benchmark task definition.

    A ``TaskDefinition`` describes a single benchmark task and is
    independent of the environment type. Browser-specific data lives
    inside ``setup`` (a ``BrowserSetup``) or in the open-ended
    ``metadata`` dict.

    Loaders are responsible for normalizing raw benchmark data into this
    shape via their ``_normalize()`` method.
    """

    model_config = ConfigDict(extra="allow")

    # ── Identity ──
    task_id: str
    benchmark: str
    env_type: str = "browser"  # "browser" | "docker_terminal" | "composite" | ...

    # ── Instruction ──
    task_text: str = ""
    language: str = "en"

    # ── Per-env setup (discriminated union) ──
    setup: TaskSetup = Field(default_factory=BrowserSetup)

    # ── Evaluation ──
    expected_answer: str | None = None  # simple string answer (exact-match scenarios)
    reference: TaskReference | None = None  # rich reference (rubric / encrypted)

    # ── Classification ──
    difficulty: str | None = None  # "easy", "medium", "hard"
    domain: str | None = None  # "ecommerce", "social_lifestyle", ...
    tags: list[str] = Field(default_factory=list)  # free-form: ["L2", "multi_step", "T1"]

    # ── Catch-all for truly benchmark-specific fields ──
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "BrowserSetup",
    "TaskSetup",
    "ScoringItem",
    "ScoringRubric",
    "TaskReference",
    "TaskDefinition",
]
