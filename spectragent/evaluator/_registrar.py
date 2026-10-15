"""Evaluator registry using conscribe.

Evaluation was the only varying axis in this framework with no registry.
Every other axis — agent, loop, llm, memory, environment, capability,
action, observation fragment, hook, task loader, eval *data* loader — is
a ``create_registrar`` keyed on a discriminator and reachable from YAML.
Scoring was a gitignored ``run.py`` per benchmark with its own argparse,
its own OpenAI client and an import of a module that does not exist.

This module closes that gap with the same three pieces every other layer
uses: a Protocol (:mod:`spectragent.evaluator.base`), a registrar keyed on
``evaluator_id``, and a concrete base that subclasses inherit from and
conscribe auto-registers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar

from conscribe import create_registrar

from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic, close_models
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.base import BaseEvaluator
from spectragent.evaluator.prompts import evaluator_prompt_dir, load_prompt
from spectragent.evaluator.trace_view import TraceView
from spectragent.prompts import template_snapshot, text_snapshot
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import (
    EvalDetails,
    EvalResult,
    EvalUsage,
)
from spectragent.schemas.prompt import PromptRef, TemplatePrompt, TextPrompt

EvaluatorRegistrar = create_registrar(
    "evaluator",
    BaseEvaluator,
    discriminator_field="evaluator_id",
)


@dataclass
class EvaluatorBase(metaclass=EvaluatorRegistrar.Meta):
    """Concrete base for every evaluator.

    Subclasses set ``__registry_key__`` and implement :meth:`evaluate`.
    For benchmark graders the key is the **benchmark name** — the same
    string the task and eval loaders register under — so
    ``EvaluatorRegistrar.get(benchmark)`` and
    ``EvalLoaderRegistrar.get(benchmark)`` are the same lookup, and the
    prompt directory is ``prompts/evaluators/<that key>/``.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    # ── Protocol implementation ─────────────────────────────────────

    @property
    def evaluator_id(self) -> str:
        """The conscribe registry key."""
        return self.__registry_key__

    async def evaluate(self, trace: RunTrace, reference: EvalReference) -> EvalResult:
        raise NotImplementedError(
            f"Evaluator {self.evaluator_id!r} does not implement evaluate(trace, reference)."
        )

    # ── Prompt access ───────────────────────────────────────────────

    @property
    def prompt_dir(self):
        """This evaluator's prompt directory — derived, never mapped."""
        return evaluator_prompt_dir(self.__registry_key__)

    def load_prompt(self, name: str) -> tuple[str, PromptRef]:
        return load_prompt(self.__registry_key__, name)

    # ── Configuration identity ──────────────────────────────────────

    def config_signature(self) -> dict[str, Any]:
        """Every setting of this evaluator that can change a verdict.

        ``evaluator_id`` is a *name*, not an identity: ``exact_match``
        with ``allow_substring`` on and off are two different scorers
        wearing one label, and an LLM judge is a different scorer the
        moment the judge model changes. Recording the settings alongside
        the verdict is what lets two verdicts be compared — and hashing
        them into :attr:`verdict_slug` is what stops the second one from
        overwriting the first.
        """
        signature: dict[str, Any] = {}
        for spec in fields(self):
            if not spec.init or spec.name.startswith("_"):
                continue
            signature[spec.name] = _jsonable(getattr(self, spec.name, None))
        judge = self._judge_id()
        if judge is not None:
            signature["llm"] = judge
            instance = getattr(self, "llm", None)
            endpoint = getattr(instance, "base_url", None)
            provider = getattr(instance, "provider", None)
            signature["judge_route"] = {
                "implementation": type(instance).__name__,
                "provider": type(provider).__name__ if provider is not None else None,
                # A free-form endpoint can carry credentials. Compare routes without
                # recording URL credentials, path tokens or query strings.
                "endpoint_sha256": (
                    hashlib.sha256(str(endpoint).encode()).hexdigest() if endpoint else None
                ),
            }
        return signature

    @property
    def config_digest(self) -> str:
        """Short, stable digest of :meth:`config_signature`.

        Stable across processes — hence ``sha256`` over sorted JSON and
        not :func:`hash`, which is salted per interpreter and would give
        the same evaluator a different filename on every run.
        """
        payload = json.dumps(self.config_signature(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

    @property
    def verdict_slug(self) -> str:
        """Filename stem for verdicts this evaluator produces.

        ``docs/execution_dag.md`` requires the evals filename to carry
        "evaluator_id + version", and ``architecture_design.md`` draws the
        end state as ``llm_judge_gpt4o_v1.json`` and
        ``llm_judge_claude_v2.json`` sitting side by side. The digest is
        that version, derived rather than hand-maintained: re-scoring a
        trace under a changed threshold, normalization or judge model
        lands in a new file instead of destroying the old verdict — which
        for a non-deterministic judge cost real money and is not
        recomputable.
        """
        return f"{self.evaluator_id}_{self.config_digest}"

    def _judge_id(self) -> str | None:
        """Identity of the injected judge model, when there is one.

        ``getattr`` with a default is deliberate: before composition
        injects it, conscribe's ``WiredField`` descriptor raises
        ``AttributeError`` — an evaluator asked for its signature too
        early has no judge, rather than a judge called ``None``.
        """
        judge = getattr(self, "llm", None)
        if judge is None:
            return None
        for attr in ("model", "name"):
            value = getattr(judge, attr, None)
            if isinstance(value, str) and value:
                return value
        return type(judge).__name__

    # ── Result assembly ─────────────────────────────────────────────

    def build_result(
        self,
        view: TraceView,
        *,
        predicted_label: int,
        details: EvalDetails,
        task: str = "",
        failure_category: str | None = None,
        task_type: str | None = None,
        correct_answer: str | None = None,
        agent_response: str | None = None,
    ) -> EvalResult:
        """Assemble the standard ``EvalResult`` envelope.

        Every evaluator produces the same envelope; only
        ``evaluation_details`` and the verdict differ. Centralising it
        means ``agent_metrics`` is always nested (never flat-merged into
        the eval usage) and the trace back-pointer is never forgotten.
        """
        if details.agent_metrics is None:
            details.agent_metrics = view.metrics
        return EvalResult(
            task_id=view.task_id,
            task=task or view.task_text,
            timestamp=datetime.now(UTC),
            evaluator_id=self.evaluator_id,
            evaluator_config=self.config_signature(),
            benchmark=view.benchmark,
            subset=view.subset,
            run_id=view.run_id,
            agent_result_ref=view.result_ref(),
            predicted_label=predicted_label,
            model_id=view.model_id,
            browser_id=view.browser_id,
            evaluation_details=details,
            failure_category=failure_category,
            task_type=task_type,
            correct_answer=correct_answer,
            agent_response=agent_response,
        )


def _jsonable(value: Any) -> Any:
    """Coerce a config value into something JSON can hold.

    Config fields are scalars in practice; the recursive cases exist so a
    list- or dict-valued knob still contributes to the digest instead of
    collapsing to an object repr (which carries a memory address and
    would change the digest on every run).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_jsonable(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


class LLMJudgeReceptors:
    """Annotation-only receptor slot for the judge model.

    ``ConfigComposer`` injects a wired dependency only when the owning
    class declares a slot — an ``__init__`` parameter or a class-level
    annotation. Keeping this on a plain (non-dataclass) mixin preserves
    conscribe's ``WiredField`` descriptor, so reading ``self.llm`` before
    injection raises a named ``AttributeError`` instead of returning
    ``None`` and failing three frames later.
    """

    llm: BaseChatModel


@dataclass
class UsageTally:
    """Per-evaluation token accounting.

    A local object handed down the call chain rather than state on the
    evaluator instance: an evaluator is constructed once per run and
    scores many traces, so accumulating on ``self`` would bill task 40
    for tasks 1-39.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0
    reported_calls: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        if usage is None:
            return
        self.reported_calls += 1
        self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        self.cached_tokens += int(getattr(usage, "prompt_cached_tokens", 0) or 0)

    def to_eval_usage(self) -> EvalUsage | None:
        if not self.reported_calls:
            return None
        return EvalUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cached_tokens=self.cached_tokens,
            non_cached_prompt=max(0, self.prompt_tokens - self.cached_tokens),
        )


@dataclass
class LLMJudgeEvaluator(LLMJudgeReceptors, EvaluatorBase):
    """Base for evaluators whose verdict comes from a judge model.

    The judge is an ordinary member of the ``llm`` registry, injected
    through ``__wiring__``. That is the whole point of putting evaluation
    behind a registrar: the three ported graders each carried a bespoke
    OpenAI client with its own retry policy, its own env-var names and no
    way to point at Anthropic or a local model. Here the judge is chosen
    in YAML from the same union as the agent's model, and can be a
    different provider entirely.
    """

    __abstract__ = True

    __wiring__: ClassVar[dict[str, Any]] = {
        "llm": "llm",  # Mode 1: any registered LLM may serve as judge
    }

    _prompt_cache: dict[str, tuple[str, PromptRef]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        """End this evaluator's judge execution, without inspecting other owners."""
        return await close_models([self.llm], timeout=timeout)

    # ── Judge invocation ────────────────────────────────────────────

    async def ask(
        self,
        *,
        text: str,
        system: str | None = None,
        images: Sequence[ContentPartImageParam] = (),
        tally: UsageTally | None = None,
    ) -> str:
        """One judge call. Returns the completion text.

        *system* is optional because not every ported grader used one —
        BrowseComp's is a single self-contained user turn, and injecting
        a system message it was never tuned with would change verdicts.
        """
        content: list[ContentPartTextParam | ContentPartImageParam] = [
            ContentPartTextParam(text=text)
        ]
        content.extend(images)
        messages: list[BaseMessage] = []
        if system is not None:
            messages.append(SystemMessage(content=system))
        messages.append(UserMessage(content=content))
        completion = await self.llm.ainvoke(messages)
        if tally is not None:
            tally.add(completion.usage)
        return str(completion.completion or "")

    # ── Prompt helpers ──────────────────────────────────────────────

    def prompt(self, name: str) -> tuple[str, PromptRef]:
        """Load-and-memoise one prompt for this evaluator instance."""
        cached = self._prompt_cache.get(name)
        if cached is None:
            cached = self.load_prompt(name)
            self._prompt_cache[name] = cached
        return cached

    def text_prompt(self, name: str) -> tuple[str, TextPrompt]:
        text, ref = self.prompt(name)
        return text, text_snapshot(text, ref)

    def render(self, name: str, params: dict[str, Any]) -> tuple[str, TemplatePrompt]:
        """Format a template and snapshot exactly what was substituted."""
        template, ref = self.prompt(name)
        return template.format(**params), template_snapshot(template, ref, params)
