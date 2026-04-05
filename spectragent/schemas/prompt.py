"""Prompt reference and snapshot schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from spectragent.schemas._types import UTCDatetime


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
