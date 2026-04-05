"""LLM provider registry using conscribe."""

from conscribe import create_registrar

from spectragent.core.llm.base import BaseChatModel

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

    @property
    def model_name(self) -> str:
        """Legacy support — returns self.model."""
        return self.model
