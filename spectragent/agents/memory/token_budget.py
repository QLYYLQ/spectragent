"""TokenBudgetMemory — keeps recent messages within a token budget."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, ClassVar

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.agents.memory._tool_pairing import drop_broken_tool_groups
from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartRefusalParam,
    ContentPartTextParam,
    SystemMessage,
)


def _estimate_tokens_char_ratio(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def _estimate_tokens_word_ratio(text: str) -> int:
    """Rough token estimate: ~0.75 words per token for English text."""
    return max(1, int(len(text.split()) / 0.75))


#: Registry of text token estimators, selected by the ``token_estimator`` field.
TEXT_TOKEN_ESTIMATORS: Mapping[str, Callable[[str], int]] = MappingProxyType(
    {
        "char_ratio": _estimate_tokens_char_ratio,
        "word_ratio": _estimate_tokens_word_ratio,
    }
)

#: Default per-image token cost.  A 1024x768 screenshot costs ~1105 tokens on
#: OpenAI (85 base + 170 per 512x512 tile) and ~1049 on Anthropic (w*h/750).
#: Counting an image by the length of its base64 payload is wrong — that tracks
#: PNG compression, not vision-token cost.
DEFAULT_IMAGE_TOKENS = 1105


@dataclass
class TokenBudgetMemory(MemoryBase):
    """Keeps the most recent messages that fit within *max_tokens*.

    System messages are always included first (their tokens are
    subtracted from the budget).  Remaining budget is filled with
    the most recent non-system messages, newest first.

    Image parts are charged at *image_tokens* each.  Estimating from
    ``message.text`` alone drops images entirely, so a 400 KB base64
    screenshot would score as a single token and every budget check
    would pass.
    """

    __registry_key__ = "token_budget"
    supports_protocols: ClassVar[frozenset[MessageProtocol]] = frozenset(
        {"user_assistant", "tool_call"}
    )

    # ── config ─────────────────────────────────────────────────
    max_tokens: Annotated[int, PydanticField(description="Token budget for context messages")] = (
        8000
    )
    token_estimator: Annotated[
        str, PydanticField(description="Token estimation method: 'char_ratio' or 'word_ratio'")
    ] = "char_ratio"
    image_tokens: Annotated[
        int, PydanticField(description="Estimated token cost charged per image part")
    ] = DEFAULT_IMAGE_TOKENS

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    @property
    def memory_type(self) -> str:
        return "token_budget"

    def _text_estimator(self) -> Callable[[str], int]:
        try:
            return TEXT_TOKEN_ESTIMATORS[self.token_estimator]
        except KeyError:
            raise ValueError(
                f"Unknown token_estimator {self.token_estimator!r}. "
                f"Available: {sorted(TEXT_TOKEN_ESTIMATORS)}"
            ) from None

    def _estimate_tokens(self, message: BaseMessage) -> int:
        estimate_text = self._text_estimator()
        content = message.content

        # Tool calls are billed like any other output: name plus the serialized
        # argument JSON. Counting only `content` scored an assistant turn that
        # *only* calls tools at zero, so a function-calling conversation could
        # overflow the budget without ever spending it.
        total = 0
        for call in getattr(message, "tool_calls", ()):
            total += estimate_text(call.function.name) + estimate_text(call.function.arguments)

        if content is None:
            return total
        if isinstance(content, str):
            return total + estimate_text(content)

        for part in content:
            if isinstance(part, ContentPartTextParam):
                total += estimate_text(part.text)
            elif isinstance(part, ContentPartRefusalParam):
                total += estimate_text(part.refusal)
            elif isinstance(part, ContentPartImageParam):
                total += self.image_tokens
            else:  # pragma: no cover - defensive, unknown part kinds
                total += estimate_text(str(part))
        return total

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        self._messages.extend(messages)

    def get_messages(self) -> list[BaseMessage]:
        return list(self._messages)

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        budget = max_tokens if max_tokens is not None else self.max_tokens
        if not self._messages:
            return []

        # Always include system messages first
        system_msgs: list[BaseMessage] = []
        non_system: list[BaseMessage] = []
        for msg in self._messages:
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
            else:
                non_system.append(msg)

        used = sum(self._estimate_tokens(m) for m in system_msgs)

        # Fill remaining budget from most recent non-system messages
        selected: list[BaseMessage] = []
        for msg in reversed(non_system):
            cost = self._estimate_tokens(msg)
            if used + cost > budget:
                break
            selected.append(msg)
            used += cost

        selected.reverse()
        # The budget can run out mid tool-call group, which would hand the
        # provider an orphaned tool result. Repair the view, not the store.
        return system_msgs + drop_broken_tool_groups(selected)

    def clear(self) -> None:
        self._messages.clear()

    @property
    def message_count(self) -> int:
        return len(self._messages)
