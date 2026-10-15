"""Recorded contributions and helper calls through complete source and branch runs."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast

import pytest

from spectragent.core.hooks.prompt_contribution import PromptContributionHook
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops import BranchLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.usage import recorded_calls, recorded_usage
from spectragent.schemas.prompt import PromptContribution, PromptRequest
from tests._helpers.branch_fakes import BranchFakeAgent, ScriptedLLM, record_source, run_branch


async def page_advice(*, request: PromptRequest) -> PromptContribution:
    assert request.helper is not None
    response = await request.helper.ainvoke(
        [UserMessage(content=f"Advise about policy step {request.context.policy_step}")]
    )
    return PromptContribution(text=response.completion, placement="system")


def _hook(
    replies: list[Any],
    *,
    scope: Literal["first_policy_step", "each_policy_step"] = "first_policy_step",
) -> tuple[PromptContributionHook, ScriptedLLM]:
    helper = ScriptedLLM(replies)
    return (
        PromptContributionHook(
            function=f"{__name__}:page_advice",
            scope=scope,
            helper_llms=[cast(BaseChatModel, helper)],
        ),
        helper,
    )


async def test_source_history_reuses_recorded_contributions_and_ignores_helper_replies(tmp_path):
    source_hook, helper = _hook(
        ["First external instruction.", "Second external instruction.", "Checkpoint instruction."],
        scope="each_policy_step",
    )
    source = await record_source(
        tmp_path,
        ["click 100", "click 101", "done source"],
        extra_hooks=[source_hook],
    )
    source_trace = RunTrace(source)
    assert len(helper.prompts) == 3
    assert [call.purpose for call in recorded_calls(source_trace)] == [
        "prompt_helper",
        "agent_decision",
    ] * 3
    agent = BranchFakeAgent()
    out = await run_branch(tmp_path, source, 2, ["done branch"], agent=agent)

    assert out.result is not None and out.result.agent_done.value == "done"
    assert agent.parsed_log == ["click 100", "click 101", "done branch"]
    assert len(helper.prompts) == 3
    prompt = out.llm.prompts[0]
    text = "\n".join(str(message.content) for message in prompt)
    assert "External prompt contribution:\nFirst external instruction." in text
    assert "External prompt contribution:\nSecond external instruction." in text
    assert "Checkpoint instruction." not in text
    assert out.run_dir is not None
    branch_trace = RunTrace(out.run_dir)
    assert [call.purpose for call in recorded_calls(branch_trace)] == ["agent_decision"]
    assert recorded_usage(branch_trace).tokens == ScriptedLLM.USAGE.total_tokens
    source_step = source_trace.step(0)
    assert source_step is not None
    assert source_step.prompt_contributions[0].code_digest == source_hook.code_digest
    assert source_step.prompt_contributions[0].placement == "system"


async def test_new_branch_hook_cannot_generate_while_replaying_source_history(tmp_path):
    source_hook, old_helper = _hook(["Old zero", "Old one", "Old two"], scope="each_policy_step")
    source = await record_source(
        tmp_path, ["click 100", "click 101", "done source"], extra_hooks=[source_hook]
    )
    branch_hook, branch_helper = _hook(["New first policy instruction."])
    out = await run_branch(tmp_path, source, 2, ["done branch"], extra_hooks=[branch_hook])

    assert out.result is not None and out.result.agent_done.value == "done"
    assert len(old_helper.prompts) == 3
    assert len(branch_helper.prompts) == 1
    assert out.run_dir is not None
    calls = recorded_calls(RunTrace(out.run_dir))
    assert [(call.purpose, call.step) for call in calls] == [
        ("prompt_helper", 2),
        ("agent_decision", 2),
    ]
    text = "\n".join(str(message.content) for message in out.llm.prompts[0])
    assert "Old zero" in text and "Old one" in text
    assert "Old two" not in text
    assert "New first policy instruction." in text


@pytest.mark.parametrize(
    ("scope", "helper_replies", "expected_contribution_counts"),
    [
        ("first_policy_step", ["First policy advice."], [1, 0]),
        ("each_policy_step", ["First policy advice.", "Second policy advice."], [1, 1]),
    ],
)
async def test_retry_reuses_contribution_and_online_usage_equals_all_recorded_calls(
    tmp_path, scope, helper_replies, expected_contribution_counts
):
    source = await record_source(tmp_path, ["click 100", "click 101", "done source"])
    hook, helper = _hook(helper_replies, scope=scope)
    out = await run_branch(
        tmp_path,
        source,
        1,
        ["not parseable", "click 101", "done branch"],
        loop=BranchLoop(continue_steps=1, parse_retries_per_step=1),
        extra_hooks=[hook],
    )
    assert out.result is not None and out.result.agent_done.value == "done"
    assert len(out.llm.prompts) == 3
    assert len(helper.prompts) == len(helper_replies)
    assert out.run_dir is not None
    trace = RunTrace(out.run_dir)
    records = [record for record in trace.step_records if record.origin == "policy"]
    assert [len(record.prompt_contributions) for record in records] == expected_contribution_counts
    calls = recorded_calls(trace)
    assert len([call for call in calls if call.purpose == "agent_decision"]) == 3
    helpers = [call for call in calls if call.purpose == "prompt_helper"]
    assert len(helpers) == len(helper_replies)
    assert helpers[0].step == 1
    assert all(call.model == "scripted-teacher" for call in calls)
    assert all(call.caller == f"prompt_contribution:{hook.function}" for call in helpers)
    measured = recorded_usage(trace)
    metrics = out.result.metrics
    assert metrics.llm_calls == measured.calls == 3 + len(helper_replies)
    assert metrics.usage is not None
    assert metrics.usage.total_tokens == measured.tokens == 12 * measured.calls
    assert metrics.usage_coverage is not None and metrics.usage_coverage.complete
    assert measured.without_usage == ()
    assert "First policy advice." in str(out.llm.prompts[-1])


async def test_failed_helper_is_a_recorded_execution_failure_without_main_call(tmp_path):
    source = await record_source(tmp_path, ["click 100", "done source"])
    hook, helper = _hook([RuntimeError("helper provider unavailable")])
    out = await run_branch(
        tmp_path,
        source,
        0,
        ["done must not be requested"],
        extra_hooks=[hook],
        raise_on_error=False,
    )
    assert out.result is not None and out.result.agent_done.value == "error"
    assert out.result.error is not None and "helper provider unavailable" in out.result.error
    assert out.llm.prompts == []
    assert len(helper.prompts) == 1
    assert out.run_dir is not None
    calls = recorded_calls(RunTrace(out.run_dir))
    assert len(calls) == 1
    assert (calls[0].purpose, calls[0].status) == ("prompt_helper", "failed")
    usage = recorded_usage(RunTrace(out.run_dir))
    assert usage.calls == 1 and usage.tokens == 0
    assert len(usage.without_usage) == 1
    assert out.result.metrics.usage_coverage is not None
    assert not out.result.metrics.usage_coverage.complete
    assert helper.closed >= 1


async def test_cancelled_helper_is_recorded_and_cancellation_reaches_caller(tmp_path):
    source = await record_source(tmp_path, ["click 100", "done source"])
    cancellation = asyncio.CancelledError("helper cancelled")
    hook, helper = _hook([cancellation])
    with pytest.raises(asyncio.CancelledError) as caught:
        await run_branch(
            tmp_path,
            source,
            0,
            ["done must not be requested"],
            run_id="cancelled-helper",
            extra_hooks=[hook],
            raise_on_error=False,
        )
    assert caught.value is cancellation
    trace = RunTrace(tmp_path / "branches" / "cancelled-helper")
    calls = recorded_calls(trace)
    assert len(calls) == 1
    assert (calls[0].purpose, calls[0].status) == ("prompt_helper", "cancelled")
    assert len(recorded_usage(trace).without_usage) == 1
    assert helper.closed >= 1
