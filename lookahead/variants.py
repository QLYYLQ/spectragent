"""``variants``: alternative tasks that start from a recorded screen, for task-variant branches.

For each recorded run (not a replay, not a branch) a few checkpoints are
chosen: policy steps the branch loader accepts as checkpoints (a committed
prefix, the trace complete through them, an observation recorded) that have a
screenshot, spread over the run. For each, the judge model sees the
screenshot and the source task and writes alternative tasks: a different
information need on the same site, answerable without logging in, concrete
and checkable, in the source task's language. Replies are validated (count,
distinctness, writing system) and written as a ``task-variants-1`` file the
``trace_branches`` loader reads (its ``task_variants`` option).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lookahead.judge import Judge, JudgeRequest, image_part, text_part
from lookahead.runs import Run, RunSet
from spectragent.core.data_loader.task.task_variants import (
    TASK_VARIANTS_FORMAT,
    TaskVariantEntry,
    TaskVariantsFile,
)

PURPOSE = "task_variants"
SYSTEM = (
    "You design browsing tasks for evaluating web agents. You only propose tasks whose answer "
    "can be read on the website shown, without logging in, and you reply with JSON only."
)


class VariantTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_text: str = Field(min_length=1)
    answer_location: str


class VariantReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variants: list[VariantTask]


def writing_system(text: str) -> str:
    """``japanese``, ``korean``, ``chinese`` (Han characters) or ``other``."""
    if re.search(r"[぀-ヿ]", text):
        return "japanese"
    if re.search(r"[가-힯]", text):
        return "korean"
    if re.search(r"[一-鿿]", text):
        return "chinese"
    return "other"


LANGUAGE_NAMES = {"japanese": "Japanese", "korean": "Korean", "chinese": "Chinese"}


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def reply_model_for(count: int, source_task: str) -> type[VariantReply]:
    """The reply model for one checkpoint: exactly *count* distinct tasks in the source's script."""
    script = writing_system(source_task)
    source = _normalized(source_task)

    class CheckedVariantReply(VariantReply):
        @model_validator(mode="after")
        def _checked(self) -> CheckedVariantReply:
            if len(self.variants) != count:
                raise ValueError(f"expected exactly {count} variants, got {len(self.variants)}")
            seen: set[str] = set()
            for variant in self.variants:
                text = _normalized(variant.task_text)
                if not text:
                    raise ValueError("a variant's task_text is blank")
                if text == source:
                    raise ValueError("a variant repeats the original task")
                if text in seen:
                    raise ValueError("two variants are the same task")
                seen.add(text)
                if writing_system(variant.task_text) != script:
                    raise ValueError(
                        f"variant {variant.task_text!r} is not written in the original task's "
                        f"language ({LANGUAGE_NAMES.get(script, 'its script')})"
                    )
            return self

    return CheckedVariantReply


def candidate_steps(run: Run) -> list[int]:
    """Policy steps the branch loader would accept as checkpoints, that have a screenshot.

    The loader's rule: a closed trace, a contiguous committed prefix, a step
    the trace is complete through, decided by the agent, whose observation
    was recorded (it did not fail while observing).
    """
    if not run.closed:
        return []
    steps: list[int] = []
    step = 0
    while step in run.records and step <= run.complete_through:
        record = run.records[step]
        if (
            record.origin == "policy"
            and record.error_phase != "observe"
            and run.has_screenshot(step)
        ):
            steps.append(step)
        step += 1
    return steps


def spread(candidates: list[int], count: int) -> list[int]:
    """*count* of *candidates*, one from the middle of each of *count* equal stretches."""
    if count <= 0 or not candidates:
        return []
    if count >= len(candidates):
        return list(candidates)
    picked = [candidates[int((index + 0.5) * len(candidates) / count)] for index in range(count)]
    return sorted(set(picked))


