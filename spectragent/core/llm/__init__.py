"""LLM abstraction layer — model_type + provider dual-layer design.

model_type = pure communication protocol (discriminator level 0)
provider   = nested sub-provider (discriminator level 1, hierarchical keys)
"""

from typing import TYPE_CHECKING

# Lightweight imports that are commonly used
from spectragent.core.llm._registrar import ChatModelBase, LLMRegistrar
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.lifecycle import (
    ClientCleanupDiagnostic,
    ModelClientLifecycle,
    close_models,
    model_session,
)
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.messages import (
    ContentPartImageParam as ContentImage,
)
from spectragent.core.llm.messages import (
    ContentPartRefusalParam as ContentRefusal,
)
from spectragent.core.llm.messages import (
    ContentPartTextParam as ContentText,
)

# Type stubs for lazy imports
if TYPE_CHECKING:
    from spectragent.core.llm.providers.anthropic import ChatAnthropic
    from spectragent.core.llm.providers.anthropic_aws import ChatAnthropicAWS
    from spectragent.core.llm.providers.anthropic_base import AnthropicCompatBase
    from spectragent.core.llm.providers.bedrock import ChatBedrock
    from spectragent.core.llm.providers.browser_use import ChatBrowserUse
    from spectragent.core.llm.providers.google import ChatGoogle
    from spectragent.core.llm.providers.mistral import ChatMistral
    from spectragent.core.llm.providers.oci import ChatOCI
    from spectragent.core.llm.providers.ollama import ChatOllama
    from spectragent.core.llm.providers.openai import ChatOpenAI
    from spectragent.core.llm.providers.openai_azure import ChatAzureOpenAI
    from spectragent.core.llm.providers.openai_base import OpenAICompatBase
    from spectragent.core.llm.providers.openai_cerebras import ChatCerebras
    from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
    from spectragent.core.llm.providers.openai_groq import ChatGroq
    from spectragent.core.llm.providers.openai_openrouter import ChatOpenRouter
    from spectragent.core.llm.providers.openai_vercel import ChatVercel

# Lazy imports mapping for heavy chat models
_LAZY_IMPORTS = {
    # --- Primary classes ---
    "ChatOpenAI": ("spectragent.core.llm.providers.openai", "ChatOpenAI"),
    "ChatAzureOpenAI": ("spectragent.core.llm.providers.openai_azure", "ChatAzureOpenAI"),
    "ChatGroq": ("spectragent.core.llm.providers.openai_groq", "ChatGroq"),
    "ChatDeepSeek": ("spectragent.core.llm.providers.openai_deepseek", "ChatDeepSeek"),
    "ChatCerebras": ("spectragent.core.llm.providers.openai_cerebras", "ChatCerebras"),
    "ChatOpenRouter": ("spectragent.core.llm.providers.openai_openrouter", "ChatOpenRouter"),
    "ChatVercel": ("spectragent.core.llm.providers.openai_vercel", "ChatVercel"),
    "OpenAICompatBase": ("spectragent.core.llm.providers.openai_base", "OpenAICompatBase"),
    "ChatAnthropic": ("spectragent.core.llm.providers.anthropic", "ChatAnthropic"),
    "ChatAnthropicAWS": ("spectragent.core.llm.providers.anthropic_aws", "ChatAnthropicAWS"),
    "AnthropicCompatBase": ("spectragent.core.llm.providers.anthropic_base", "AnthropicCompatBase"),
    "ChatGoogle": ("spectragent.core.llm.providers.google", "ChatGoogle"),
    "ChatBedrock": ("spectragent.core.llm.providers.bedrock", "ChatBedrock"),
    "ChatOllama": ("spectragent.core.llm.providers.ollama", "ChatOllama"),
    "ChatMistral": ("spectragent.core.llm.providers.mistral", "ChatMistral"),
    "ChatOCI": ("spectragent.core.llm.providers.oci", "ChatOCI"),
    "ChatBrowserUse": ("spectragent.core.llm.providers.browser_use", "ChatBrowserUse"),
}


def __getattr__(name: str):
    """Lazy import mechanism for heavy chat model imports."""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        try:
            from importlib import import_module

            module = import_module(module_path)
            attr = getattr(module, attr_name)
            return attr
        except ImportError as e:
            raise ImportError(f"Failed to import {name} from {module_path}: {e}") from e

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    # Message types
    "BaseMessage",
    "UserMessage",
    "SystemMessage",
    "AssistantMessage",
    "ContentText",
    "ContentRefusal",
    "ContentImage",
    # Registry
    "LLMRegistrar",
    "ChatModelBase",
    # Execution lifecycle
    "ClientCleanupDiagnostic",
    "ModelClientLifecycle",
    "close_models",
    "model_session",
    # Base classes
    "BaseChatModel",
    "OpenAICompatBase",
    "AnthropicCompatBase",
    # OpenAI-compatible providers
    "ChatOpenAI",
    "ChatAzureOpenAI",
    "ChatGroq",
    "ChatDeepSeek",
    "ChatCerebras",
    "ChatOpenRouter",
    "ChatVercel",
    # Anthropic providers
    "ChatAnthropic",
    "ChatAnthropicAWS",
    # Other providers
    "ChatGoogle",
    "ChatBedrock",
    "ChatOllama",
    "ChatMistral",
    "ChatOCI",
    "ChatBrowserUse",
]
