"""Public re-exports for spectragent.schemas."""

from spectragent.schemas._types import (
    AgentDoneStatus,
    AgentStatus,
    EnvironmentStatus,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import (
    AgentMetrics,
    AgentResult,
    AgentUsage,
)
from spectragent.schemas.config import BenchConfig, RunConfig
from spectragent.schemas.task import (
    BrowserSetup,
    ScoringItem,
    ScoringRubric,
    TaskDefinition,
    TaskReference,
    TaskSetup,
)
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import (
    AgentResultRef,
    EvalDetails,
    EvalResult,
    EvalUsage,
    FailureClassification,
)
from spectragent.schemas.eval_summary import (
    CostSummary,
    EvalSummary,
    FailureCategoryStats,
    MetricStats,
    OverallStatistics,
)
from spectragent.schemas.login import LoginSnapshot
from spectragent.schemas.prompt import PromptRef, PromptSnapshot, TemplatePrompt, TextPrompt
from spectragent.schemas.step_record import StepRecord, TokenUsage

__all__ = [
    # Types
    "EnvironmentStatus",
    "AgentDoneStatus",
    "AgentStatus",  # Deprecated alias for EnvironmentStatus
    # Action
    "Action",
    "ActionDescriptor",
    "ActionResult",
    # Step record
    "StepRecord",
    "TokenUsage",
    # Login
    "LoginSnapshot",
    # Task
    "TaskDefinition",
    "TaskSetup",
    "BrowserSetup",
    "TaskReference",
    "ScoringRubric",
    "ScoringItem",
    # Config
    "RunConfig",
    "BenchConfig",
    # Prompt
    "PromptRef",
    "PromptSnapshot",
    "TemplatePrompt",
    "TextPrompt",
    # Agent result
    "AgentUsage",
    "AgentMetrics",
    "AgentResult",
    # Eval reference
    "EvalReference",
    # Eval result
    "EvalUsage",
    "EvalDetails",
    "FailureClassification",
    "AgentResultRef",
    "EvalResult",
    # Eval summary
    "MetricStats",
    "OverallStatistics",
    "FailureCategoryStats",
    "CostSummary",
    "EvalSummary",
]
