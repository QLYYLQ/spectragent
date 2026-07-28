"""Keep tool-call / tool-result pairs intact across memory eviction.

A function-calling turn is not one message, it is a group: the
:class:`~spectragent.core.llm.messages.AssistantMessage` that requests the calls
plus one :class:`~spectragent.core.llm.messages.ToolMessage` answering each of
them. Both OpenAI and Anthropic reject the two halves separately:

* a ``tool_result`` / ``tool`` message whose call is missing is an orphan;
* an assistant message with ``tool_calls`` that are never answered is equally
  invalid ("must be followed by tool messages responding to each tool_call_id").

So eviction that slices the history at an arbitrary offset — which is exactly
what a sliding window or a token budget does — produces a request the provider
refuses, and it fails at the API rather than anywhere near memory.

The repair is applied to the *context view* (``get_context_messages``), never to
the store. ``get_messages()`` keeps the full history for the trace; only what is
about to be handed to a provider is required to be well-formed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from spectragent.core.llm.messages import AssistantMessage, BaseMessage, ToolMessage


def drop_broken_tool_groups(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Return *messages* with every incomplete tool-call group removed.

    A group is an assistant message announcing calls plus the *contiguous run*
    of tool messages that answers it — that adjacency is the provider's own
    requirement ("must be followed by tool messages responding to each
    tool_call_id"), so the repair matches on it rather than on a global set of
    ids. The run is matched against the announced calls as a multiset: each
    result consumes one outstanding call of the same id. A group survives only
    when every announced call is consumed; otherwise the assistant message and
    its whole run are dropped together. Results in the run that answer no
    outstanding call are orphans and are dropped on their own, as is any tool
    message not preceded by an assistant turn that called something.

    Matching positionally rather than by set membership is what makes the pass
    correct when a call id repeats across turns — ids synthesised for providers
    that send none used to collide, and a set difference then cancelled an
    unanswered call against an unrelated orphaned result and repaired nothing.
    Multiplicity matters for the same reason: two live calls with one id need
    two results, not merely one matching id somewhere in the list.

    One forward pass suffices. Dropping a group removes the assistant message
    and every result bound to it at the same time, so it can never orphan a
    message that an earlier or later group depends on.

    A trailing assistant message whose results have not been produced yet is
    incomplete by this definition and is dropped from the view. That is correct:
    sending it would be an API error, and the message is still in the store, so
    it reappears in the view as soon as its results are added.
    """
    kept: list[BaseMessage] = []
    index = 0
    total = len(messages)

    while index < total:
        message = messages[index]

        if not (isinstance(message, AssistantMessage) and message.tool_calls):
            # A tool message here answers no preceding call — an orphan.
            if not isinstance(message, ToolMessage):
                kept.append(message)
            index += 1
            continue

        run: list[ToolMessage] = []
        run_end = index + 1
        while run_end < total:
            candidate = messages[run_end]
            if not isinstance(candidate, ToolMessage):
                break
            run.append(candidate)
            run_end += 1

        outstanding = Counter(call.id for call in message.tool_calls)
        matched: list[BaseMessage] = []
        for result in run:
            if outstanding[result.tool_call_id] > 0:
                outstanding[result.tool_call_id] -= 1
                matched.append(result)
            # else: answers no outstanding call — orphan, dropped.

        if not any(outstanding.values()):
            kept.append(message)
            kept.extend(matched)
        # else: an announced call went unanswered, so the assistant message and
        # everything in its run go together.

        index = run_end

    return kept


__all__ = ["drop_broken_tool_groups"]
