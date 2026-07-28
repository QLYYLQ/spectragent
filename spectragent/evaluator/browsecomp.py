"""BrowseComp evaluator — LLM grader over an encrypted answer key.

Ported from ``benchmarks/BrowseComp/eval/src/{run,utils}.py``, which was
unimportable (it did ``from spectragent.utils import ...`` and that module
has never existed).

Preserved scoring logic, unchanged:

* the answer key is XOR-encrypted under the record's canary string, and
  both question and answer are decrypted at grade time — the plaintext
  never touches disk, which is the whole point of BrowseComp shipping
  that way;
* the grader prompt is ``grader_user.txt``, formatted with
  ``{question, correct_answer, response}``, sent as a **single user
  message with no system prompt**;
* the verdict is ``re.search(r"correct: (yes|no)", ..., IGNORECASE)`` on
  the grader's reply, defaulting to *no* when the pattern is absent.

Dropped, deliberately: the bespoke ``GraderModel`` OpenAI client with its
own retry loop and its ``return "correct: no"`` on exhaustion. Swallowing
an API outage as a wrong answer silently converts infrastructure failure
into a benchmark score. The judge is now an ordinary registry LLM and a
transport failure propagates.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator._registrar import LLMJudgeEvaluator, UsageTally
from spectragent.evaluator.errors import ReferenceMismatchError
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import EvalDetails, EvalResult

_CORRECT = re.compile(r"correct:\s*(yes|no)", re.IGNORECASE)


def derive_key(password: str, length: int) -> bytes:
    """SHA-256 of the canary, repeated to the ciphertext's length."""
    key = hashlib.sha256(password.encode()).digest()
    return key * (length // len(key)) + key[: length % len(key)]


def decrypt(ciphertext_b64: str, password: str) -> str:
    """Undo BrowseComp's base64 + XOR obfuscation."""
    encrypted = base64.b64decode(ciphertext_b64)
    key = derive_key(password, len(encrypted))
    return bytes(a ^ b for a, b in zip(encrypted, key, strict=False)).decode()


@dataclass
class BrowseCompEvaluator(LLMJudgeEvaluator):
    """Grades a BrowseComp answer against the decrypted key."""

    __registry_key__ = "browsecomp"

    async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult:
        view = TraceView(trace)
        question, correct_answer = self._decrypt_pair(reference)
        agent_response = view.answer

        tally = UsageTally()
        prompt_text, user_snapshot = self.render(
            "grader_user",
            {
                "question": question,
                "correct_answer": correct_answer,
                "response": agent_response,
            },
        )
        response = await self.ask(text=prompt_text, tally=tally)

        match = _CORRECT.search(response)
        is_correct = bool(match) and match.group(1).lower() == "yes"

        details = EvalDetails(
            user_prompt=user_snapshot,
            response=response,
            is_correct=is_correct,
            reasoning=_reasoning(response),
            eval_usage=tally.to_eval_usage(),
            benchmark_details={
                "match_result": match.group(1).lower() if match else "no",
                "grader_matched_pattern": bool(match),
                "agent_done": view.agent_done,
            },
        )

        return self.build_result(
            view,
            predicted_label=1 if is_correct else 0,
            details=details,
            task=question,
            failure_category=None if is_correct else "wrong_answer",
            correct_answer=correct_answer,
            agent_response=agent_response,
        )

    # ── internals ───────────────────────────────────────────────────

    def _decrypt_pair(self, reference: EvalReference) -> tuple[str, str]:
        ref = reference.reference
        canary = str(reference.metadata.get("canary") or "")
        if ref is None or not ref.encrypted_question or not ref.encrypted_answer:
            raise ReferenceMismatchError(
                f"BrowseComp task {reference.task_id!r} is missing "
                f"reference.encrypted_question / reference.encrypted_answer. "
                f"The answer key ships encrypted; there is nothing to grade "
                f"against without it."
            )
        if not canary:
            raise ReferenceMismatchError(
                f"BrowseComp task {reference.task_id!r} has no "
                f"metadata['canary']; it is the decryption password for the "
                f"encrypted question and answer."
            )
        return (
            decrypt(ref.encrypted_question, canary),
            decrypt(ref.encrypted_answer, canary),
        )


def _reasoning(response: str) -> str | None:
    """The grader's explanation, i.e. everything before its verdict line."""
    match = _CORRECT.search(response)
    if match is None:
        return response.strip() or None
    head = response[: match.start()].strip()
    return head or None
