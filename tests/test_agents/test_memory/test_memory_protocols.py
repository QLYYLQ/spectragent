"""Tests for Memory message protocol declarations (M17)."""

from __future__ import annotations

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.memory.token_budget import TokenBudgetMemory


class TestMemorySupportsProtocols:
    """``tool_call`` is now earned rather than asserted.

    ``sliding_window`` and ``token_budget`` advertised it before a
    ``ToolMessage`` even existed, so memory negotiation passed and the failure
    surfaced later, at message construction. They now keep tool-call groups
    intact across eviction (``_tool_pairing.drop_broken_tool_groups``), which is
    what the claim actually requires.

    ``SimpleMemory`` still claims only ``user_assistant``. It was already honest
    — it evicts on ``max_messages`` and would split a group the same way — and
    negotiation needs a strategy that genuinely lacks the protocol for the
    incompatibility path to mean anything.
    """

    def test_simple_supports(self) -> None:
        assert SimpleMemory.supports_protocols == frozenset({"user_assistant"})

    def test_sliding_window_supports(self) -> None:
        assert SlidingWindowMemory.supports_protocols == frozenset({"user_assistant", "tool_call"})

    def test_token_budget_supports(self) -> None:
        assert TokenBudgetMemory.supports_protocols == frozenset({"user_assistant", "tool_call"})
