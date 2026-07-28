"""Pydantic output_format models used to probe structured-output support.

Two shapes:
  * PersonInfo   — flat, the simplest possible structured case.
  * ProjectPlan  — nested model + list-of-models + enum. Generates $defs, which
                   stresses SchemaOptimizer flattening and surfaces the
                   ``additionalProperties: false`` + all-required strict-schema
                   rejection that some proxies enforce.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PersonInfo(BaseModel):
    """A person with basic info."""

    name: str = Field(description="The person's full name")
    age: int = Field(description="The person's age in years")
    hobbies: list[str] = Field(description="A few of the person's hobbies")


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class TaskItem(BaseModel):
    """A single task in a project plan."""

    title: str = Field(description="Short task title")
    priority: Priority = Field(description="Task priority")


class ProjectPlan(BaseModel):
    """A small software project plan (nested + list + enum stress case)."""

    name: str = Field(description="Project name")
    owner: PersonInfo = Field(description="The project owner")
    tasks: list[TaskItem] = Field(description="The tasks in the plan")
    tags: list[str] = Field(description="Free-form tags")


# Prompts paired with the schemas above.
PERSON_PROMPT = (
    "Create a fictional person named Alice who is 28 years old and likes "
    "reading, hiking, and coding."
)
PROJECT_PROMPT = (
    "Create a fictional software project plan. Give it a name, an owner "
    "(a fictional person with a name, age, and a couple of hobbies), exactly "
    "three tasks each with a title and a priority (low, medium, or high), and "
    "two or three tags."
)
