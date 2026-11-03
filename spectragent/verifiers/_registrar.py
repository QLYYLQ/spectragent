"""The verifier registry (``docs/architecture_design.md`` §4.10.1 "判分器有两条轴").

A verifier's kind is a registry key, ``verifier_id``: today's trace-reading
judges (``exact_match`` here; each benchmark's own judge in that benchmark's
package, such as ``online_mind2web``'s WebJudge). The registry is inline-only:
a verifier is written in a benchmark block's ``verifiers`` list and nowhere
else, and a benchmark's ``__wiring__`` names the kinds it takes.

The same three pieces every other layer uses: a Protocol
(:mod:`spectragent.verifiers.base`), a registrar keyed on ``verifier_id``, and
a concrete base that subclasses inherit from and conscribe auto-registers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

from conscribe import create_registrar
from pydantic import JsonValue

from spectragent.core.llm.base import BaseChatModel, require_inference_protocol
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic, close_models
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.prompts import template_snapshot, text_snapshot
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.prompt import PromptRef, TemplatePrompt, TextPrompt
from spectragent.schemas.verdict import JudgeUsage, Judgment, VerifierIdentity
from spectragent.verifiers.base import Verifier, VerifierNeed
from spectragent.verifiers.prompts import load_prompt, verifier_prompt_dir

VerifierRegistrar = create_registrar(
    "verifier",
    Verifier,
    discriminator_field="verifier_id",
    # Written only in a benchmark block's ``verifiers`` (§4.10.1).
    inline_only=True,
)


@dataclass
class VerifierBase(metaclass=VerifierRegistrar.Meta):
    """Concrete base for every verifier.

    Subclasses set ``__registry_key__``, declare what they need
    (``__needs__``) and implement :meth:`verify`. A benchmark's own verifier
    registers under the benchmark's key (``online_mind2web``), and its prompts
    are ``prompts/verifiers/<that key>/``.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    #: What it needs to judge a run (``VerifierNeed``); where it is attached
    #: follows. The default reads the trace and the task's reference row.
    __needs__: ClassVar[frozenset[VerifierNeed]] = frozenset({"trace", "reference"})

    # ── Protocol implementation ─────────────────────────────────────

    @property
    def verifier_id(self) -> str:
        """The conscribe registry key."""
        return self.__registry_key__

    @property
    def needs(self) -> frozenset[VerifierNeed]:
        return type(self).__needs__

    def identity(self) -> VerifierIdentity:
        """Its kind, judge model and settings: what every verdict it gives records."""
        return VerifierIdentity(
            verifier_id=self.verifier_id,
            model=self._judge_id(),
            config_signature=self.config_signature(),
        )

    async def verify(self, trace: RunTrace, reference: EvalReference | None) -> Judgment:
        raise NotImplementedError(
            f"Verifier {self.verifier_id!r} does not implement verify(trace, reference)."
        )

    # ── Prompt access ───────────────────────────────────────────────

    @property
    def prompt_dir(self):
        """This verifier's prompt directory — derived, never mapped."""
        return verifier_prompt_dir(self.__registry_key__)

    def load_prompt(self, name: str) -> tuple[str, PromptRef]:
        return load_prompt(self.__registry_key__, name)

    # ── Configuration identity ──────────────────────────────────────

    def config_signature(self) -> dict[str, JsonValue]:
        """Every setting of this verifier that can change a verdict.

        ``verifier_id`` is a *name*, not an identity: ``exact_match`` with
        ``allow_substring`` on and off are two different judges wearing one
        label, and an LLM judge is a different judge the moment its model
        changes. Recording the settings in every verdict is what lets two
        verdicts be compared — and the slug derived from them is what stops
        the second one from replacing the first.
        """
        signature: dict[str, JsonValue] = {}
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
                "provider": str(provider) if provider is not None else None,
                # A free-form endpoint can carry credentials. Compare routes without
                # recording URL credentials, path tokens or query strings.
                "endpoint_sha256": (
                    hashlib.sha256(str(endpoint).encode()).hexdigest() if endpoint else None
                ),
            }
            # Two judges on one model that sample differently are two judges.
            signature["judge_settings"] = {
                name: _jsonable(getattr(instance, name))
                for name in JUDGE_SETTINGS
                if hasattr(instance, name)
            }
        return signature

    def _judge_id(self) -> str | None:
        """Identity of the injected judge model, when there is one.

        ``getattr`` with a default is deliberate: before composition
        injects it, conscribe's ``WiredField`` descriptor raises
        ``AttributeError`` — a verifier asked for its identity too early has
        no judge, rather than a judge called ``None``.
        """
        judge = getattr(self, "llm", None)
        if judge is None:
            return None
        for attr in ("model", "name"):
            value = getattr(judge, attr, None)
            if isinstance(value, str) and value:
                return value
        return type(judge).__name__


#: The judge model's settings that can change its reply, as the providers name
#: them: sampling, length and reasoning. An LLM judge's identity records each
#: one its model has. Named, never collected: a model's other fields hold
#: credentials (``api_key``, ``aws_secret_key``, ``auth_token`` …), its
#: endpoint (recorded hashed) and transport knobs that change no reply.
JUDGE_SETTINGS = (
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "stop_sequences",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "reasoning_effort",
    "thinking_budget",
    "thinking_level",
)


def _jsonable(value: object) -> JsonValue:
    """Coerce a config value into something JSON can hold.

    Config fields are scalars in practice; the recursive cases exist so a
    list- or dict-valued knob still contributes to the signature instead of
    collapsing to an object repr (which carries a memory address and would
    change the slug on every run).
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
    """Per-judgment token accounting.

    A local object handed down the call chain rather than state on the
    verifier instance: a verifier is constructed once per pass and judges
    many traces, so accumulating on ``self`` would bill task 40 for tasks
    1-39.
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

    def to_usage(self) -> JudgeUsage | None:
        """What the judge spent; ``None`` when it was never called."""
        if not self.calls:
            return None
        return JudgeUsage(
            calls=self.calls,
            reported_calls=self.reported_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cached_tokens=self.cached_tokens,
        )


@dataclass
class LLMJudgeVerifier(LLMJudgeReceptors, VerifierBase):
    """Base for verifiers whose verdict comes from a judge model.

    The judge is an ordinary member of the ``llm`` registry, written in the
    verifier's block in the benchmark YAML. That is the whole point of judging
    through a registrar: the three ported graders each carried a bespoke
    OpenAI client with its own retry policy, its own env-var names and no way
    to point at Anthropic or a local model. Here the judge is chosen in YAML
    from the same union as the agent's model, and can be a different provider
    entirely.
    """

    __abstract__ = True
    __needs__: ClassVar[frozenset[VerifierNeed]] = frozenset({"trace", "reference", "model"})

    __wiring__: ClassVar[dict[str, Any]] = {
        "llm": "llm",  # Mode 1: any registered LLM may serve as judge
    }

    _prompt_cache: dict[str, tuple[str, PromptRef]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        """End this verifier's judge execution, without inspecting other owners."""
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
        require_inference_protocol(self.llm, "chat", consumer=f"{self.verifier_id}.llm")
        completion = await self.llm.ainvoke(messages)
        if tally is not None:
            tally.add(completion.usage)
        return str(completion.completion or "")

    # ── Prompt helpers ──────────────────────────────────────────────

    def prompt(self, name: str) -> tuple[str, PromptRef]:
        """Load-and-memoise one prompt for this verifier instance."""
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
