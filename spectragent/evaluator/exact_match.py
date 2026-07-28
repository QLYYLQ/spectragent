"""Exact-match evaluator — deterministic, offline, no judge model.

The cheapest possible scorer and the reference implementation of the
Protocol: it reads ``answer`` out of a trace, compares it to
``EvalReference.expected_answer``, and returns a verdict. No network, no
browser, no LLM — which is what makes it the evaluator an end-to-end test
can run in CI.

It is also the right scorer for a real family of benchmarks (GAIA and
most short-answer QA sets grade by normalized string equality), so this
is not a toy.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator._registrar import EvaluatorBase
from spectragent.evaluator.errors import ReferenceMismatchError
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import EvalDetails, EvalResult

_WHITESPACE = re.compile(r"\s+")
_ARTICLES = frozenset({"a", "an", "the"})


@dataclass
class ExactMatchEvaluator(EvaluatorBase):
    """Normalized string equality between the agent's answer and the key."""

    __registry_key__ = "exact_match"

    case_sensitive: Annotated[
        bool,
        Field(False, description="Compare without lowercasing first."),
    ] = False

    strip_punctuation: Annotated[
        bool,
        Field(True, description="Drop punctuation before comparing."),
    ] = True

    strip_articles: Annotated[
        bool,
        Field(True, description="Drop leading a/an/the before comparing."),
    ] = True

    allow_substring: Annotated[
        bool,
        Field(
            False,
            description=(
                "Accept when the expected answer occurs inside the agent's "
                "answer. Off by default: it turns a verbose wrong answer "
                "that happens to quote the key into a pass."
            ),
        ),
    ] = False

    async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult:
        view = TraceView(trace)

        expected = reference.expected_answer
        if expected is None:
            raise ReferenceMismatchError(
                f"Task {reference.task_id!r} of benchmark "
                f"{reference.benchmark!r} carries no expected_answer, so "
                f"exact_match has nothing to compare against. Use an "
                f"LLM-judge evaluator for rubric-scored benchmarks."
            )

        actual = view.answer
        norm_expected = self.normalize(expected)
        norm_actual = self.normalize(actual)

        matched = norm_actual == norm_expected
        if not matched and self.allow_substring and norm_expected:
            matched = norm_expected in norm_actual

        details = EvalDetails(
            response=actual,
            is_correct=matched,
            reasoning=(
                f"normalized answer {norm_actual!r} "
                f"{'==' if matched else '!='} expected {norm_expected!r}"
            ),
            benchmark_details={
                "normalized_answer": norm_actual,
                "normalized_expected": norm_expected,
                "agent_done": view.agent_done,
            },
        )

        return self.build_result(
            view,
            predicted_label=1 if matched else 0,
            details=details,
            failure_category=None if matched else "wrong_answer",
            correct_answer=expected,
            agent_response=actual,
        )

    def normalize(self, value: str) -> str:
        """Apply the configured normalizations, in a fixed order."""
        text = unicodedata.normalize("NFKC", value).strip()
        if not self.case_sensitive:
            text = text.lower()
        if self.strip_punctuation:
            text = text.translate(str.maketrans("", "", string.punctuation))
        text = _WHITESPACE.sub(" ", text).strip()
        if self.strip_articles:
            tokens = [t for t in text.split(" ") if t not in _ARTICLES]
            text = " ".join(tokens)
        return text
