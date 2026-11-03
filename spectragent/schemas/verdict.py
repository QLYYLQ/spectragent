"""One verifier's verdict on one run, and what goes into it (``docs/architecture_design.md`` §5.3 "判定统一存放").

Every verdict — from a verifier at the scoring point (``live``) or from the
committed trace (``offline``) — is one :class:`Verdict`, kept in one place:
the run directory's ``verdicts/<slug>.json`` (``core/trace/verdicts.py``).
Each says who judged (:class:`VerifierIdentity`: the verifier's kind, version,
judge model and the settings that can change its verdict), where it was
attached, whether it judged, what it found, and when.

A verifier hands back only what it found, a :class:`Judgment`. Whoever
attaches it — the environment at the scoring point, the offline runner — adds
the identity, the attachment, and the start time and duration it measured
around the call, so a verdict's timing never depends on the verifier's own
clock. At the scoring point a verifier that ran out of time or raised becomes
a ``not_judged`` verdict saying why: that verdict cannot be asked for again
once the environment is torn down. Offline, a failed attempt writes nothing;
the trace is still there to judge again.

Like the other records that cross from the capabilities into the trace
(``schemas/restoration.py``, ``schemas/agent_result.py``), this module imports
nothing of ``spectragent.core``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, model_validator

#: Where a verifier is attached (§4.10.1 "只有两个挂接点"): at the scoring point,
#: against the live environment, or offline, from the committed trace.
Attachment = Literal["live", "offline"]


class JudgeUsage(BaseModel):
    """What a judge model spent on one verdict, as its provider reported it.

    ``calls`` counts every call the verifier made; ``reported_calls`` those
    whose provider reported usage. The tokens sum only the reported ones, so a
    call without a report is visible (``calls > reported_calls``) and never
    counted as free.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: int = Field(ge=0)
    reported_calls: int = Field(ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class VerifierIdentity(BaseModel):
    """Who judged: the verifier's kind, its version, its judge model and its settings.

    Attributes:
        verifier_id: The verifier's kind, its registry key (``online_mind2web``,
            ``exact_match``).
        version: The verifier's version, when it has one (a pinned commit).
        model: The judge model's identity for an LLM judge; ``None`` for every
            other kind.
        config_signature: Every setting that can change the verdict (``{}``
            when there is none): two verifiers of one kind with different
            settings are different judges.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_id: str = Field(min_length=1)
    version: str | None = None
    model: str | None = None
    config_signature: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def slug(self) -> str:
        """The verdict's name in the store: ``<verifier_id>_<first 8 hex of the settings' sha256>``.

        Stable across processes (sha256 over sorted JSON, never ``hash``), so
        judging a run again with the same settings replaces its verdict and
        judging it with other settings lands beside it.
        """
        payload = json.dumps(self.config_signature, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
        return f"{self.verifier_id}_{digest}"


class Judgment(BaseModel):
    """What a verifier found on one run, and nothing else.

    Attributes:
        score: The verifier's own score, on its benchmark's scale (AndroidWorld:
            0 to 1, partial credit for composite tasks; LexBench: 0 to 100).
        label: Its pass or fail, when it decides one: 1 passed, 0 failed.
        rationale: Why, in the verifier's words (a judge's reasoning).
        evidence: What it judged and how: the prompts it sent, the replies,
            what it read from the benchmark's task. JSON only.
        usage: What its judge model spent, for an LLM judge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    score: FiniteFloat | None = None
    label: Literal[0, 1] | None = None
    rationale: str | None = None
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    usage: JudgeUsage | None = None

    @model_validator(mode="after")
    def _says_something(self) -> Judgment:
        if self.score is None and self.label is None:
            raise ValueError("a judgment carries a score, a label or both")
        return self


class Verdict(BaseModel):
    """One verifier's verdict on one run: who judged, where, whether, what, when.

    ``status`` decides the rest. A ``judged`` verdict carries what the
    verifier found (a score, a label or both, with its rationale and
    evidence) and no reason; a ``not_judged`` one — only ever written at the
    scoring point — carries no finding and says why (``reason``) with a
    diagnostic (``detail``). Neither a ``not_judged`` verdict nor a run with
    none counts as a failed task.

    Attributes:
        verifier: Who judged.
        attachment: ``live`` (at the scoring point) or ``offline`` (from the
            committed trace).
        status: ``judged`` or ``not_judged``.
        score, label, rationale, evidence, usage: What it found
            (:class:`Judgment`).
        reason: Why there is no finding: ``timeout`` (the attachment's time
            limit cut it off) or ``error`` (it raised, or handed back
            something that is not a judgment).
        detail: The ``not_judged`` diagnostic: the limit for a timeout; the
            exception's class name and message for an error.
        started_at: Wall-clock Unix time the verifier was asked, comparable
            with ``StepRecord.timestamp``.
        duration_ms: How long it took, measured around the call by whoever
            attached it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    verifier: VerifierIdentity
    attachment: Attachment
    status: Literal["judged", "not_judged"]
    score: FiniteFloat | None = None
    label: Literal[0, 1] | None = None
    rationale: str | None = None
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    usage: JudgeUsage | None = None
    reason: Literal["timeout", "error"] | None = None
    detail: str | None = None
    started_at: FiniteFloat
    duration_ms: int = Field(ge=0)

    @property
    def slug(self) -> str:
        """Its name in the store (:attr:`VerifierIdentity.slug`)."""
        return self.verifier.slug

    @classmethod
    def judged(
        cls,
        verifier: VerifierIdentity,
        attachment: Attachment,
        judgment: Judgment,
        *,
        started_at: float,
        duration_ms: int,
        evidence: dict[str, JsonValue] | None = None,
    ) -> Verdict:
        """The verdict of *judgment*; *evidence* adds what the attaching side knows (the reference it used)."""
        return cls(
            verifier=verifier,
            attachment=attachment,
            status="judged",
            score=judgment.score,
            label=judgment.label,
            rationale=judgment.rationale,
            evidence={**judgment.evidence, **(evidence or {})},
            usage=judgment.usage,
            started_at=started_at,
            duration_ms=duration_ms,
        )

    @model_validator(mode="after")
    def _the_status_decides_the_rest(self) -> Verdict:
        if self.status == "judged":
            if self.score is None and self.label is None:
                raise ValueError("a judged verdict carries a score, a label or both")
            if self.reason is not None:
                raise ValueError(
                    f"a judged verdict has no reason not to be judged ({self.reason!r})"
                )
        else:
            if self.reason is None:
                raise ValueError("a not_judged verdict says why: reason 'timeout' or 'error'")
            if self.score is not None or self.label is not None:
                raise ValueError("a not_judged verdict carries no score and no label")
        return self


__all__ = ["Attachment", "JudgeUsage", "Judgment", "Verdict", "VerifierIdentity"]
