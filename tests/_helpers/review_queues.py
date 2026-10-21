"""A recorded run with screenshots and archived frames, plus review queues over it.

The run goes through the real ``WALWriter`` and archive encoding (as
``trace_builder`` does); the queues are the ``review-queue-1`` files the
tools that make them write.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from spectragent.annotation import ReviewQueue, write_queue
from spectragent.core.trace.models import TraceStatus
from tests._helpers.trace_builder import (
    agent_result,
    archive_file,
    new_wal,
    screenshot,
    step_record,
)

FRAMES = "browser.frames"
SCREEN_SIZE = (64, 40)


def png(
    size: tuple[int, int] = SCREEN_SIZE, color: tuple[int, int, int] = (250, 250, 250)
) -> bytes:
    """A real PNG of *size*, so image headers carry a readable size."""
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


@dataclass
class ReviewRun:
    run_dir: Path
    key: str
    #: The frame names archived at each step (``None``: the trailing commit).
    frames: dict[int | None, list[str]]


def build_review_run(
    base: Path,
    run_id: str = "run-review",
    *,
    steps: int = 3,
    screens: list[bytes] | None = None,
    frames: dict[int | None, list[tuple[str, bytes]]] | None = None,
) -> ReviewRun:
    """*steps* committed steps with a screenshot each and ``browser.frames`` images.

    Every step also archives a ``frames.json`` index, which is not an image.
    """
    screens = screens or [png(color=(250 - 20 * i, 250, 250)) for i in range(steps)]
    frames = frames or {
        step: [(f"frame-{step}-before.png", png(color=(200, 220, 240 - step)))]
        for step in range(steps)
    }
    wal = new_wal(base, run_id)
    for step in range(steps):
        evidence = [
            archive_file(FRAMES, name, data, step=step, media_type="image/png")
            for name, data in frames.get(step, [])
        ]
        evidence.append(
            archive_file(FRAMES, "frames.json", b'{"format": "browser-frames-1"}', step=step)
        )
        wal.commit_step(
            step,
            step_record(step),
            fragments=[screenshot(step, screens[step])],
            extra_files=evidence,
        )
    trailing = [
        archive_file(FRAMES, name, data, trailing=0, media_type="image/png")
        for name, data in frames.get(None, [])
    ]
    if trailing:
        wal.commit_trailing(extra_files=trailing)
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return ReviewRun(
        wal.run_dir,
        run_id,
        {step: [name for name, _ in files] for step, files in frames.items()},
    )


def fork_judge_queue(run: str, **overrides: Any) -> ReviewQueue:
    """Accept or correct a model's verdict on a branched action."""
    data: dict[str, Any] = {
        "format": "review-queue-1",
        "queue_id": "fork-judge",
        "title": "Branched actions",
        "instructions": "Is the branched action a reasonable next step for the task?",
        "label": {
            "choices": [
                {"value": "reasonable", "text": "Reasonable"},
                {"value": "unreasonable", "text": "Unreasonable"},
            ],
            "multi": False,
            "regions": None,
            "note": True,
        },
        "items": [
            {
                "item_id": f"{run}/s{step}",
                "title": f"Step {step}",
                "run": run,
                "step": step,
                "images": [
                    {
                        "role": "before",
                        "text": "Current screen",
                        "source": {"kind": "observation", "run": run, "step": step},
                    }
                ],
                "fields": [{"text": "Action", "value": "browser.click"}],
                "links": [{"text": "Source run", "run": run, "step": step}],
                "suggestion": {
                    "choice": "reasonable",
                    "source": "judge-model",
                    "rationale": "It opens the search result the task names.",
                },
            }
            for step in range(2)
        ],
    }
    data.update(overrides)
    return ReviewQueue.model_validate(data)


def evidence_queue(run: str, frame: str, **overrides: Any) -> ReviewQueue:
    """Draw where the next decision's information appears."""
    data: dict[str, Any] = {
        "format": "review-queue-1",
        "queue_id": "gold-evidence",
        "title": "Where the needed information appears",
        "instructions": "Box what the next decision needs on the next screen.",
        "label": {
            "choices": [
                {"value": "read", "text": "Read a value"},
                {"value": "locate", "text": "Find a control"},
            ],
            "multi": True,
            "regions": {
                "kinds": [
                    {"value": "evidence", "text": "Evidence"},
                    {"value": "target", "text": "Target"},
                ]
            },
            "note": True,
        },
        "items": [
            {
                "item_id": "first",
                "title": "Step 0 to 1",
                "run": run,
                "step": 0,
                "images": [
                    {
                        "role": "before",
                        "text": "Current screen",
                        "source": {"kind": "observation", "run": run, "step": 0},
                    },
                    {
                        "role": "frame",
                        "text": "Before the action",
                        "source": {
                            "kind": "archive",
                            "run": run,
                            "step": 0,
                            "archiver": FRAMES,
                            "name": frame,
                        },
                    },
                ],
                "suggestion": {
                    "choices": ["read"],
                    "regions": [{"image": "before", "kind": "evidence", "box": [1, 2, 30, 20]}],
                    "source": "proposal-model",
                    "rationale": "The price is in the first row.",
                },
            },
            {"item_id": "second", "title": "No suggestion", "run": run, "step": 1, "images": []},
        ],
    }
    data.update(overrides)
    return ReviewQueue.model_validate(data)


def save(directory: Path, *queues: ReviewQueue) -> None:
    for queue in queues:
        write_queue(directory, queue)