def prompt(task: str, step: int, url: str | None, count: int) -> str:
    script = writing_system(task)
    language = LANGUAGE_NAMES.get(script, "the same language as the original task")
    where = f" (URL: {url})" if url else ""
    return (
        f"A web agent was working on the task below in a browser. The screenshot shows the "
        f"screen it had reached at step {step}{where}.\n\n"
        f"Original task: {task}\n\n"
        f"Write exactly {count} new tasks that a person could start from exactly this screen. "
        "Each new task must:\n"
        "1. need a different piece of information than the original task, found on this same "
        "website;\n"
        "2. be answerable without logging in, paying, posting or changing anything on the site;\n"
        "3. be concrete and checkable: it asks for a specific fact (a number, a name, a date, a "
        "title or a short list) that two people would agree on;\n"
        f"4. be written in {language}, like the original task;\n"
        "5. be self-contained: it says what to look for, never 'the item above';\n"
        "6. continue from this screen as it is: never ask to redo the steps that led here "
        "(searching again, returning to the home page).\n"
        "The tasks must differ from each other. For each task also say, in one sentence, where on "
        "this website its answer can be found.\n\n"
        'Reply with only a JSON object of this shape: {"variants": [{"task_text": "...", '
        '"answer_location": "..."}]}'
    )


def requests(
    runs: RunSet, steps_per_run: int, per_step: int
) -> tuple[list[tuple[str, int, JudgeRequest[Any]]], dict[str, str]]:
    """``(source run id, step, request)`` per chosen checkpoint, and why each run gave none."""
    found: list[tuple[str, int, JudgeRequest[Any]]] = []
    skipped: dict[str, str] = {}
    for run in runs.runs:
        if run.kind != "record":
            skipped[run.key] = f"a {run.kind} run: variants start from recorded runs"
            continue
        candidates = candidate_steps(run)
        if not candidates:
            skipped[run.key] = "no policy step with a screenshot that a branch can start from"
            continue
        for step in spread(candidates, steps_per_run):
            image = run.screenshot_bytes(step)
            if image is None:
                continue
            shot = run.screenshot(step)
            url = shot.source_url if shot is not None else None
            request: JudgeRequest[Any] = JudgeRequest(
                purpose=PURPOSE,
                item=f"{run.key}@{step}",
                system=SYSTEM,
                content=[
                    text_part(prompt(run.config.task, step, url, per_step)),
                    image_part(image),
                ],
                reply_model=reply_model_for(per_step, run.config.task),
                image_sources=[f"{run.key}@{step}"],
            )
            found.append((run.run_id, step, request))
    return found, skipped


def variants_document(
    checkpoints: list[tuple[str, int, JudgeRequest[Any]]], replies: list[Any], model: str
) -> TaskVariantsFile:
    """The loader's own ``task-variants-1`` model, so the file is what the loader reads."""
    variants: list[TaskVariantEntry] = []
    for (run_id, step, _), outcome in zip(checkpoints, replies, strict=True):
        if outcome.value is None:
            continue
        for number, variant in enumerate(outcome.value.variants, start=1):
            variants.append(
                TaskVariantEntry(
                    source_run_id=run_id,
                    step=step,
                    variant_id=str(number),
                    task_text=variant.task_text.strip(),
                    note=(
                        f"written by {model} from the screenshot of step {step}; "
                        f"answer location: {variant.answer_location.strip()}"
                    ),
                )
            )
    return TaskVariantsFile(format=TASK_VARIANTS_FORMAT, variants=variants)


def calls_path(out: Path) -> Path:
    """Where the judge's call records go: beside *out*, ``<stem>.calls.jsonl``."""
    return out.with_name(f"{out.stem}.calls.jsonl")


async def generate(
    runs: RunSet, judge: Judge, out: Path, *, steps_per_run: int, per_step: int
) -> dict[str, Any]:
    """Ask for the variants, write the ``task-variants-1`` file, and say what was done."""
    if steps_per_run < 1 or per_step < 1:
        raise ValueError("--steps-per-run and --per-step must be at least 1")
    checkpoints, skipped = requests(runs, steps_per_run, per_step)
    replies = await judge.ask_all([request for _, _, request in checkpoints])
    document = variants_document(checkpoints, replies, judge.model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "checkpoints": len(checkpoints),
        "checkpoints_without_variants": sum(1 for reply in replies if reply.value is None),
        "variants": len(document.variants),
        "skipped_runs": skipped,
    }
