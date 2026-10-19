"""Explicit prompt contributions through the Agent's public prompt boundary."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, get_type_hints

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.llm.base import BaseChatModel, require_inference_protocol
from spectragent.core.trace.redaction import trace_safe
from spectragent.schemas.prompt import PromptContribution, PromptRequest, ReactPromptContext

if TYPE_CHECKING:
    from spectragent.core.llm.call_context import RunCallContext


def _resolve_function(reference: str) -> tuple[Callable[..., Any], str]:
    """Resolve a module-level function with one explicit request parameter."""
    module_name, separator, function_name = reference.partition(":")
    if (
        not separator
        or not module_name
        or any(not part.isidentifier() for part in module_name.split("."))
        or not function_name.isidentifier()
    ):
        raise ValueError(
            f"Prompt contribution function {reference!r} needs a stable "
            "'package.module:function' reference"
        )
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"Cannot resolve prompt contribution function {reference!r}") from exc
    if (
        not inspect.isfunction(function)
        or function.__module__ != module_name
        or function.__qualname__ != function_name
    ):
        raise ValueError(
            f"Prompt contribution {reference!r} must name its defining module's "
            "top-level Python function; aliases, lambdas and nested callables are unsupported"
        )
    parameters = inspect.signature(function, follow_wrapped=False).parameters
    request = parameters.get("request")
    if (
        len(parameters) != 1
        or request is None
        or request.kind is not inspect.Parameter.KEYWORD_ONLY
        or request.default is not inspect.Parameter.empty
    ):
        raise ValueError(
            f"Prompt contribution {reference!r} must declare exactly '(*, request: PromptRequest)'"
        )
    try:
        annotation = get_type_hints(function).get("request")
    except (NameError, TypeError) as exc:
        raise ValueError(f"Prompt contribution {reference!r} has unresolved annotations") from exc
    if annotation is not PromptRequest:
        raise ValueError(
            f"Prompt contribution {reference!r} must annotate request as PromptRequest"
        )
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise ValueError(f"Prompt contribution {reference!r} cannot be a generator")
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError) as exc:
        raise ValueError(
            f"Prompt contribution {reference!r} needs readable Python source for its code digest"
        ) from exc
    return function, sha256(source.encode("utf-8")).hexdigest()


@dataclass(eq=False)
class PromptContributionHook(HookBase):
    """Generate one contribution per eligible step, before the Agent renders it.

    The context owns the generation cache. This component declares its explicitly
    configured helpers; Runtime supplies woven invocations and closes the models
    at the execution boundary, including failures of earlier lifecycle hooks.
    """

    __registry_key__ = "prompt_contribution"
    __wiring__: ClassVar[dict[str, Any]] = {"helper_llms": "llm"}
    __intercepts__ = [Intercept("build_react_prompt", role="agent", advice="around")]

    function: Annotated[
        str,
        trace_safe(
            "importable Python function identity, not a credential",
            description="Stable package.module:function implementing (*, request: PromptRequest).",
        ),
    ]
    parameters: Annotated[
        dict[str, Any], Field(description="Named data supplied in the contribution request.")
    ] = field(default_factory=dict)
    scope: Annotated[
        Literal["first_policy_step", "each_policy_step"],
        Field(description="Generate at the first branch policy step, or at every policy step."),
    ] = "first_policy_step"
    helper_llms: Annotated[
        list[BaseChatModel],
        Field(max_length=1, description="Optional independently configured helper model."),
    ] = field(default_factory=list)

    _function: Callable[..., Any] = field(init=False, repr=False)
    _code_digest: str = field(init=False, repr=False)
    _models: tuple[BaseChatModel, ...] = field(default=(), init=False, repr=False)
    _call_context: RunCallContext | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.scope not in ("first_policy_step", "each_policy_step"):
            raise ValueError(f"Unsupported prompt contribution scope {self.scope!r}")
        if len(self.helper_llms) > 1:
            raise ValueError("A prompt contribution accepts at most one helper model")
        self.helper_llms = list(self.helper_llms)
        for model in self.helper_llms:
            require_inference_protocol(model, "chat", consumer="prompt_contribution.helper_llms")
        self.parameters = deepcopy(self.parameters)
        self._function, self._code_digest = _resolve_function(self.function)

    @property
    def code_digest(self) -> str:
        """SHA-256 of the resolved function's Python source, independent of its path."""
        return self._code_digest

    def bind_prompt_models(
        self, models: Sequence[BaseChatModel], call_context: RunCallContext
    ) -> None:
        """Bind woven invocation surfaces without replacing the owned raw models."""
        if len(models) != len(self.helper_llms):
            raise ValueError("Prompt helper bindings must cover exactly the configured models")
        self._models = tuple(models)
        self._call_context = call_context

    async def on_run_start(self, *, call_context: RunCallContext, **kwargs: Any) -> None:
        self._call_context = call_context

    async def around_build_react_prompt(
        self, *, proceed: Any, args: tuple[Any, ...], kwargs: dict[str, Any], **ctx: Any
    ) -> Any:
        context = kwargs.get("context")
        if not isinstance(context, ReactPromptContext):
            raise TypeError("Prompt contribution requires keyword-only context: ReactPromptContext")
        eligible = context.phase == "policy" and (
            self.scope == "each_policy_step" or (context.is_branch and context.policy_step == 0)
        )
        if not eligible or self in context.generated:
            return await proceed()
        if self._call_context is None or len(self._models) != len(self.helper_llms):
            raise RuntimeError(
                "Prompt contribution models and call context must be bound by Runtime"
            )
        observation = args[0] if args else kwargs["observation"]
        request = PromptRequest(
            observation=observation,
            context=context,
            parameters=deepcopy(self.parameters),
            helper=self._models[0] if self._models else None,
        )
        with self._call_context.scope(
            caller=f"prompt_contribution:{self.function}",
            purpose="prompt_helper",
            step=context.step,
        ):
            contribution = self._function(request=request)
            if inspect.isawaitable(contribution):
                contribution = await contribution
        if isinstance(contribution, str):
            contribution = PromptContribution(text=contribution)
        if not isinstance(contribution, PromptContribution):
            raise TypeError(
                f"Prompt contribution {self.function!r} returned {type(contribution).__name__}; "
                "expected str or PromptContribution"
            )
        context.contributions.append(
            contribution.model_copy(
                update={"source": self.function, "code_digest": self.code_digest}
            )
        )
        context.generated.add(self)
        return await proceed()
