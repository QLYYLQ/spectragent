from collections.abc import Iterable
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from spectragent.core.llm.messages import ToolCall

T = TypeVar("T", bound=BaseModel | str)


def join_reasoning(pieces: Iterable[object]) -> str | None:
    """The reasoning a response carried, as one string, or ``None`` if it carried none.

    Every provider fills :attr:`ChatInvokeCompletion.thinking` and
    :attr:`ChatInvokeCompletion.redacted_thinking` through here, so one rule
    holds for all of them: the pieces stay in the order the response gave
    them, joined by a newline; a piece that is not a string, or holds nothing
    but whitespace, is dropped; nothing left is ``None``, never an empty
    string. A kept piece is not trimmed or rewritten.
    """
    texts = [piece for piece in pieces if isinstance(piece, str) and piece.strip()]
    return "\n".join(texts) if texts else None


class ModelInvokeUsage(BaseModel):
    """
    Provider-reported usage for a model invocation, including Decision calls.
    """

    prompt_tokens: int
    """The number of tokens in the prompt (this includes the cached tokens as well. When calculating the cost, subtract the cached tokens from the prompt tokens)"""

    prompt_cached_tokens: int | None
    """The number of cached tokens."""

    prompt_cache_creation_tokens: int | None
    """Anthropic only: The number of tokens used to create the cache."""

    prompt_image_tokens: int | None
    """Google only: The number of tokens in the image (prompt tokens is the text tokens + image tokens in that case)"""

    completion_tokens: int
    """The number of tokens in the completion."""

    completion_reasoning_tokens: int | None = None
    """How many of :attr:`completion_tokens` were the model's reasoning; ``None``: not reported.

    Read from what the response states (an OpenAI-compatible reply's
    ``completion_tokens_details.reasoning_tokens``, the Responses API's
    ``output_tokens_details.reasoning_tokens``, Gemini's
    ``thoughts_token_count``, OCI's ``completion_tokens_details``), never
    estimated. A provider whose usage has no such count leaves it ``None``,
    which is not zero: Anthropic bills thinking inside ``output_tokens`` and
    states no split. The default keeps call records written before the field
    existed readable.
    """

    total_tokens: int
    """The total number of tokens in the response."""


class ChatInvokeCompletion(BaseModel, Generic[T]):
    """
    Response from a chat model invocation.
    """

    completion: T
    """The completion of the response."""

    thinking: str | None = None
    """The model's reasoning, as the provider's response carried it beside the reply.

    Read from whatever the response already holds (an OpenAI-compatible
    message's ``reasoning_content`` or ``reasoning``, Anthropic's thinking
    blocks, Gemini's thought parts, ...), through :func:`join_reasoning`.
    ``None`` when the response carried none. No request is changed to obtain
    it, and it is a record, not context: the Loop and the agent read only
    :attr:`completion`, so it never reaches the next prompt
    (``docs/architecture_design.md`` §4.7).
    """

    redacted_thinking: str | None = None
    """Reasoning the provider returned only in opaque form, under the same rules.

    Anthropic's ``redacted_thinking`` blocks (their ``data``), Bedrock's
    redacted reasoning (base64), a Responses API item's ``encrypted_content``.
    """

    usage: ModelInvokeUsage | None
    """The usage of the response."""

    stop_reason: str | None = None
    """The reason the model stopped generating. Common values: 'end_turn', 'max_tokens', 'stop_sequence'."""

    tool_calls: list[ToolCall] = Field(default_factory=list)
    """Tool calls the model emitted, when ``tools=`` was passed to ``ainvoke``.

    Empty on every text/structured-output path, so callers that never advertise
    tools are unaffected. A caller drives the next turn by echoing these back as
    an :class:`AssistantMessage` and appending one :class:`ToolMessage` per call
    — see ``ToolMessage`` for why the pair must stay together.
    """
