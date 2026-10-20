"""Review job execution owns its calls, output directory and model cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.weaver import Weaver
from spectragent.core.llm.base import require_inference_protocol
from spectragent.core.llm.lifecycle import close_models
from spectragent.core.trace.encoding import PendingLLMCall, encode_model_call
from spectragent.core.trace.redaction import redact_config
from spectragent.replay_review.base import BaseReplayReviewer
from spectragent.replay_review.evidence import prepare_case
from spectragent.replay_review.models import ReviewAssessment, ReviewEvidenceError, ReviewPair
from spectragent.replay_review.storage import ReviewItem, ReviewStore, json_file

logger = logging.getLogger(__name__)


class ReviewCallRecorder:
    """A job-owned AOP observer using the same exactly-once pending invocation contract."""

    __intercepts__ = (Intercept("ainvoke", role="llm", advice="around"),)
    blocking = True
    hook_id = "replay_review_recording"

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.calls: list[PendingLLMCall] = []
        self.problems: list[str] = []

    async def around_ainvoke(self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any) -> Any:
        pending = None
        try:
            target = ctx["target"]
            pending = PendingLLMCall(
                None,
                kind="decision",
                owner="replay_review",
                owner_id=self.job_id,
                request=args[0] if args else kwargs["request"],
                call_id=f"{self.job_id}:{len(self.calls)}",
                call=len(self.calls),
                caller="replay_reviewer",
                purpose="replay_review",
                model=target.model,
            )
            self.calls.append(pending)
        except Exception as exc:
            self.problems.append(f"capture: {type(exc).__name__}: {exc}")
        try:
            result = await proceed()
        except BaseException as exc:
            if pending is not None:
                try:
                    pending.fail(exc) if isinstance(exc, Exception) else pending.cancel(exc)
                except Exception as fault:
                    self.problems.append(f"settle: {type(fault).__name__}: {fault}")
            raise
        if pending is not None:
            try:
                pending.complete(result)
            except Exception as exc:
                self.problems.append(f"settle: {type(exc).__name__}: {exc}")
        return result


class ReplayReviewRunner:
    def __init__(self, reviewer: BaseReplayReviewer) -> None:
        self.reviewer = reviewer

    async def run(
        self, pairs: list[ReviewPair], *, out: Path, config: Any = None
    ) -> dict[str, Any]:
        reviewer = self.reviewer
        require_inference_protocol(
            reviewer.decision_model,
            "decision",
            consumer=f"replay_reviewer[{reviewer.reviewer_id}].decision_model",
        )
        model = reviewer.decision_model
        job_id = uuid4().hex
        recorder = ReviewCallRecorder(job_id)
        weaver = Weaver([recorder])
        reviewer.decision_model = weaver.wrap(model, role="llm")
        weaver.raise_for_unclaimed_roles()
        weaver.audit_intercepts()
        store = ReviewStore(out)
        problems = []
        assessments = []
        summary: dict[str, Any] = {}
        initialized = False
        try:
            settings = reviewer.settings()
            store.create(
                job_id,
                len(pairs),
                {
                    "resolved": redact_config(config) if config is not None else {},
                    "reviewer": reviewer.reviewer_id,
                    "model": model.model,
                    "model_type": model.model_type,
                    "provider": model.provider,
                    "questions": reviewer.questions.content(),
                    "settings": settings.model_dump(mode="json"),
                    "pairs": [p.model_dump(mode="json") for p in pairs],
                },
            )
            initialized = True
            for index, pair in enumerate(pairs):
                first = len(recorder.calls)
                case = None
                cancelled = None
                try:
                    case = await prepare_case(pair, reviewer)
                    assessment = await reviewer.review(case)
                except ReviewEvidenceError as exc:
                    assessment = ReviewAssessment(
                        status=exc.status,
                        reasons=[exc.status],
                        detail=str(exc),
                        settings=settings,
                    )
                except asyncio.CancelledError as exc:
                    cancelled = exc
                    assessment = ReviewAssessment(
                        status="cancelled", reasons=["cancelled"], settings=settings
                    )
                except Exception as exc:
                    assessment = ReviewAssessment(
                        status="review_error",
                        reasons=["review_error"],
                        detail=f"{type(exc).__name__}: {exc}",
                        settings=settings,
                    )
                assessments.append(assessment)
                try:
                    calls = [
                        encode_model_call(
                            call, rel_path=f"calls/{call.call:06d}.json", step=None, call=call.call
                        )
                        for call in recorder.calls[first:]
                    ]
                    item = ReviewItem(
                        index=index,
                        status=assessment.status,
                        assessment=f"items/{index:06d}.json",
                        evidence=f"evidence/{index:06d}.json" if case is not None else None,
                        calls=[call.file.rel_path for call in calls],
                    )
                    files = [
                        json_file(
                            item.assessment,
                            assessment.model_dump(mode="json", exclude={"request", "result"}),
                        )
                    ]
                    if case is not None:
                        assert item.evidence is not None
                        files.append(
                            json_file(
                                item.evidence,
                                case.model_dump(
                                    mode="json",
                                    exclude={"reference": {"image"}, "replay": {"image"}},
                                ),
                            )
                        )
                    store.commit_item(item, files, calls)
                except Exception as exc:
                    problems.append(f"item {index}: {type(exc).__name__}: {exc}")
                    logger.warning("Review recording failed: %s", problems[-1])
                if cancelled is not None:
                    raise cancelled
        finally:
            reviewer.decision_model = model
            try:
                await close_models([model])
            finally:
                measured = [call.usage for call in recorder.calls if call.usage is not None]
                summary = {
                    "inputs": len(pairs),
                    "assessed": sum(a.status == "assessed" for a in assessments),
                    "statuses": dict(Counter(a.status for a in assessments)),
                    "routes": dict(Counter(a.route for a in assessments)),
                    "calls": len(recorder.calls),
                    "usage_reported_calls": len(measured),
                    "missing_usage_calls": len(recorder.calls) - len(measured),
                    "tokens_reported": sum(u["total_tokens"] for u in measured),
                    "reasons": dict(Counter(r for a in assessments for r in a.reasons)),
                    "threshold_scopes": dict(
                        Counter(a.thresholds.scope for a in assessments if a.thresholds)
                    ),
                }
                if initialized:
                    try:
                        store.finish([*problems, *recorder.problems], summary)
                    except Exception as exc:
                        problems.append(f"close: {type(exc).__name__}: {exc}")
                        logger.warning("Review recording close failed: %s", problems[-1])
        return {
            "job_id": job_id,
            "out": str(out),
            "summary": summary,
            "recording_problems": [*problems, *recorder.problems],
        }
