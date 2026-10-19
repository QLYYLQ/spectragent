"""LLM provider registry using conscribe."""

import json
from collections.abc import Sequence
from typing import Any, Literal, TypeVar

from conscribe import create_registrar
from pydantic import BaseModel, ValidationError

from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.exceptions import (
    StructuredOutputValidationError,
    ToolsUnsupportedError,
)
from spectragent.core.llm.model import ModelBase
from spectragent.core.llm.model_families import ModelFamily, detect_model_family
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.tools import ToolDefinition

T = TypeVar("T", bound=BaseModel)

LLMRegistrar = create_registrar(
    "llm",
    BaseChatModel,
    discriminator_fields=["model_type", "provider"],
    key_separator=".",
)


class ChatModelBase(ModelBase, metaclass=LLMRegistrar.Meta):
    """Concrete base for all chat model implementations.

    Subclasses auto-register via conscribe's AutoRegistrar metaclass.
    Set __registry_key__ = "key" to specify the registration key.

    Provider authors share ``client_resources``, ``use_strict_schema`` and
    ``validate_structured`` through this base. They are extension APIs, not
    additional requirements on the consumer-facing ``BaseChatModel`` Protocol.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    @property
    def inference_kind(self) -> Literal["chat"]:
        return "chat"

    @property
    def model_family(self) -> ModelFamily:
        """Declared capabilities for this model (see ``core/llm/model_families``)."""
        return detect_model_family(str(self.model))

    # ------------------------------------------------------------------
    # Native function calling
    # ------------------------------------------------------------------
    @property
    def supports_tools(self) -> bool:
        """Whether this provider implements native function calling.

        Overridden to ``False`` by providers whose endpoint has no tool
        vocabulary at all. See :meth:`reject_unsupported_tools`.
        """
        return True

    def reject_unsupported_tools(
        self,
        tools: Sequence[ToolDefinition] | None,
        *,
        reason: str | None = None,
    ) -> None:
        """Raise if *tools* were requested but cannot be honoured.

        Called on every path that does not forward ``tools`` to the wire. The
        alternative — accepting the argument and ignoring it — produces a model
        that simply never calls a tool, which is indistinguishable from a bad
        prompt and costs a debugging session to attribute.
        """
        if not tools:
            return
        provider = getattr(type(self), "__name__", "this provider")
        detail = reason or f"{provider} does not implement native function calling."
        raise ToolsUnsupportedError(
            message=(
                f"tools= was passed to {provider} "
                f"(model={getattr(self, 'model', '?')!r}) but {detail} "
                f"Silently dropping them would leave the model unable to call "
                f"anything while the caller assumes it can."
            ),
            model=str(getattr(self, "model", "")),
        )

    # ------------------------------------------------------------------
    # Structured output
    # ------------------------------------------------------------------
    def use_strict_schema(self, output_format: type[BaseModel] | None = None) -> bool:
        """Whether to emit an OpenAI-style ``"strict": true`` schema.

        Provider extension API: pair the model's capabilities with the exact
        output schema before compiling the provider request.

        Two independent questions, and both must be answered yes:

        1. *Does this model support the strict dialect?* — the declarative
           capability table, not a substring test on the model name.
        2. *Can this particular schema be expressed in it?* — the compiled
           schema. An open map (``dict[str, Any]``, which is what an action
           field typed ``list[dict[str, Any]]`` becomes) produces an object
           node with no ``properties``, which strict rejects.
           :class:`SchemaOptimizer` deliberately leaves such nodes open, so
           asking for strict anyway ships a schema the API refuses.

        Called with no argument it answers (1) alone — the model's capability,
        independent of any payload.
        """
        if not self.model_family.supports_strict_mode:
            return False
        if output_format is None:
            return True
        return SchemaOptimizer.is_strict_compatible(
            SchemaOptimizer.create_optimized_json_schema(output_format, strict=True)
        )

    def validate_structured(
        self,
        output_format: type[T],
        payload: str | dict[str, Any],
        *,
        usage: Any = None,
        wire_text: str | None = None,
    ) -> T:
        """Validate a structured reply, or raise a *recoverable* error.

        Provider extension API: call this after any wire-format unwrapping,
        carrying the reply's usage and original text when available.

        Every provider's structured path ends in the same two lines —
        parse, then validate — and every one of them used to let the
        resulting ``ValidationError`` escape as a generic
        :class:`ModelProviderError`. The Loop cannot tell that apart from a
        dead endpoint, so it aborted the run at the LLM phase: a model that
        wrote one wrong parameter ended the episode, while the same mistake
        made in free text came back as a ``ParseError`` and was fed back
        for the model to fix.

        Raising :class:`StructuredOutputValidationError` instead keeps the
        model's own text, so the Loop can put it through the agent's parser
        and hand the model a located, single-line correction.

        **Every** registered provider routes its structured path through
        here, and a test walks ``providers/`` to keep it that way. That is
        not tidiness: whether a mistyped parameter costs a step or the
        whole run would otherwise depend on which provider key the YAML
        happens to name, which is the per-provider split the LLM layer
        exists to remove. The single exemption, ``_groq_parser``, is a
        salvage attempt inside an already-failed request and documents
        itself.

        Takes a ``str`` (validated as JSON, so malformed text is caught
        here too) or an already-parsed mapping.

        Args:
            wire_text: What the provider actually received, when *payload*
                is not that — a fence was unwrapped, prose was trimmed off,
                the text was parsed to a dict. ``raw_output`` still carries
                *payload*, because its job is to go back through the
                agent's parser and the unwrapped form is the one that
                parses. But the difference is a diagnosis: a reply that
                arrived fenced *and* does not fit the schema is the
                signature of a gateway that dropped ``response_format`` on
                the way to a vendor's native API, and with only the
                unwrapped body in the trace there is no sign of it. So the
                fact is stated in the message, which the trace's
                failure record keeps as ``CallFailure.detail``.
        """
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        # ``getattr`` rather than ``type(self).__name__``: conscribe's
        # registrar metaclass leaves the attribute unresolvable to a static
        # checker, and this is the same spelling ``reject_unsupported_tools``
        # above already uses for the same reason.
        who = str(getattr(self, "name", None) or getattr(type(self), "__name__", "provider"))
        unwrapped = (
            " The reply was unwrapped before validation (it arrived fenced or "
            "wrapped in prose); raw_output is the unwrapped body."
            if wire_text is not None and wire_text.strip() != raw
            else ""
        )
        try:
            if isinstance(payload, str):
                return output_format.model_validate_json(payload)
            return output_format.model_validate(payload)
        except ValidationError as exc:
            raise StructuredOutputValidationError(
                f"{who} returned a reply that does not match "
                f"{output_format.__name__}: {exc}{unwrapped}",
                raw_output=raw,
                model=str(getattr(self, "model", "")),
                usage=usage,
            ) from exc
        except ValueError as exc:
            # Malformed JSON on the text-shaped paths — equally the
            # model's to fix, and equally not a transport failure.
            raise StructuredOutputValidationError(
                f"{who} returned a reply that is not valid JSON for "
                f"{output_format.__name__}: {exc}{unwrapped}",
                raw_output=raw,
                model=str(getattr(self, "model", "")),
                usage=usage,
            ) from exc
