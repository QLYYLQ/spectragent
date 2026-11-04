"""Which data a benchmark's tasks came from (``docs/architecture_design.md`` §4.10.1 "数据").

A benchmark pins its data source and version: a Hugging Face dataset at a
commit, a git repository at a commit, or a file at a URL with its SHA-256.
``spectragent benchmark fetch`` checks what it downloaded against the pin and
writes the :class:`DataVersion` beside the data (``data_version.json``); the
task loader reads it back, and every run records it
(``TraceConfig.data_version``), so a trace says which version of the data its
task came from. Like the other records that cross into the trace, this module
imports nothing of ``spectragent.core``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Where the data comes from: a Hugging Face dataset, a git repository, or one file at a URL.
DataSource = Literal["huggingface", "git", "url"]


class DataVersion(BaseModel):
    """A benchmark's data source and its pinned version.

    Attributes:
        source: The kind of source.
        repository: The Hugging Face dataset (``osunlp/Online-Mind2Web``), the
            git repository's URL, or the file's URL.
        revision: The commit of a Hugging Face dataset or a git repository;
            the SHA-256 of a file at a URL, which has no version of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: DataSource
    repository: str = Field(min_length=1)
    revision: str = Field(min_length=1)

    def describe(self) -> str:
        """The version in one phrase: ``osunlp/Online-Mind2Web at eacad896``."""
        at = "sha256" if self.source == "url" else "at"
        return f"{self.repository} {at} {self.revision}"


__all__ = ["DataSource", "DataVersion"]
