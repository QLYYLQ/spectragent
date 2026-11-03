"""One verifier's account of a judging pass: given, judged, passed, failed, and what its judge spent.

The denominator is what the pass was given, never a whole benchmark split:
a task nobody ran is not counted as anything, and a trace a verifier could
not judge (a judge call failed, the evidence was not enough) is an error,
never a failed task. The success rate is over the judged verdicts that carry
a label, with the coverage beside it (``docs/architecture_design.md`` §4.11
"独立入口").
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from spectragent.schemas.verdict import JudgeUsage, VerifierIdentity
from spectragent.verifiers.runner import VerificationReport


@dataclass(frozen=True)
class VerifierTally:
    """One verifier's counts over a pass.

    Attributes:
        verifier: Who judged.
        provided: Traces it was given.
        judged: Verdicts it wrote.
        passed: Of those, labelled 1.
        failed: Of those, labelled 0.
        errors: Traces it could not judge.
        usage: What its judge model spent over the pass, including the
            attempts that failed; ``None`` when it never called one.
    """

    verifier: VerifierIdentity
    provided: int
    judged: int
    passed: int
    failed: int
    errors: int
    usage: JudgeUsage | None

    @property
    def coverage(self) -> float | None:
        """Judged over provided; ``None`` when it was given nothing."""
        return self.judged / self.provided if self.provided else None

    @property
    def success_rate(self) -> float | None:
        """Passed over the labelled verdicts; ``None`` when there is none."""
        labelled = self.passed + self.failed
        return self.passed / labelled if labelled else None

    @property
    def calls_without_usage(self) -> int:
        """Judge calls whose provider reported no usage: unknown, never free."""
        return self.usage.calls - self.usage.reported_calls if self.usage is not None else 0

    def describe(self) -> str:
        """One line: who, the counts, the rate with its coverage."""
        rate = f"{self.success_rate:.1%}" if self.success_rate is not None else "-"
        coverage = f"{self.coverage:.1%}" if self.coverage is not None else "-"
        line = (
            f"{self.verifier.slug}: judged {self.judged} of {self.provided} "
            f"(coverage {coverage}), passed {self.passed}, failed {self.failed}, "
            f"could not judge {self.errors}; success rate {rate}"
        )
        if self.usage is not None:
            line += (
                f"; judge tokens prompt={self.usage.prompt_tokens} "
                f"completion={self.usage.completion_tokens} cached={self.usage.cached_tokens}"
            )
            if self.calls_without_usage:
                line += f", {self.calls_without_usage} call(s) reported no usage (not counted)"
        return line


def tallies(report: VerificationReport) -> list[VerifierTally]:
    """Each verifier's counts over *report*, in the order the verifiers first appear."""
    slugs: dict[str, VerifierIdentity] = {}
    for outcome in report.outcomes:
        slugs.setdefault(outcome.slug, outcome.verifier)
    counted: list[VerifierTally] = []
    for slug, identity in slugs.items():
        own = [outcome for outcome in report.outcomes if outcome.slug == slug]
        verdicts = [outcome.verdict for outcome in own if outcome.verdict is not None]
        counted.append(
            VerifierTally(
                verifier=identity,
                provided=len(own),
                judged=len(verdicts),
                passed=sum(verdict.label == 1 for verdict in verdicts),
                failed=sum(verdict.label == 0 for verdict in verdicts),
                errors=sum(outcome.error is not None for outcome in own),
                usage=total_usage(
                    [
                        *(verdict.usage for verdict in verdicts),
                        *(outcome.usage for outcome in own if outcome.error is not None),
                    ]
                ),
            )
        )
    return counted


def total_usage(usages: Iterable[JudgeUsage | None]) -> JudgeUsage | None:
    """The sum of *usages*; ``None`` when none was recorded (never zero-filled)."""
    known = [usage for usage in usages if usage is not None]
    if not known:
        return None
    return JudgeUsage(
        calls=sum(usage.calls for usage in known),
        reported_calls=sum(usage.reported_calls for usage in known),
        prompt_tokens=sum(usage.prompt_tokens for usage in known),
        completion_tokens=sum(usage.completion_tokens for usage in known),
        cached_tokens=sum(usage.cached_tokens for usage in known),
    )


__all__ = ["VerifierTally", "tallies", "total_usage"]
