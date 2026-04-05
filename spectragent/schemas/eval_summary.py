"""Evaluation summary schema."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetricStats(BaseModel):
    count: int = 0
    mean: float = 0.0
    min: float = 0.0
    max: float = 0.0
    median: float = 0.0


class OverallStatistics(BaseModel):
    total_tasks: int
    evaluated_tasks: int
    successful_tasks: int
    failed_tasks: int
    success_rate: float
    failure_rate: float


class FailureCategoryStats(BaseModel):
    total_failed_tasks: int
    by_category: dict[str, dict[str, float]]


class CostSummary(BaseModel):
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    total_non_cached_prompt: int = 0
    costs: dict[str, float] = Field(default_factory=dict)


class EvalSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    overall_statistics: OverallStatistics
    metrics_statistics: dict[str, MetricStats]
    successful_tasks_metrics: dict[str, MetricStats]
    failed_tasks_metrics: dict[str, MetricStats]
    failure_category_statistics: FailureCategoryStats
    task_list: dict[str, list[str]]
    evaluation_config: dict[str, Any] = Field(default_factory=dict)
    evaluation_cost: CostSummary | None = None
    benchmark_summary: dict[str, Any] = Field(default_factory=dict)
