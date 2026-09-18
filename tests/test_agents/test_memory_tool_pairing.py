"""Tool-call groups must survive memory eviction (F4.5 / §5.2.5).

``sliding_window`` and ``token_budget`` advertised ``tool_call`` in
``supports_protocols`` before a ``ToolMessage`` even existed, so memory
negotiation passed and the failure surfaced much later, when messages were
constructed. Now that the message type exists, the claim has to be earned.

A function-calling turn is a *group*: the assistant message announcing the
calls plus one result per call. Both OpenAI and Anthropic reject the halves
separately — an orphaned result is an API error, and so is an unanswered call.
An eviction policy that slices history at an arbitrary offset will cut a group
in half unless it is taught not to.

The repair is applied to the context view only. ``get_messages()`` keeps the
full history so the trace stays complete.
"""

from __future__ import annotations

from spectragent.agents.memory._tool_pairing import drop_broken_tool_groups
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.memory.token_budget import TokenBudgetMemory
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    Function,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def _call(id_: str, name: str = "f") -> ToolCall:
    return ToolCall(id=id_, function=Function(name=name, arguments="{}"))


def _assistant(*ids: str) -> AssistantMessage:
    return AssistantMessage(content=None, tool_calls=[_call(i) for i in ids])


def _result(id_: str) -> ToolMessage:
    return ToolMessage(tool_call_id=id_, content="ok")


def _ids(messages: list[BaseMessage]) -> list[str]:
    """A compact readable rendering of a message list."""
    out = []
    for m in messages:
        if isinstance(m, AssistantMessage) and m.tool_calls:
            out.append("A[" + ",".join(c.id for c in m.tool_calls) + "]")
        elif isinstance(m, ToolMessage):
            out.append(f"T[{m.tool_call_id}]")
        elif isinstance(m, SystemMessage):
            out.append("S")
        elif isinstance(m, UserMessage):
            out.append("U")
        else:
            out.append("A")
    return out


