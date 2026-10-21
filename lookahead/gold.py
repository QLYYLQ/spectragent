"""``gold-queue``: a review queue for human gold labels of where the needed information appears.

Each item shows the screen the agent saw (O_t), the actions it issued, the
next screen (O_{t+1}) and, when the frames archiver took one, the frame right
before the first action. The annotator picks the kind of information the
agent needs next and draws ``evidence`` boxes on the next screen. The
suggested boxes are the pixel difference between the two screens, as hints
only: no choice is suggested.

Items are policy steps whose two screens were both recorded, sampled in turn
across (site, action type) groups, preferring runs used least so far.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from lookahead import imaging
from lookahead.contracts import FRAMES_ARCHIVER_ID
from lookahead.extract import step_rows
from lookahead.forks import FILLER, action_type, describe_actions
from lookahead.queues import (
    Choice,
    ItemField,
    ItemLink,
    LabelSpec,
    QueueImage,
    QueueItem,
    RegionKind,
    RegionLabel,
    ReviewQueue,
    SuggestedRegion,
    Suggestion,
    archive_image,
    observation_image,
    write_queue,
)
from lookahead.rows import StepRow
from lookahead.runs import Run, RunSet

HINT_SOURCE = "pixel-diff"
NO_SITE = "(no site)"
THOUGHT_CHARS = 600
CHOICES = [
    Choice(value="result_list", text="A result list"),
    Choice(value="confirmation", text="A confirmation or feedback"),
    Choice(value="detail_fields", text="Detail fields"),
    Choice(value="navigation_target", text="A navigation target"),
    Choice(value="nothing_new", text="Nothing new is needed"),
    Choice(value="other", text="Other"),
]
INSTRUCTIONS = (
    "The agent saw the current screen and issued the actions listed. On the next screen, find "
    "the information the agent needs for its next decision toward the task. Choose what kind of "
    "information it is, and draw Evidence boxes on the next screen around where it appears. "
    "The suggested boxes only show where the screen changed; they are hints, not answers."
)


def main_action(row: StepRow) -> str:
    """The step's action type for sampling: its first action other than waiting."""
    for action in row.actions:
        kind = action_type(action.name)
        if kind not in FILLER:
            return kind
    return "wait" if row.actions else "none"


def candidates(runs: RunSet) -> list[tuple[Run, StepRow]]:
    found: list[tuple[Run, StepRow]] = []
    for run in runs.runs:
        for row in step_rows(run, runs):
            if row.origin != "policy" or not row.observation.screenshot:
                continue
            if not (row.next.exists and row.next.screenshot):
                continue
            found.append((run, row))
    return found


def sample(pool: list[tuple[Run, StepRow]], count: int, seed: int) -> list[tuple[Run, StepRow]]:
    """Up to *count* items: in turn from each (site, action type) group, the least-used run first."""
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[tuple[Run, StepRow]]] = {}
    for run, row in pool:
        groups.setdefault((row.site or NO_SITE, main_action(row)), []).append((run, row))
    for members in groups.values():
        rng.shuffle(members)
    used: Counter[str] = Counter()
    chosen: list[tuple[Run, StepRow]] = []
    order = sorted(groups)
    rng.shuffle(order)
    while len(chosen) < count and any(groups[key] for key in order):
        for key in order:
            members = groups[key]
            if not members or len(chosen) >= count:
                continue
            pick = min(range(len(members)), key=lambda index: used[members[index][0].key])
            run, row = members.pop(pick)
            used[run.key] += 1
            chosen.append((run, row))
    return chosen


def _thought(row: StepRow) -> str:
    text = row.llm_output.strip()
    return text if len(text) <= THOUGHT_CHARS else text[: THOUGHT_CHARS - 3] + "..."


def gold_item(run: Run, row: StepRow) -> QueueItem:
    assert row.next.step is not None
    images: list[QueueImage] = [
        observation_image("current", "What the agent saw", run.key, row.step),
        observation_image("next", "The next screen", run.key, row.next.step),
    ]
    before = row.frame("before_action")
    if before is not None and before.captured and before.batch_step is not None:
        assert before.name is not None
        images.append(
            archive_image(
                "before_action",
                "Right before the first action",
                run.key,
                before.batch_step,
                FRAMES_ARCHIVER_ID,
                before.name,
            )
        )
    hints: list[SuggestedRegion] = []
    current, following = run.screenshot_bytes(row.step), run.screenshot_bytes(row.next.step)
    note = "the two screens could not be compared"
    if current is not None and following is not None:
        diff = imaging.difference(current, following)
        if diff.compared:
            hints = [SuggestedRegion(image="next", kind="evidence", box=box) for box in diff.boxes]
            note = f"{len(diff.boxes)} changed region(s) between the two screens"
        elif diff.reason:
            note = diff.reason
    fields = [
        ItemField(text="Task", value=row.task),
        ItemField(text="Step", value=str(row.step)),
        ItemField(text="Actions", value=describe_actions(row, "")),
        ItemField(text="URL before", value=row.observation.url or "unknown"),
        ItemField(text="URL after", value=row.next.url or "unknown"),
        ItemField(text="Agent's reply", value=_thought(row)),
    ]
    return QueueItem(
        item_id=f"{run.key}@{row.step}",
        title=f"{row.site or NO_SITE}: step {row.step} ({main_action(row)})",
        run=run.key,
        step=row.step,
        images=images,
        fields=fields,
        links=[ItemLink(text="Run", run=run.key, step=row.step)],
        suggestion=Suggestion(choice=None, regions=hints, source=HINT_SOURCE, rationale=note),
    )


def gold_queue(
    runs: RunSet, out: Path, *, annotations: Path, queue_id: str, count: int, seed: int
) -> tuple[dict[str, Any], Path]:
    """Write the queue and a ``sampling.json`` summary in *out*; the summary and the queue file."""
    if count < 1:
        raise ValueError("--n must be at least 1")
    pool = candidates(runs)
    chosen = sample(pool, count, seed)
    queue = ReviewQueue(
        queue_id=queue_id,
        title="Where does the information for the next decision appear?",
        instructions=INSTRUCTIONS,
        label=LabelSpec(
            choices=CHOICES,
            multi=False,
            regions=RegionLabel(kinds=[RegionKind(value="evidence", text="Evidence")]),
            note=True,
        ),
        items=[gold_item(run, row) for run, row in chosen],
    )
    path = write_queue(annotations, queue)
    summary = {
        "queue": str(path),
        "candidates": len(pool),
        "items": len(chosen),
        "seed": seed,
        "by_site": dict(Counter(row.site or NO_SITE for _, row in chosen)),
        "by_action": dict(Counter(main_action(row) for _, row in chosen)),
        "by_run": dict(Counter(run.key for run, _ in chosen)),
        "skipped_runs": dict(runs.skipped),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "sampling.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary, path
