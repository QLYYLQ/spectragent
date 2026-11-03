"""The verifier layer — the judging half of Record Once, Evaluate Many.

A verifier judges a run (``docs/architecture_design.md`` §4.10.1, §4.11). Its
kind is a registry key (``verifier_id``); it is written only in a benchmark
block's ``verifiers`` list; where it is attached follows from what it needs:
the live environment at the scoring point, otherwise the committed trace,
offline. Every verdict, from either attachment, is one ``Verdict`` in the
run's own store (``verdicts/<slug>.json``).

Layout::

    base.py         the Verifier protocols, what a verifier can need, attachment_of
    _registrar.py   VerifierRegistrar, VerifierBase, LLMJudgeVerifier
    exact_match.py  a generic verifier; a benchmark's own lives in its package
    trace_view.py   the read-side vocabulary offline verifiers share
    runner.py       VerificationRunner: offline verifiers over traces
    report.py       one verifier's counts over a pass
    cli.py          ``spectragent eval``

Importing this package imports no verifier implementation.
"""

from spectragent.verifiers._registrar import (
    LLMJudgeVerifier,
    UsageTally,
    VerifierBase,
    VerifierRegistrar,
)
from spectragent.verifiers.base import (
    OfflineVerifier,
    Verifier,
    VerifierLifecycle,
    VerifierNeed,
    attachment_of,
)

__all__ = [
    "LLMJudgeVerifier",
    "OfflineVerifier",
    "UsageTally",
    "Verifier",
    "VerifierBase",
    "VerifierLifecycle",
    "VerifierNeed",
    "VerifierRegistrar",
    "attachment_of",
]
