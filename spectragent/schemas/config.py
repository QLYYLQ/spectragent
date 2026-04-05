"""Configuration data contracts."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskDefinition(BaseModel):
    """Single benchmark task definition. Allows extra fields for benchmark-specific metadata."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    task_text: str = ""
    url: str | None = None
    expected_answer: str | None = None
    benchmark: str = ""
    env_type: str = ""  # "browser" | "desktop" | "mobile"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    """Runtime configuration for a single benchmark run. Sub-fields are dict placeholders."""

    model_config = ConfigDict(extra="allow")

    llm: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    provider: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    timeout: float = 300.0


class BenchConfig(BaseModel):
    """Top-level benchmark configuration."""

    model_config = ConfigDict(extra="allow")

    project_name: str = "browseruse-bench"
    benchmark: str = ""
    subset: str | None = None
    run: RunConfig = Field(default_factory=RunConfig)
    output_dir: str = "experiments"
