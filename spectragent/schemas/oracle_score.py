"""What a task oracle reads from the live environment, and the score the trace records.

A capability implementing ``TaskOracle`` (``core/capabilities/base.py``) hands
back an :class:`OracleReading`, holding only what the oracle itself knows. The
environment builds every :class:`OracleScore`, one entry of a run's
``oracle_scores.json`` (``docs/architecture_design.md`` §4.2 "判分时间点",
§5.3): a scored entry from a reading, with the capability's id and the start
time and duration it measured around the call, or the not-scored entry that
stands in for an oracle that timed out or raised.

Both sit with the other records that cross from the capabilities into the
trace (``schemas/restoration.py``, ``schemas/agent_result.py``), not in
``core/trace/models.py``: neither the capability nor the environment imports
the trace layer (§10).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class OracleReading(BaseModel):
    """What a task oracle hands back for this task: only what the oracle itself knows.

    The environment adds the rest when it records the :class:`OracleScore`:
    the capability's id, and when the read started and how long it took, on
    its own clock. Any other field is refused, so an oracle cannot report its
    own timing.

    Attributes:
        score: The benchmark's own score (AndroidWorld: 0 to 1, partial credit
            for composite tasks).
        oracle: The oracle's identity, such as ``"androidworld"``.
        oracle_version: The oracle's version, such as a pinned commit.
        provenance: What was judged (the benchmark's task id, a digest of its
            parameters).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    score: FiniteFloat
    oracle: str
    oracle_version: str | None = None
    provenance: dict[str, str] = Field(default_factory=dict)


class OracleScore(BaseModel):
    """One capability's task oracle on this task: its score, or why it gave none.

    ``status`` decides the rest. A ``scored`` entry carries the score and the
    oracle that gave it and no reason; a ``not_scored`` entry carries no score,
    and says why (``reason``) with a diagnostic (``detail``). An evaluator
    counts neither a not-scored entry nor a run without one as a failure
    (§4.11).

    Attributes:
        capability_id: The capability whose oracle this is.
        status: ``scored`` or ``not_scored``.
        score: The benchmark's own score; present exactly when scored.
        oracle: The oracle's identity; present when scored, ``None`` when the
            oracle handed nothing back.
        oracle_version: The oracle's version.
        provenance: What was judged, as the oracle gave it.
        started_at: Wall-clock Unix time the environment asked the oracle,
            comparable with ``StepRecord.timestamp``.
        duration_ms: How long the oracle took, measured by the environment
            around the call.
        reason: Why there is no score (``timeout`` or ``error``); present
            exactly when not scored.
        detail: The not-scored diagnostic: the limit for a timeout; the
            exception's class name and message for an error.
    """

    model_config = ConfigDict(frozen=True)

    capability_id: str
    status: Literal["scored", "not_scored"]
    score: FiniteFloat | None = None
    oracle: str | None = None
    oracle_version: str | None = None
    provenance: dict[str, str] = Field(default_factory=dict)
    started_at: FiniteFloat
    duration_ms: int = Field(ge=0)
    reason: Literal["timeout", "error"] | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _the_status_decides_the_rest(self) -> OracleScore:
        if self.status == "scored":
            if self.score is None or self.oracle is None:
                raise ValueError("a scored entry carries its score and the oracle that gave it")
            if self.reason is not None:
                raise ValueError(f"a scored entry has no reason not to be scored ({self.reason!r})")
        else:
            if self.reason is None:
                raise ValueError("a not_scored entry says why: reason 'timeout' or 'error'")
            if self.score is not None:
                raise ValueError(f"a not_scored entry carries no score (got {self.score!r})")
        return self
