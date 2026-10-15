"""Prompt reference and snapshot schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas._types import UTCDatetime
from spectragent.schemas.action import Action, ActionResult


class PromptRef(BaseModel):
    """Prompt reference: path + SHA-256 content hash for versioning.

    ``content_hash`` is the primary version identifier (deterministic,
    independent of filesystem metadata).  ``timestamp`` is kept as an
    optional convenience reference but should not be relied upon for
    uniqueness (git clone/pull resets mtime).
    """

    path: str
    content_hash: str
    timestamp: UTCDatetime | None = None


class TemplatePrompt(BaseModel):
    """A template prompt with {placeholders} and substitution params."""

    kind: Literal["template"] = "template"
    ref: PromptRef
    template: str
    params: dict[str, str]


class TextPrompt(BaseModel):
    """A plain text prompt (no placeholders)."""

    kind: Literal["text"] = "text"
    ref: PromptRef
    text: str


PromptSnapshot = Annotated[
    TemplatePrompt | TextPrompt,
    Field(discriminator="kind"),
]


class PromptContribution(BaseModel):
    """An external instruction with its actual location and reproducible source."""

    model_config = ConfigDict(frozen=True)

    text: str
    placement: Literal["system", "turn"] = "turn"
    source: str = ""
    code_digest: str = ""


@dataclass(kw_only=True)
class ReactPromptContext:
    """Facts for one builder call; owned by the run, never by a hook counter.

    History callers provide recorded contributions. A policy interceptor may
    append contributions before calling the Agent. The Loop passes those facts
    to its step record without interpreting their text or placement.
    """

    step: int
    phase: Literal["history", "policy", "terminal"]
    policy_step: int = 0
    is_branch: bool = False
    contributions: list[PromptContribution] = field(default_factory=list)
    source_run_id: str | None = None
    source_reply: str | None = None
    generated: set[str] = field(default_factory=set, repr=False)


class RestoredHistory(BaseModel):
    """Actual restored execution, with old reply retained only as source material."""

    model_config = ConfigDict(frozen=True)

    source_run_id: str
    source_step: int = Field(ge=0)
    source_reply: str = ""
    source_actions: list[Action] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    results: list[ActionResult] = Field(default_factory=list)

    def validate_execution(self) -> None:
        """Refuse incomplete or mismatched execution before an Agent renders it."""
        if len(self.actions) != len(self.results):
            raise ValueError("restored history needs one actual result for each executed action")


@dataclass(frozen=True, kw_only=True)
class PromptRequest:
    """The stable Python contribution function's single keyword-only input."""

    observation: Any
    context: ReactPromptContext
    parameters: dict[str, Any]
    helper: Any = None
