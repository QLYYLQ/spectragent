"""BaseEvaluator Protocol — the scoring contract.

**Trace in, verdict out.** An Evaluator receives a persisted
:class:`~spectragent.core.trace.reader.RunTrace` and the benchmark's
:class:`~spectragent.schemas.eval_reference.EvalReference`, and returns an
:class:`~spectragent.schemas.eval_result.EvalResult`. It never touches a
live environment, never re-runs the agent, and never reads in-memory
state — which is precisely what makes "Record Once, Evaluate Many" true:
the same trace directory can be scored by a dozen evaluators, months
apart, on a machine that has no browser installed.

Scoring that genuinely needs a *live* environment — SWE-bench running
``pytest FAIL_TO_PASS`` inside the container, OSWorld evaluating
predicates against the VM — does not belong here. It goes through
``RuntimeHook.on_run_end(result, env)``, which is the one moment the
environment is both alive and reachable by a framework-aware component.
Such a hook writes its findings *into the trace*, and an Evaluator then
reads them back like any other recorded fact.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import EvalResult


@runtime_checkable
class BaseEvaluator(Protocol):
    """Contract every registered evaluator satisfies.

    ``evaluator_id`` mirrors the conscribe registry key, exactly as
    ``agent_id`` / ``hook_id`` / ``benchmark_name`` do on the other
    layers. Benchmark-specific evaluators key on the *same* string as
    their task and eval loaders (``browsecomp``, ``online_mind2web``,
    ``lexbench_browser``), so a benchmark name resolves a loader, a
    reference set and a grader with no lookup table anywhere.
    """

    @property
    def evaluator_id(self) -> str: ...

    def config_signature(self) -> dict[str, Any]:
        """Settings that can change this evaluator's verdict; ``{}`` when none exist."""
        ...

    @property
    def verdict_slug(self) -> str:
        """Stable filename identity derived from evaluator id and settings."""
        ...

    async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult: ...


@runtime_checkable
class EvaluatorLifecycle(Protocol):
    """Optional resource ownership for an independent evaluation execution."""

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]: ...
