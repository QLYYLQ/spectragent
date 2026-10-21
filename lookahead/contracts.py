"""Reading the recording features that arrive with other parts of the work.

Three recording features are built beside this pipeline and read here only in
the shapes agreed for them:

- per-action start and end times, ``StepRecord.action_spans`` (``None``: not
  measured; ``[]``: the step called the environment zero times);
- the ``browser.frames`` archiver: clean viewport frames taken right before
  each action and once more a delay after each observation, read with the
  browser package's pure frames reader;
- task-variant branches, whose provenance carries ``task_variant``.

Every access goes through this module, and every spectragent symbol of those
features is imported inside the function that needs it, so the rest of the
pipeline never depends on whether a feature is installed. A trace that holds
no ``browser.frames`` file never imports the frames reader at all.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.trace.models import TraceConfig
from spectragent.schemas.step_record import StepRecord

#: The frames archiver's key.
FRAMES_ARCHIVER_ID = "browser.frames"


def plain(value: Any) -> Any:
    """A JSON-ready copy of a pydantic model, a dataclass, a mapping or a sequence."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    return value


def action_spans(record: StepRecord) -> list[dict[str, Any]] | None:
    """The step's per-action spans as recorded, or ``None`` when they were not measured."""
    spans = getattr(record, "action_spans", None)
    if spans is None:
        return None
    return [plain(span) for span in spans]


def task_variant(config: TraceConfig) -> dict[str, Any] | None:
    """A task-variant branch's variant provenance, or ``None`` for every other run."""
    branch = config.branch
    if branch is None:
        return None
    value = getattr(branch, "task_variant", None)
    return plain(value) if value is not None else None


def records_frames(reader: ArchiveReader) -> bool:
    """Whether the run committed any ``browser.frames`` file."""
    entries = reader.iter_archive(FRAMES_ARCHIVER_ID, include_trailing=True)
    return next(iter(entries), None) is not None


def run_frames(reader: ArchiveReader) -> list[tuple[int | None, Any]]:
    """Every frame record of the run with the step whose commit took it (``None``: trailing)."""
    if not records_frames(reader):
        return []
    # The frames reader arrives with the frames archiver; the ignore is moot once it is merged.
    from spectragent.core.capabilities.browser.archive.frames import (  # pyright: ignore[reportMissingImports]
        iter_frames,
    )

    return list(iter_frames(reader, include_trailing=True))


def frame_image(reader: ArchiveReader, step: int | None, record: Any) -> bytes:
    """The PNG bytes of one captured frame."""
    from spectragent.core.capabilities.browser.archive.frames import (  # pyright: ignore[reportMissingImports]
        read_frame_image,
    )

    return read_frame_image(reader, step, record)
