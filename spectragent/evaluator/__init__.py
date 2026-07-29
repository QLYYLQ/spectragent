"""Evaluator layer — the "Evaluate Many" half of Record Once, Evaluate Many.

Evaluation was the one varying axis of this framework with no registry:
every other axis is a ``conscribe`` registrar reachable from YAML, while
scoring lived in per-benchmark scripts outside the package. This package
closes that gap.

Layout mirrors every other layer::

    base.py         BaseEvaluator Protocol   — trace in, verdict out
    _registrar.py   EvaluatorRegistrar       — keyed on ``evaluator_id``
    runner.py       EvaluationRunner         — score a directory of traces
    summary.py      EvalResult[] -> EvalSummary
    <benchmark>.py  one registered evaluator per benchmark

Benchmark evaluators register under the **same key as their loaders**
(``browsecomp``, ``online_mind2web``, ``lexbench_browser``), and their
prompts live at ``prompts/evaluators/<that key>/`` — so a benchmark name
resolves task data, reference data, grader and prompts with no alias
table anywhere.

``evaluator`` is an optional layer in the composed config. A run that
only records traces omits it; a run that scores as it goes adds::

    evaluator:
      - evaluator_id: online_mind2web
        score_threshold: 3
        require_screenshots: true
        # The judge is an ordinary member of the llm registry, so it can
        # be a different provider than the agent under test — which is
        # the point of grading through a registrar rather than a script.
        llm:
          model_type: anthropic
          model: claude-sonnet-4-5
          provider:
            name: direct

Scoring then runs after the sweep::

    spectragent-bench --benchmark online_mind2web --subset test --config run.yaml

which writes one trace directory per task, an ``EvalResult`` into each
trace's ``evals/`` (plus a flat copy under ``--evals-dir``), and prints
the ``EvalSummary``. ``--no-eval`` records traces only; the traces stay
scorable later, which is the entire premise.
"""

from spectragent.evaluator._registrar import (
    EvaluatorBase,
    EvaluatorRegistrar,
    LLMJudgeEvaluator,
    UsageTally,
)
from spectragent.evaluator.base import BaseEvaluator
from spectragent.evaluator.browsecomp import BrowseCompEvaluator
from spectragent.evaluator.errors import (
    EvaluationError,
    ReferenceMismatchError,
    TraceIncompleteError,
)
from spectragent.evaluator.exact_match import ExactMatchEvaluator
from spectragent.evaluator.lexbench_browser import LexBenchBrowserEvaluator
from spectragent.evaluator.online_mind2web import OnlineMind2WebEvaluator
from spectragent.evaluator.prompts import (
    EVALUATOR_PROMPT_ROOT,
    evaluator_prompt_dir,
    load_prompt,
)
from spectragent.evaluator.runner import (
    EvaluationOutcome,
    EvaluationReport,
    EvaluationRunner,
    discover_traces,
)
from spectragent.evaluator.summary import format_summary, summarize
from spectragent.evaluator.trace_view import TraceView
from spectragent.prompts import PromptNotFoundError

__all__ = [
    # Contract + registry
    "BaseEvaluator",
    "EvaluatorRegistrar",
    "EvaluatorBase",
    "LLMJudgeEvaluator",
    "UsageTally",
    # Registered evaluators
    "ExactMatchEvaluator",
    "BrowseCompEvaluator",
    "OnlineMind2WebEvaluator",
    "LexBenchBrowserEvaluator",
    # Driving
    "EvaluationRunner",
    "EvaluationReport",
    "EvaluationOutcome",
    "discover_traces",
    "summarize",
    "format_summary",
    # Reading
    "TraceView",
    "EVALUATOR_PROMPT_ROOT",
    "evaluator_prompt_dir",
    "load_prompt",
    # Errors
    "EvaluationError",
    "PromptNotFoundError",
    "ReferenceMismatchError",
    "TraceIncompleteError",
]
