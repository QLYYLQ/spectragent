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
from spectragent.schemas.browser_task import (
    BrowserSetup,
    BrowserTask,
    ScoringItem,
    ScoringRubric,
    TaskReference,
)
from spectragent.schemas.config import BenchConfig, RunConfig, TaskDefinition
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
    # Browser task
    "BrowserTask",
    "BrowserSetup",
    "TaskReference",
    "ScoringRubric",
    "ScoringItem",
    # Config
    "TaskDefinition",
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
