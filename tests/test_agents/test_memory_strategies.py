"""Memory strategy behaviour regressions (§6.3.1, §6.3.3)."""

from __future__ import annotations

import base64

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.token_budget import TokenBudgetMemory
from spectragent.core.llm.messages import (
    AssistantMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)


def _roles(messages) -> list[str]:
    return [m.role for m in messages]


class TestSimpleMemoryKeepsSystem:
    """§6.3.1 — SimpleMemory silently evicted the system prompt."""

    def test_system_prompt_survives_eviction(self) -> None:
        """The handoff's exact repro: max_messages=3, four messages in."""
        mem = SimpleMemory(max_messages=3)
        mem.add_messages(
            [
                SystemMessage(content="SYS"),
                UserMessage(content="u1"),
                AssistantMessage(content="a1"),
                UserMessage(content="u2"),
            ]
        )
        assert _roles(mem.get_messages()) == ["system", "assistant", "user"]
        assert mem.get_messages()[0].content == "SYS"

    def test_add_message_one_at_a_time_also_keeps_system(self) -> None:
        mem = SimpleMemory(max_messages=3)
        for msg in [
            SystemMessage(content="SYS"),
            UserMessage(content="u1"),
            AssistantMessage(content="a1"),
            UserMessage(content="u2"),
            AssistantMessage(content="a2"),
        ]:
            mem.add_message(msg)
        stored = mem.get_messages()
        assert stored[0].content == "SYS"
        assert len(stored) == 3
        assert _roles(stored) == ["system", "user", "assistant"]

    def test_original_order_is_preserved(self) -> None:
        mem = SimpleMemory(max_messages=4)
        mem.add_messages(
            [
                UserMessage(content="u1"),
                SystemMessage(content="SYS"),
                UserMessage(content="u2"),
                UserMessage(content="u3"),
                UserMessage(content="u4"),
            ]
        )
        # Oldest non-system dropped; the system message stays where it was.
        assert [m.content for m in mem.get_messages()] == ["SYS", "u2", "u3", "u4"]

    def test_keep_system_false_restores_plain_truncation(self) -> None:
        mem = SimpleMemory(max_messages=3, keep_system=False)
        mem.add_messages(
            [
                SystemMessage(content="SYS"),
                UserMessage(content="u1"),
                AssistantMessage(content="a1"),
                UserMessage(content="u2"),
            ]
        )
        assert _roles(mem.get_messages()) == ["user", "assistant", "user"]

    def test_all_system_messages_kept_when_over_budget(self) -> None:
        mem = SimpleMemory(max_messages=2)
        mem.add_messages(
            [
                SystemMessage(content="SYS A"),
                SystemMessage(content="SYS B"),
                SystemMessage(content="SYS C"),
                UserMessage(content="u1"),
            ]
        )
        assert [m.content for m in mem.get_messages()] == ["SYS A", "SYS B", "SYS C"]

    def test_under_cap_is_untouched(self) -> None:
        mem = SimpleMemory(max_messages=10)
        mem.add_messages([SystemMessage(content="SYS"), UserMessage(content="u1")])
        assert mem.message_count == 2

    def test_context_messages_include_system(self) -> None:
        mem = SimpleMemory(max_messages=2)
        mem.add_messages(
            [SystemMessage(content="SYS"), UserMessage(content="u1"), UserMessage(content="u2")]
        )
        assert any(m.role == "system" for m in mem.get_context_messages())


_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"\x00" * 300_000).decode()


class TestTokenBudgetEstimation:
    """§6.3.3 — token_estimator was never read and images scored as 1 token."""

    def _image_message(self) -> UserMessage:
        return UserMessage(
            content=[
                ContentPartTextParam(text="look at this"),
                ContentPartImageParam(
                    image_url=ImageURL(url=_PNG_DATA_URL, media_type="image/png")
                ),
            ]
        )

    def test_image_is_not_one_token(self) -> None:
        mem = TokenBudgetMemory()
        cost = mem._estimate_tokens(self._image_message())
        assert cost >= mem.image_tokens
        assert cost > 1000

    def test_image_message_is_excluded_when_over_budget(self) -> None:
        """The whole point: a screenshot must be able to blow the budget."""
        mem = TokenBudgetMemory(max_tokens=100)
        mem.add_messages([SystemMessage(content="SYS"), self._image_message()])
        selected = mem.get_context_messages()
        assert _roles(selected) == ["system"]

    def test_image_message_fits_under_a_large_budget(self) -> None:
        mem = TokenBudgetMemory(max_tokens=50_000)
        mem.add_messages([SystemMessage(content="SYS"), self._image_message()])
        assert _roles(mem.get_context_messages()) == ["system", "user"]

    def test_image_tokens_is_configurable(self) -> None:
        cheap = TokenBudgetMemory(image_tokens=10)
        assert cheap._estimate_tokens(self._image_message()) < 100

    def test_token_estimator_is_actually_read(self) -> None:
        text = "the quick brown fox jumps over the lazy dog " * 20
        msg = UserMessage(content=text)
        by_chars = TokenBudgetMemory(token_estimator="char_ratio")._estimate_tokens(msg)
        by_words = TokenBudgetMemory(token_estimator="word_ratio")._estimate_tokens(msg)
        assert by_chars != by_words

    def test_unknown_estimator_raises_instead_of_being_ignored(self) -> None:
        mem = TokenBudgetMemory(token_estimator="tiktoken")
        with pytest.raises(ValueError, match="Unknown token_estimator"):
            mem._estimate_tokens(UserMessage(content="hi"))

    def test_text_only_budgeting_still_works(self) -> None:
        # char_ratio: 4 chars == 1 token. system=1, "A"*40=10, "B"*8=2.
        mem = TokenBudgetMemory(max_tokens=5)
        mem.add_messages(
            [
                SystemMessage(content="S" * 4),
                UserMessage(content="A" * 40),
                UserMessage(content="B" * 8),
            ]
        )
        selected = mem.get_context_messages()
        # Newest first within the remaining budget; the 10-token message does not fit.
        assert [m.content for m in selected] == ["S" * 4, "B" * 8]
