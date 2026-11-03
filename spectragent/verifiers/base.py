"""The verifier contracts (``docs/architecture_design.md`` §4.10.1 "判分器有两条轴", §4.11).

A verifier judges a run. Its kind is a registry key (``VerifierRegistrar``,
``verifier_id``): an LLM judge, Python rules, a command. It is written only
inside a benchmark block, in its ``verifiers`` list, and never sets anything
on the run: it says what it needs, never how the run is configured.

Where it is attached is not configured either. It follows from what the
verifier declares it needs (:data:`VerifierNeed`), as the trace writer is
attached by what it intercepts: one that needs the live environment judges at
the scoring point, after the agent's run and before the teardown; every other
one judges offline, from the committed trace (:func:`attachment_of`), which is
what lets the same trace be judged again later, by another judge, on a machine
with no browser. There is no per-step attachment. A verifier never changes the
run, never ends it, and its failure never changes how the agent ended.

A verifier hands back only what it found (:class:`~spectragent.schemas.verdict.Judgment`);
the side that attached it adds who judged, where and when, and writes the
:class:`~spectragent.schemas.verdict.Verdict` into the run's verdict store.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.verdict import Attachment, Judgment, VerifierIdentity

#: What a verifier can need to judge a run:
#:
#: - ``live_environment``: the environment itself, alive after the agent's run
#:   (a device's databases and settings); it attaches the verifier at the
#:   scoring point;
#: - ``trace``: the committed trace (what the agent saw, did and answered);
#: - ``answer``: the run's final answer, handed over at the scoring point (an
#:   offline verifier reads it from the trace);
#: - ``reference``: the benchmark's reference row for the task, from its eval
#:   loader (a task-variant branch has none);
#: - ``model``: a judge model.
VerifierNeed = Literal["live_environment", "trace", "answer", "reference", "model"]


@runtime_checkable
class Verifier(Protocol):
    """What every verifier says about itself: its kind, what it needs, who it is."""

    @property
    def verifier_id(self) -> str:
        """Its kind: the registry key it registers under."""
        ...

    @property
    def needs(self) -> frozenset[VerifierNeed]:
        """What it needs to judge a run; where it is attached follows (:func:`attachment_of`)."""
        ...

    def identity(self) -> VerifierIdentity:
        """Who it is, as every verdict it gives records it: kind, version, judge model, settings."""
        ...


@runtime_checkable
class OfflineVerifier(Verifier, Protocol):
    """A verifier that judges a run from its committed trace, after the run."""

    async def verify(self, trace: RunTrace, reference: EvalReference | None) -> Judgment:
        """Judge *trace*; *reference* is the task's reference row exactly when it needs one.

        Raises:
            VerificationError: It cannot judge this trace (a judge call failed,
                the trace lacks what it reads). Not a failed task: nothing is
                written, and the trace can be judged again.
        """
        ...


@runtime_checkable
class VerifierLifecycle(Protocol):
    """Optional: the resources a verifier owns for one judging pass (its judge model's client)."""

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]: ...


def attachment_of(verifier: Verifier) -> Attachment:
    """Where *verifier* is attached: ``live`` when it needs the live environment, else ``offline``."""
    return "live" if "live_environment" in verifier.needs else "offline"


__all__ = [
    "OfflineVerifier",
    "Verifier",
    "VerifierLifecycle",
    "VerifierNeed",
    "attachment_of",
]
