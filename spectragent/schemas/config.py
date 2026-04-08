"""Configuration data contracts."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
