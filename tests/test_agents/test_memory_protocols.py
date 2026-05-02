"""Tests for Memory message protocol declarations (M17)."""

from __future__ import annotations

from spectragent.agents.memory import ALL_PROTOCOLS
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.memory.summary import SummaryMemory
from spectragent.agents.memory.token_budget import TokenBudgetMemory


class TestAllProtocols:
    def test_all_protocols_contents(self) -> None:
        expected = frozenset({"user_assistant", "tool_call", "planning", "code_execution"})
        assert expected == ALL_PROTOCOLS


class TestMemorySupportsProtocols:
    def test_simple_supports(self) -> None:
        assert SimpleMemory.supports_protocols == frozenset({"user_assistant"})

    def test_sliding_window_supports(self) -> None:
        assert SlidingWindowMemory.supports_protocols == frozenset({"user_assistant", "tool_call"})

    def test_summary_supports(self) -> None:
        assert SummaryMemory.supports_protocols == frozenset({"user_assistant"})

    def test_token_budget_supports(self) -> None:
        assert TokenBudgetMemory.supports_protocols == frozenset({"user_assistant", "tool_call"})
