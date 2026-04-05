"""Message serializers for each protocol format."""

from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer
from spectragent.core.llm.serializers.bedrock_format import AWSBedrockMessageSerializer
from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer
from spectragent.core.llm.serializers.oci_format import OCIRawMessageSerializer
from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.serializers.openai_responses import ResponsesAPIMessageSerializer

# Aliases — these providers use the same OpenAI message format
DeepSeekMessageSerializer = OpenAIMessageSerializer
CerebrasMessageSerializer = OpenAIMessageSerializer
OpenRouterMessageSerializer = OpenAIMessageSerializer
VercelMessageSerializer = OpenAIMessageSerializer
GroqMessageSerializer = OpenAIMessageSerializer

__all__ = [
    "OpenAIMessageSerializer",
    "ResponsesAPIMessageSerializer",
    "AnthropicMessageSerializer",
    "GoogleMessageSerializer",
    "AWSBedrockMessageSerializer",
    "OCIRawMessageSerializer",
    "OllamaMessageSerializer",
    "DeepSeekMessageSerializer",
    "CerebrasMessageSerializer",
    "OpenRouterMessageSerializer",
    "VercelMessageSerializer",
    "GroqMessageSerializer",
]
