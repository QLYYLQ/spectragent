"""Online-Mind2Web evaluator — WebJudge, the image judge.

Ported from ``benchmarks/Online-Mind2Web/eval/src/{run,webjudge_online_mind2web,utils}.py``.

The three-stage algorithm is preserved exactly:

1. **identify_key_points** — one text call turns the task description into
   an explicit checklist. The reply is cut at ``**Key Points**:`` (falling
   back to ``Key Points:``) and each line is left-stripped.
2. **judge_image** — one *vision* call per screenshot, scoring 1-5 how
   much that frame evidences task completion. The score is the first
   ``[1-5]`` digit after the literal ``Score``; the reasoning is the first
   paragraph after ``**Reasoning**:``. A frame that fails to parse scores
   0 and is recorded as such rather than dropped.
3. **WebJudge** — the frames scoring at or above ``score_threshold`` are
   re-attached to a final call together with their reasonings and the
   action history; the verdict is read off ``Status:``.

**The image path is the point of this evaluator, and it used to be dead.**
``RunTrace.screenshots`` globbed ``observations/*_screenshot.png`` while
``TraceWriter`` only ever wrote ``*_screenshot.json`` with the PNG
base64'd inside — so the list was empty for every trace ever recorded, the
grader took the ``webjudge_user_no_images`` branch, and emitted a weaker
text-only verdict with no error and no warning. Screenshots now persist as
real image files via ``ScreenshotObs.trace_binary()`` — ``.png``, or
``.jpg`` when the capability was configured for JPEG observations — and
``RunTrace.screenshots`` lists each committed step's file from the
manifest's fragment index, so stage 2 actually runs.

To make sure that can never silently regress, degradation is now recorded
in the verdict: ``benchmark_details`` carries ``screenshots_found`` and
``image_judge_skipped``, and ``require_screenshots`` turns a screenshot-less
trace into a hard error instead of a quietly different judgement.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import Field

from spectragent.core.llm.messages import ContentPartImageParam
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator._images import image_part
from spectragent.evaluator._registrar import LLMJudgeEvaluator, UsageTally
from spectragent.evaluator.errors import TraceIncompleteError
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import EvalDetails, EvalResult
from spectragent.schemas.prompt import PromptSnapshot

logger = logging.getLogger(__name__)

_IMAGE_CALL_DRAIN_TIMEOUT_S = 1.0
_SCORE_DIGIT = re.compile(r"[1-5]")
_FAILURE_CATEGORY = re.compile(r"Failure Category\s*:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
_REASONING_PATTERNS = (
    re.compile(r"Reasoning\s*:\s*(.+?)(?:\n(?:Category|Status)\s*:|$)", re.IGNORECASE | re.DOTALL),
    re.compile(r"Thoughts\s*:\s*(.+?)(?:\nStatus\s*:|$)", re.IGNORECASE | re.DOTALL),
)


@dataclass
class OnlineMind2WebEvaluator(LLMJudgeEvaluator):
    """WebJudge: key points → per-screenshot image judge → final verdict."""

    __registry_key__ = "online_mind2web"

    # A request that ignores cancellation remains owned until it actually
    # finishes. It must not be reported as stopped when the drain expires.
    _image_calls: set[asyncio.Task[str]] = field(default_factory=set, init=False, repr=False)

    image_detail: Annotated[
        str,
        Field("high", description="Vision detail level for judged screenshots."),
    ] = "high"

    score_threshold: Annotated[
        int,
        Field(
            3,
            description=(
                "Minimum per-image score (1-5) for a screenshot to be "
                "forwarded to the final judgement."
            ),
        ),
    ] = 3

    max_images: Annotated[
        int,
        Field(50, description="Cap on screenshots attached to the final call."),
    ] = 50

    require_screenshots: Annotated[
        bool,
        Field(
            False,
            description=(
                "Fail rather than fall back to the text-only prompt when a "
                "trace recorded no screenshots. The text-only path produces "
                "a materially weaker verdict; this makes that visible."
            ),
        ),
    ] = False

    async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult:
        view = TraceView(trace)
        task = view.task_text
        if not task:
            raise TraceIncompleteError(
                f"{view.trace_dir} records no task text. WebJudge grades "
                f"against the instruction; there is nothing to grade."
            )

        tally = UsageTally()
        snapshots: dict[str, PromptSnapshot] = {}

        key_points = await self._identify_key_points(task, tally, snapshots)
        screenshots = view.screenshots
        if not screenshots and self.require_screenshots:
            raise TraceIncompleteError(
                f"{view.trace_dir} has no screenshots under observations/. "
                f"WebJudge is an image judge; scoring it from text alone "
                f"yields a different, weaker verdict. Re-run with a "
                f"screenshot-providing capability or set "
                f"require_screenshots=false to accept the text-only path."
            )
        if not screenshots:
            logger.warning(
                "WebJudge: %s recorded no screenshots — falling back to the "
                "text-only prompt. This verdict is not comparable to an "
                "image-judged one.",
                view.trace_dir,
            )

        records, kept_images, kept_thoughts = await self._judge_images(
            task, key_points, screenshots, tally, snapshots
        )

        response, user_snapshot, system_snapshot = await self._final_judgement(
            task=task,
            key_points=key_points,
            # A branch run's restored prefix first, labelled, then what its
            # own agent did; for any other trace the first list is empty and
            # the prompt is what it always was.
            action_history=view.restored_action_history + view.action_history,
            images=kept_images,
            thoughts=kept_thoughts,
            tally=tally,
        )

        predicted_label = extract_predication(response)
        failure_category = extract_failure_category(response) if predicted_label == 0 else None

        details = EvalDetails(
            system_prompt=system_snapshot,
            user_prompt=user_snapshot,
            additional_prompts=snapshots or None,
            response=response,
            is_correct=bool(predicted_label),
            reasoning=extract_reasoning(response),
            eval_usage=tally.to_eval_usage(),
            benchmark_details={
                "key_points": key_points,
                "image_judge_record": records,
                # Explicit degradation trail. An empty ``screenshots_found``
                # is the signature of the bug this port exists to close.
                "screenshots_found": len(screenshots),
                "images_forwarded": len(kept_images),
                "image_judge_skipped": not screenshots,
                "score_threshold": self.score_threshold,
                "agent_done": view.agent_done,
            },
        )

        return self.build_result(
            view,
            predicted_label=predicted_label,
            details=details,
            task=task,
            failure_category=failure_category,
            agent_response=view.answer,
        )

    # ── Stage 1 ─────────────────────────────────────────────────────

    async def _identify_key_points(
        self,
        task: str,
        tally: UsageTally,
        snapshots: dict[str, PromptSnapshot],
    ) -> str:
        system, system_snapshot = self.text_prompt("identify_key_points_system")
        user, user_snapshot = self.render("identify_key_points_user", {"task": task})
        snapshots["identify_key_points_system"] = system_snapshot
        snapshots["identify_key_points_user"] = user_snapshot

        response = await self.ask(system=system, text=user, tally=tally)
        return _parse_key_points(response)

    # ── Stage 2 ─────────────────────────────────────────────────────

    async def _judge_images(
        self,
        task: str,
        key_points: str,
        screenshots: list[Any],
        tally: UsageTally,
        snapshots: dict[str, PromptSnapshot],
    ) -> tuple[list[dict[str, Any]], list[ContentPartImageParam], list[str]]:
        if not screenshots:
            return [], [], []

        system, system_snapshot = self.text_prompt("judge_image_system")
        user, user_snapshot = self.render(
            "judge_image_user", {"task": task, "key_points": key_points}
        )
        snapshots["judge_image_system"] = system_snapshot
        snapshots["judge_image_user"] = user_snapshot

        parts = [image_part(path, detail=self.image_detail) for path in screenshots]
        calls = [
            asyncio.create_task(self.ask(system=system, text=user, images=[part], tally=tally))
            for part in parts
        ]
        for call in calls:
            self._image_calls.add(call)
            call.add_done_callback(self._image_calls.discard)
            call.add_done_callback(_consume_finished)
        joined = asyncio.gather(*calls)
        joined.add_done_callback(_consume_finished)
        try:
            responses = await asyncio.shield(joined)
        except BaseException as original:
            # A judge may suppress cancellation. Drain for a finite budget,
            # then let the runner's model lifecycle proceed with an honest
            # account of calls still in flight. A new external cancellation
            # outranks an earlier provider error, with that error as its cause.
            later_cancel: asyncio.CancelledError | None = None
            try:
                later_cancel = await _cancel_and_drain(calls)
            except Exception as cleanup_error:
                logger.warning("Could not drain image judge calls", exc_info=True)
                original.add_note(
                    f"Image judge drain also failed: {type(cleanup_error).__name__}: {cleanup_error}"
                )
            outcome = (
                later_cancel
                if later_cancel is not None and not isinstance(original, asyncio.CancelledError)
                else original
            )
            try:
                records: list[dict[str, Any]] = []
                unfinished: list[int] = []
                cancelled: list[int] = []
                failed: list[dict[str, Any]] = []
                for index, call in enumerate(calls):
                    if not call.done():
                        unfinished.append(index)
                    elif call.cancelled():
                        cancelled.append(index)
                    elif error := call.exception():
                        failed.append({"index": index, "error": f"{type(error).__name__}: {error}"})
                    else:
                        response = call.result()
                        score, _ = _parse_image_judgement(response)
                        records.append({"index": index, "Response": response, "Score": score})
                previous = getattr(original, "details", None)
                details = (
                    previous.model_copy(deep=True)
                    if isinstance(previous, EvalDetails)
                    else EvalDetails()
                )
                details.additional_prompts = {
                    **snapshots,
                    **(details.additional_prompts or {}),
                }
                if details.eval_usage is None and tally is not None:
                    details.eval_usage = tally.to_eval_usage()
                details.benchmark_details.update(
                    {
                        "key_points": key_points,
                        "image_judge_record": records,
                        "screenshots_found": len(parts),
                        "score_threshold": self.score_threshold,
                        "image_judge_unfinished": unfinished,
                        "image_judge_cancelled": cancelled,
                        "image_judge_failed": failed,
                    }
                )
                outcome.__dict__["details"] = details
            except Exception as evidence_error:
                logger.warning("Could not retain partial image judge evidence", exc_info=True)
                outcome.add_note(
                    "Partial image judge evidence also failed: "
                    f"{type(evidence_error).__name__}: {evidence_error}"
                )
            if outcome is not original:
                raise outcome from original
            raise

        records: list[dict[str, Any]] = []
        kept_images: list[ContentPartImageParam] = []
        kept_thoughts: list[str] = []
        for response, part in zip(responses, parts, strict=True):
            score, thought = _parse_image_judgement(response)
            records.append({"Response": response, "Score": score})
            if score >= self.score_threshold:
                kept_images.append(part)
                if thought:
                    kept_thoughts.append(thought)

        return records, kept_images[: self.max_images], kept_thoughts[: self.max_images]

    # ── Stage 3 ─────────────────────────────────────────────────────

    async def _final_judgement(
        self,
        *,
        task: str,
        key_points: str,
        action_history: list[str],
        images: list[ContentPartImageParam],
        thoughts: list[str],
        tally: UsageTally,
    ) -> tuple[str, PromptSnapshot, PromptSnapshot]:
        system, system_snapshot = self.text_prompt("webjudge_system")
        template = "webjudge_user" if images else "webjudge_user_no_images"
        text, user_snapshot = self.render(
            template,
            {
                "task": task,
                "last_actions": _numbered(action_history),
                "key_points": key_points,
                "thoughts": _numbered(thoughts),
            },
        )
        response = await self.ask(system=system, text=text, images=images, tally=tally)
        return response, user_snapshot, system_snapshot


# ── Response parsing (verbatim ports) ───────────────────────────────


def _consume_finished(task: asyncio.Future[Any]) -> None:
    """Retrieve a late failure after a bounded drain stops waiting."""
    if not task.cancelled():
        task.exception()


async def _cancel_and_drain(calls: list[asyncio.Task[str]]) -> asyncio.CancelledError | None:
    """Request cancellation, then wait once for a finite cleanup budget.

    The wait runs in its own task so repeated cancellation of the evaluator
    cannot cancel the drain. Return the first cancellation delivered during
    cleanup so the caller can give it priority over an earlier ordinary error.
    """
    for call in calls:
        if not call.done():
            call.cancel()
    drain = asyncio.create_task(asyncio.wait(calls, timeout=_IMAGE_CALL_DRAIN_TIMEOUT_S))
    cancellation: asyncio.CancelledError | None = None
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    _, unfinished = drain.result()
    if unfinished:
        logger.warning(
            "%d image judge call(s) still running after %.1fs cancellation drain: %s",
            len(unfinished),
            _IMAGE_CALL_DRAIN_TIMEOUT_S,
            sorted(calls.index(call) for call in unfinished),
        )
    return cancellation


def _parse_key_points(response: str) -> str:
    """Cut the checklist out of the key-points reply."""
    text = response.replace("\n\n", "\n")
    if "**Key Points**:" in text:
        body = text.split("**Key Points**:")[1]
    else:
        body = text.split("Key Points:")[-1]
    return "\n".join(line.lstrip() for line in body.splitlines())


def _parse_image_judgement(response: str) -> tuple[int, str]:
    """Return ``(score, reasoning)`` for one judged frame.

    An unparseable reply scores 0 — never the threshold — so a malformed
    judgement can only ever *exclude* a frame, not smuggle one in.
    """
    try:
        score_text = response.split("Score")[1]
        thought = (
            response.split("**Reasoning**:")[-1]
            .strip()
            .lstrip("\n")
            .split("\n\n")[0]
            .replace("\n", " ")
        )
        score = int(_SCORE_DIGIT.findall(score_text)[0])
    except (IndexError, ValueError) as exc:
        logger.warning("WebJudge: unparseable image judgement (%s)", exc)
        return 0, ""
    return score, thought


def extract_predication(response: str) -> int:
    """1 when the judge said success, 0 otherwise (including on garbage)."""
    try:
        status_segment = response.lower().split("status:")[1]
    except IndexError:
        return 0
    return 1 if "success" in status_segment else 0


def extract_failure_category(response: str) -> str | None:
    """``Failure Category: X`` when the judge emitted one."""
    match = _FAILURE_CATEGORY.search(response)
    return match.group(1).strip() if match else None


def extract_reasoning(response: str) -> str | None:
    """The ``Reasoning:`` / ``Thoughts:`` block."""
    for pattern in _REASONING_PATTERNS:
        match = pattern.search(response)
        if match:
            return match.group(1).strip()
    return None


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))
