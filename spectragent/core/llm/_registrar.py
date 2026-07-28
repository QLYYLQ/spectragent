"""LLM provider registry using conscribe."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from conscribe import create_registrar
from pydantic import BaseModel

from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.exceptions import ToolsUnsupportedError
from spectragent.core.llm.model_families import ModelFamily, detect_model_family
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.tools import ToolDefinition

LLMRegistrar = create_registrar(
    "llm",
    BaseChatModel,
    discriminator_fields=["model_type", "provider"],
    key_separator=".",
)


class ChatModelBase(metaclass=LLMRegistrar.Meta):
    """Concrete base for all chat model implementations.

    Subclasses auto-register via conscribe's AutoRegistrar metaclass.
    Set __registry_key__ = "key" to specify the registration key.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    _verified_api_keys: bool = False

    if TYPE_CHECKING:
        # Every concrete provider declares ``model`` (``ChatOCI`` as a property
        # over ``model_id``), but the base cannot: an annotation here would be
        # visible to conscribe's field extraction. Declaring it for the type
        # checker only keeps the shared helpers below statically checkable
        # without touching what conscribe sees at runtime.
        model: str

    @property
    def model_name(self) -> str:
        """Legacy support — returns self.model."""
        return self.model

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
    def _use_strict_schema(self, output_format: type[BaseModel] | None = None) -> bool:
        """Whether to emit an OpenAI-style ``"strict": true`` schema.

        Two independent questions, and both must be answered yes:

        1. *Does this model support the strict dialect?* — the declarative
           capability table, not a substring test on the model name.
        2. *Can this particular schema be expressed in it?* — the compiled
           schema. Open maps (``dict[str, Any]``, e.g. ``BrowserUseOutput.action``)
           produce an object node with no ``properties``, which strict rejects.
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