class TestDropBrokenToolGroups:
    def test_intact_pair_survives(self) -> None:
        messages = [UserMessage(content="q"), _assistant("c1"), _result("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == ["U", "A[c1]", "T[c1]"]

    def test_orphaned_result_is_dropped(self) -> None:
        """The assistant half was evicted; the result alone is an API error."""
        messages = [_result("c1"), UserMessage(content="q")]
        assert _ids(drop_broken_tool_groups(messages)) == ["U"]

    def test_unanswered_call_is_dropped(self) -> None:
        """OpenAI: 'must be followed by tool messages responding to each
        tool_call_id'."""
        messages = [UserMessage(content="q"), _assistant("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == ["U"]

    def test_partially_answered_parallel_call_drops_the_whole_group(self) -> None:
        """Dropping the assistant message takes its other call with it, which
        orphans that call's result — so the pass has to reach a fixpoint."""
        messages = [_assistant("c1", "c2"), _result("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == []

    def test_fully_answered_parallel_call_survives(self) -> None:
        messages = [_assistant("c1", "c2"), _result("c1"), _result("c2")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[c1,c2]", "T[c1]", "T[c2]"]

    def test_only_the_broken_group_is_dropped(self) -> None:
        messages = [
            _result("old"),  # orphan
            UserMessage(content="q"),
            _assistant("c1"),
            _result("c1"),  # intact
        ]
        assert _ids(drop_broken_tool_groups(messages)) == ["U", "A[c1]", "T[c1]"]

    def test_plain_conversation_is_untouched(self) -> None:
        messages = [
            SystemMessage(content="s"),
            UserMessage(content="q"),
            AssistantMessage(content="a"),
        ]
        assert drop_broken_tool_groups(messages) == messages

    def test_empty(self) -> None:
        assert drop_broken_tool_groups([]) == []


class TestRepeatedCallIds:
    """A call id is only unique per conversation by convention, and the two
    providers that send none had theirs synthesised from the tool name and the
    position in the reply — so an agent calling ``search`` every step produced
    ``search_0`` on every turn.

    Matching announced ids against answered ids as *sets* made that invisible:
    an unanswered call cancelled against an unrelated orphaned result, the
    difference came out empty, and the pass repaired nothing while returning a
    view the provider rejects. Groups are matched by position and multiplicity
    instead.
    """

    def test_orphan_survives_nothing_when_its_id_recurs_later(self) -> None:
        """Set difference saw c1 both announced and answered and kept the
        leading orphan — a `tool` message answering no preceding `tool_calls`,
        which is a 400 on both OpenAI and Anthropic."""
        messages = [_result("search_0"), _assistant("search_0"), _result("search_0")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[search_0]", "T[search_0]"]

    def test_unanswered_call_goes_even_when_its_id_occurred_earlier(self) -> None:
        """The mirror image: a completed group followed by an in-flight call
        that reuses the id."""
        messages = [_assistant("search_0"), _result("search_0"), _assistant("search_0")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[search_0]", "T[search_0]"]

    def test_a_whole_conversation_of_colliding_ids_is_repaired(self) -> None:
        messages = [
            _assistant("search_0"),
            _result("search_0"),
            _assistant("search_0"),
            _result("search_0"),
        ]
        # Both groups are intact here — nothing to repair, and nothing dropped.
        assert _ids(drop_broken_tool_groups(messages)) == [
            "A[search_0]",
            "T[search_0]",
            "A[search_0]",
            "T[search_0]",
        ]

    def test_sliding_window_view_does_not_begin_with_an_orphan(self) -> None:
        """The reported repro: the window cuts between a group's halves and the
        next group reuses the id."""
        memory = SlidingWindowMemory(window_size=3, keep_system=False)
        memory.add_messages(
            [
                _assistant("search_0"),
                _result("search_0"),
                _assistant("search_0"),
                _result("search_0"),
            ]
        )
        assert _ids(memory.get_context_messages()) == ["A[search_0]", "T[search_0]"]
        # The store is untouched, as always.
        assert memory.message_count == 4

    def test_duplicate_ids_in_one_turn_need_one_result_each(self) -> None:
        """Multiplicity, not membership: two live calls sharing an id are two
        calls, and one result answers only one of them."""
        messages = [_assistant("dup", "dup"), _result("dup")]
        assert _ids(drop_broken_tool_groups(messages)) == []

    def test_duplicate_ids_in_one_turn_survive_when_both_answered(self) -> None:
        messages = [_assistant("dup", "dup"), _result("dup"), _result("dup")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[dup,dup]", "T[dup]", "T[dup]"]


class TestGroupsAreMatchedPositionally:
    """The provider's rule is adjacency — the results must *follow* the
    assistant turn that asked for them, with nothing in between — so the repair
    checks adjacency rather than mere co-presence in the list."""

    def test_a_result_that_precedes_its_call_is_not_a_pair(self) -> None:
        messages = [_result("c1"), _assistant("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == []

    def test_a_result_separated_from_its_call_is_dropped(self) -> None:
        messages = [_assistant("c1"), UserMessage(content="q"), _result("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == ["U"]

    def test_an_extra_result_does_not_take_the_intact_group_with_it(self) -> None:
        """Only the orphan goes; the pair it was sitting next to survives."""
        messages = [_assistant("c1"), _result("c1"), _result("stray")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[c1]", "T[c1]"]

    def test_results_may_answer_a_parallel_call_out_of_order(self) -> None:
        """Order within the run is the model's business; OpenAI only requires
        that each announced id is answered."""
        messages = [_assistant("c1", "c2"), _result("c2"), _result("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A[c1,c2]", "T[c2]", "T[c1]"]

    def test_a_result_after_a_plain_assistant_turn_is_an_orphan(self) -> None:
        messages = [AssistantMessage(content="just text"), _result("c1")]
        assert _ids(drop_broken_tool_groups(messages)) == ["A"]


class TestSlidingWindowKeepsPairsIntact:
    def test_window_boundary_does_not_orphan_a_result(self) -> None:
        memory = SlidingWindowMemory(window_size=2)
        memory.add_messages([UserMessage(content="q"), _assistant("c1"), _result("c1")])

        # The raw window would be [A[c1], T[c1]] here, which is fine; shrink it
        # by one so the boundary lands *inside* the group.
        memory.window_size = 1
        assert _ids(memory.get_context_messages()) == []

    def test_intact_group_inside_the_window_survives(self) -> None:
        memory = SlidingWindowMemory(window_size=3)
        memory.add_messages([UserMessage(content="q"), _assistant("c1"), _result("c1")])
        assert _ids(memory.get_context_messages()) == ["U", "A[c1]", "T[c1]"]

    def test_store_is_not_mutated(self) -> None:
        """The trace needs the full history even when the view is repaired."""
        memory = SlidingWindowMemory(window_size=1)
        memory.add_messages([_assistant("c1"), _result("c1")])
        assert memory.get_context_messages() == []
        assert _ids(memory.get_messages()) == ["A[c1]", "T[c1]"]
        assert memory.message_count == 2

    def test_system_messages_still_lead(self) -> None:
        memory = SlidingWindowMemory(window_size=2)
        memory.add_messages([SystemMessage(content="s"), _assistant("c1"), _result("c1")])
        assert _ids(memory.get_context_messages()) == ["S", "A[c1]", "T[c1]"]

    def test_keep_system_false_path_is_repaired_too(self) -> None:
        memory = SlidingWindowMemory(window_size=1, keep_system=False)
        memory.add_messages([_assistant("c1"), _result("c1")])
        assert memory.get_context_messages() == []


class TestTokenBudgetKeepsPairsIntact:
    def test_budget_boundary_does_not_orphan_a_result(self) -> None:
        # The budget fills newest-first, so the cheap result is taken and the
        # expensive assistant turn that announced it is not — exactly the
        # orphan both OpenAI and Anthropic reject.
        expensive = AssistantMessage(content="x" * 4000, tool_calls=[_call("c1")])
        memory = TokenBudgetMemory(max_tokens=10)
        memory.add_messages([UserMessage(content="q"), expensive, _result("c1")])
        assert _ids(memory.get_context_messages()) == []

    def test_intact_group_within_budget_survives(self) -> None:
        memory = TokenBudgetMemory(max_tokens=10_000)
        memory.add_messages([UserMessage(content="q"), _assistant("c1"), _result("c1")])
        assert _ids(memory.get_context_messages()) == ["U", "A[c1]", "T[c1]"]

    def test_tool_message_is_charged_tokens(self) -> None:
        """ToolMessage has `.content`, so the estimator must see it — a result
        that scored zero would let the budget silently overflow."""
        memory = TokenBudgetMemory(max_tokens=10_000)
        assert memory._estimate_tokens(ToolMessage(tool_call_id="c", content="x" * 400)) > 1

    def test_tool_calls_are_charged_tokens(self) -> None:
        """An assistant turn that only calls tools used to cost zero, so a
        function-calling conversation could overflow without spending budget."""
        memory = TokenBudgetMemory(max_tokens=10_000)
        only_calls = AssistantMessage(
            content=None,
            tool_calls=[ToolCall(id="c1", function=Function(name="f", arguments="x" * 400))],
        )
        assert memory._estimate_tokens(only_calls) > 1


class TestSimpleMemoryKeepsPairsIntact:
    def test_eviction_does_not_orphan_a_result(self) -> None:
        memory = SimpleMemory(max_messages=1, keep_system=False)
        memory.add_messages([_assistant("c1"), _result("c1")])
        # max_messages=1 evicted the assistant half from the store.
        assert _ids(memory.get_context_messages()) == []

    def test_intact_group_survives(self) -> None:
        memory = SimpleMemory(max_messages=10)
        memory.add_messages([UserMessage(content="q"), _assistant("c1"), _result("c1")])
        assert _ids(memory.get_context_messages()) == ["U", "A[c1]", "T[c1]"]


class TestInFlightToolCall:
    """An assistant turn whose results have not arrived yet is incomplete.

    Dropping it from the *view* is correct — sending it would be an API error —
    and it is non-destructive: the message is still in the store and reappears
    as soon as its result is added. This is the one behaviour worth calling out,
    because it means a caller must append the result before re-reading context.
    """

    def test_call_is_hidden_until_its_result_arrives(self) -> None:
        memory = SlidingWindowMemory(window_size=20)
        memory.add_messages([UserMessage(content="q"), _assistant("c1")])
        assert _ids(memory.get_context_messages()) == ["U"]

        memory.add_message(_result("c1"))
        assert _ids(memory.get_context_messages()) == ["U", "A[c1]", "T[c1]"]
