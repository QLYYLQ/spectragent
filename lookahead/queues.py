"""Building review queues with the viewer's own models (``spectragent.annotation``).

A queue is written with :func:`spectragent.annotation.write_queue` to
``<annotations>/queues/<queue_id>.json``; the viewer started with
``--annotations <annotations>`` reads it. The models are the one definition
of the format, and the viewer's loader is strict, so every queue is built
only from them. Run keys are relative to the root the viewer is started
with; boxes are ``[x0, y0, x1, y1]`` in the referenced image's own pixels.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from spectragent.annotation import ArchiveImage, ObservationImage, QueueImage, Region
from spectragent.core.capabilities.browser.archive.frames import FRAMES_ARCHIVER_ID


def default_queue_id(prefix: str) -> str:
    """``<prefix>-<YYYYMMDD>`` for today."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}"


def observation_image(role: str, text: str, run: str, step: int) -> QueueImage:
    """A step's committed screenshot."""
    return QueueImage(
        role=role, text=text, source=ObservationImage(kind="observation", run=run, step=step)
    )


def frame_image(role: str, text: str, run: str, step: int | None, name: str) -> QueueImage:
    """A ``browser.frames`` image, by the step whose commit took it (``None``: trailing)."""
    return QueueImage(
        role=role,
        text=text,
        source=ArchiveImage(
            kind="archive", run=run, step=step, archiver=FRAMES_ARCHIVER_ID, name=name
        ),
    )


def region(image: str, kind: str, box: Sequence[float]) -> Region:
    """A box on the item image *image*: ``[x0, y0, x1, y1]`` in its own pixels."""
    return Region(image=image, kind=kind, box=[*box])
