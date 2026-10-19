"""Prompt contributors obey phase, ownership and the public Agent boundary."""

from __future__ import annotations

import asyncio
import inspect
from hashlib import sha256
from typing import Any, cast

import pytest

from spectragent.core.aop.errors import DeadInterceptError
from spectragent.core.aop.weaver import Weaver
from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.prompt_contribution import PromptContributionHook
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.call_context import RunCallContext
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.schemas.prompt import PromptContribution, PromptRequest, ReactPromptContext


def plain_contribution(*, request: PromptRequest) -> str:
    request.observation["calls"].append(request.context.step)
    text = request.parameters.get("text", "Inspect the current page.")
    request.parameters["text"] = "This mutation must stay local to this request."
    return text


async def async_contribution(*, request: PromptRequest) -> PromptContribution:
    request.observation["calls"].append(request.context.step)
    await asyncio.sleep(0)
    return PromptContribution(
        text="Check the actual destination.",
        placement="system",
        source="cannot forge another function",
        code_digest="cannot forge another digest",
    )


async def helper_contribution(*, request: PromptRequest) -> str:
    assert request.helper is not None
    result = await request.helper.ainvoke([UserMessage(content=str(request.context.step))])
    return result.completion


def failing_contribution(*, request: PromptRequest) -> str:
    raise request.observation["error"]


def invalid_result(*, request: PromptRequest) -> int:
    return 42


def positional_request(request: PromptRequest) -> str:
    return "invalid"


def catch_all_request(**kwargs: Any) -> str:
    return "invalid"


def additional_parameter(*, request: PromptRequest, text: str = "invalid") -> str:
    return text


def untyped_request(*, request) -> str:
    return "invalid"


def wrong_request_type(*, request: str) -> str:
    return "invalid"


def default_request(*, request: PromptRequest | None = None) -> str:
    return "invalid"


def generator_request(*, request: PromptRequest):
    yield "invalid"


async def async_generator_request(*, request: PromptRequest):
    yield "invalid"


lambda_request = lambda *, request: "invalid"  # noqa: E731
alias_request = plain_contribution


def _nested():
    def contribution(*, request: PromptRequest) -> str:
        return "invalid"

    return contribution


nested_request = _nested()


class _Agent:
    async def build_react_prompt(self, observation, *, context):
        observation["builder"].append(context)
        return [contribution.model_dump() for contribution in context.contributions]


class _Helper:
    inference_kind = "chat"
    name = "test helper"

    def __init__(self, call_context, *, error=None):
        self.call_context = call_context
        self.calls = []
        self.error = error
        self.closed = 0

    async def aclose(self, *, timeout=5.0):
        self.closed += 1
        return ()

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(self.call_context.current)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return ChatInvokeCompletion(completion=f"Advice for step {messages[0].content}", usage=None)


def _reference(name="plain_contribution"):
    return f"{__name__}:{name}"


def _models(*models: Any) -> list[BaseChatModel]:
    """The focused doubles implement invocation and cleanup, not provider configuration."""
    return list(models)


def _prepared(*, name="plain_contribution", **kwargs):
    hook = PromptContributionHook(function=_reference(name), **kwargs)
    calls = RunCallContext()
    hook.bind_prompt_models(hook.helper_llms, calls)
    weaver = Weaver([hook])
    agent = weaver.wrap(_Agent(), role="agent")
    weaver.audit_intercepts()
    return hook, calls, agent


def _observation():
    return {"calls": [], "builder": []}


def test_registered_function_digest_and_identity():
    hook, _, _ = _prepared()
    assert HookRegistrar.get("prompt_contribution") is PromptContributionHook
    assert hook.code_digest == sha256(inspect.getsource(plain_contribution).encode()).hexdigest()
    assert hash(hook)
    assert hook.blocking


@pytest.mark.parametrize(
    "reference",
    [
        "not-a-reference",
        "package.module:",
        "package:class.method",
        " package:method",
        "tests.no_such_module:missing",
        _reference("missing"),
        _reference("positional_request"),
        _reference("catch_all_request"),
        _reference("additional_parameter"),
        _reference("untyped_request"),
        _reference("wrong_request_type"),
        _reference("default_request"),
        _reference("generator_request"),
        _reference("async_generator_request"),
        _reference("lambda_request"),
        _reference("alias_request"),
        _reference("nested_request"),
        _reference("_Agent"),
        "builtins:len",
    ],
)
def test_bad_function_contract_fails_at_construction(reference):
    with pytest.raises(ValueError, match="contribution"):
        PromptContributionHook(function=reference)


def test_direct_configuration_rejects_unsupported_scope_and_multiple_helpers():
    with pytest.raises(ValueError, match="scope"):
        PromptContributionHook(function=_reference(), scope=cast(Any, "sometimes"))
    with pytest.raises(ValueError, match="at most one"):
        PromptContributionHook(function=_reference(), helper_llms=_models(object(), object()))


async def test_first_branch_policy_generates_once_and_preserves_exact_text():
    text = "  Keep the original whitespace.\nSecond line.  "
    hook, calls, agent = _prepared(parameters={"text": text})
    observation = _observation()
    first = ReactPromptContext(step=4, phase="policy", is_branch=True, policy_step=0)
    messages = await agent.build_react_prompt(observation, context=first)
    retry = await agent.build_react_prompt(observation, context=first)
    later = ReactPromptContext(step=5, phase="policy", is_branch=True, policy_step=1)
    await agent.build_react_prompt(observation, context=later)

    assert observation["calls"] == [4]
    assert messages == retry
    assert messages[0] == {
        "text": text,
        "placement": "turn",
        "source": hook.function,
        "code_digest": hook.code_digest,
    }
    assert first.generated == {hook}
    assert later.contributions == []
    assert hook.parameters == {"text": text}
    assert calls.current.purpose == "agent_decision"


@pytest.mark.parametrize(
    ("phase", "is_branch", "policy_step"),
    [("history", True, 0), ("terminal", True, 0), ("policy", False, 0)],
)
async def test_ineligible_context_passes_recorded_contributions_without_generation(
    phase, is_branch, policy_step
):
    hook, _, agent = _prepared(name="failing_contribution")
    recorded = PromptContribution(
        text="An original contribution.",
        placement="system",
        source="old.function:contribution",
        code_digest="old-code",
    )
    context = ReactPromptContext(
        step=0,
        phase=phase,
        is_branch=is_branch,
        policy_step=policy_step,
        contributions=[recorded],
    )
    observation = _observation()
    messages = await agent.build_react_prompt(observation, context=context)

    assert messages == [recorded.model_dump()]
    assert observation["builder"] == [context]
    assert context.contributions == [recorded]
    assert hook not in context.generated


async def test_each_step_generation_uses_async_structured_placement_and_reuses_retry():
    hook, _, agent = _prepared(name="async_contribution", scope="each_policy_step")
    observation = _observation()
    for step in (0, 1, 2):
        context = ReactPromptContext(step=step, phase="policy", policy_step=step)
        messages = await agent.build_react_prompt(observation=observation, context=context)
        assert messages == await agent.build_react_prompt(observation, context=context)
        assert context.contributions == [
            PromptContribution(
                text="Check the actual destination.",
                placement="system",
                source=hook.function,
                code_digest=hook.code_digest,
            )
        ]
    assert observation["calls"] == [0, 1, 2]


async def test_concurrent_contexts_do_not_share_generated_contributions():
    _, _, agent = _prepared(name="async_contribution", scope="each_policy_step")
    observations = [_observation(), _observation()]
    contexts = [ReactPromptContext(step=0, phase="policy") for _ in observations]
    await asyncio.gather(
        *(
            agent.build_react_prompt(obs, context=ctx)
            for obs, ctx in zip(observations, contexts, strict=True)
        )
    )
    assert [obs["calls"] for obs in observations] == [[0], [0]]
    assert contexts[0].contributions is not contexts[1].contributions
    assert len(contexts[0].contributions) == len(contexts[1].contributions) == 1


async def test_distinct_hook_instances_do_not_collapse_to_one_cached_function():
    hooks = [PromptContributionHook(function=_reference()) for _ in range(2)]
    for hook in hooks:
        hook.bind_prompt_models([], RunCallContext())
    agent = Weaver(hooks).wrap(_Agent(), role="agent")
    observation = _observation()
    context = ReactPromptContext(step=0, phase="policy", is_branch=True)
    await agent.build_react_prompt(observation, context=context)
    await agent.build_react_prompt(observation, context=context)
    assert observation["calls"] == [0, 0]
    assert len(context.generated) == len(context.contributions) == 2


@pytest.mark.parametrize("error", [ValueError("contribution failed"), asyncio.CancelledError()])
async def test_function_error_or_cancellation_propagates_before_builder(error):
    _, calls, agent = _prepared(name="failing_contribution")
    context = ReactPromptContext(step=0, phase="policy", is_branch=True)
    observation = _observation() | {"error": error}
    with pytest.raises(type(error)) as caught:
        await agent.build_react_prompt(observation, context=context)
    assert caught.value is error
    assert not context.generated
    assert not context.contributions
    assert not observation["builder"]
    assert calls.current.purpose == "agent_decision"


async def test_invalid_result_is_an_execution_error():
    _, _, agent = _prepared(name="invalid_result")
    context = ReactPromptContext(step=0, phase="policy", is_branch=True)
    with pytest.raises(TypeError, match="expected str or PromptContribution"):
        await agent.build_react_prompt(_observation(), context=context)
    assert not context.generated


async def test_helper_uses_woven_surface_and_same_model_has_separate_call_purpose():
    call_context = RunCallContext()
    owned = _Helper(call_context)
    woven = _Helper(call_context)
    hook = PromptContributionHook(
        function=_reference("helper_contribution"), helper_llms=_models(owned)
    )
    hook.bind_prompt_models(_models(woven), call_context)
    await hook.on_run_start(call_context=call_context)
    agent = Weaver([hook]).wrap(_Agent(), role="agent")
    context = ReactPromptContext(step=7, phase="policy", is_branch=True)
    await agent.build_react_prompt(_observation(), context=context)
    await woven.ainvoke([UserMessage(content="main decision")])

    assert hook.helper_llms == [owned]
    assert not owned.calls
    assert [(c.caller, c.purpose, c.step) for c in woven.calls] == [
        (f"prompt_contribution:{hook.function}", "prompt_helper", 7),
        ("agent", "agent_decision", None),
    ]
    assert context.contributions[0].text == "Advice for step 7"


@pytest.mark.parametrize("error", [RuntimeError("provider unavailable"), asyncio.CancelledError()])
async def test_helper_error_or_cancellation_propagates_without_fallback(error):
    call_context = RunCallContext()
    helper = _Helper(call_context, error=error)
    hook = PromptContributionHook(
        function=_reference("helper_contribution"), helper_llms=_models(helper)
    )
    hook.bind_prompt_models(_models(helper), call_context)
    agent = Weaver([hook]).wrap(_Agent(), role="agent")
    observation = _observation()
    context = ReactPromptContext(step=2, phase="policy", is_branch=True)
    with pytest.raises(type(error)) as caught:
        await agent.build_react_prompt(observation, context=context)
    assert caught.value is error
    assert helper.calls[0].purpose == "prompt_helper"
    assert not observation["builder"]
    assert not context.contributions
    assert not context.generated
    assert call_context.current.purpose == "agent_decision"


async def test_helper_history_and_terminal_never_invoke_model():
    call_context = RunCallContext()
    helper = _Helper(call_context)
    hook = PromptContributionHook(
        function=_reference("helper_contribution"), helper_llms=_models(helper)
    )
    hook.bind_prompt_models(_models(helper), call_context)
    agent = Weaver([hook]).wrap(_Agent(), role="agent")
    for phase in ("history", "terminal"):
        context = ReactPromptContext(step=0, phase=phase, is_branch=True)
        await agent.build_react_prompt(_observation(), context=context)
    assert not helper.calls


async def test_concurrent_helper_generation_keeps_each_steps_attribution():
    call_context = RunCallContext()
    helper = _Helper(call_context)
    hook = PromptContributionHook(
        function=_reference("helper_contribution"),
        helper_llms=_models(helper),
        scope="each_policy_step",
    )
    hook.bind_prompt_models(_models(helper), call_context)
    agent = Weaver([hook]).wrap(_Agent(), role="agent")
    contexts = [ReactPromptContext(step=step, phase="policy") for step in (5, 8)]
    await asyncio.gather(
        *(agent.build_react_prompt(_observation(), context=context) for context in contexts)
    )
    assert [call.step for call in helper.calls] == [5, 8]
    assert [context.contributions[0].text for context in contexts] == [
        "Advice for step 5",
        "Advice for step 8",
    ]
    assert call_context.current.purpose == "agent_decision"


def test_binding_count_must_match_owned_helpers():
    hook = PromptContributionHook(function=_reference())
    with pytest.raises(ValueError, match="exactly the configured"):
        hook.bind_prompt_models(_models(object()), RunCallContext())


async def test_generation_requires_runtime_binding_and_public_context():
    hook = PromptContributionHook(function=_reference())
    agent = Weaver([hook]).wrap(_Agent(), role="agent")
    with pytest.raises(TypeError, match="keyword-only context"):
        await agent.build_react_prompt(_observation())
    with pytest.raises(RuntimeError, match="bound by Runtime"):
        await agent.build_react_prompt(
            _observation(), context=ReactPromptContext(step=0, phase="policy", is_branch=True)
        )


def test_unsupported_agent_boundary_fails_during_weaver_audit():
    class SyncAgent:
        def build_react_prompt(self, observation, *, context):
            return []

    for agent in (object(), SyncAgent()):
        hook = PromptContributionHook(function=_reference())
        weaver = Weaver([hook])
        weaver.wrap(agent, role="agent")
        with pytest.raises(DeadInterceptError):
            weaver.audit_intercepts()


def test_configuration_keeps_helper_optional_and_nested_model_knobs():
    from spectragent.core.archive._registrar import ArchiverRegistrar
    from spectragent.core.capabilities.browser import archive as _archive  # noqa: F401
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.llm._registrar import LLMRegistrar
    from spectragent.core.llm.providers.openai import ChatOpenAI

    composer = ConfigComposer(
        registrars={"hook": HookRegistrar, "llm": LLMRegistrar, "archiver": ArchiverRegistrar},
        discover_roots=(),
    )
    omitted = composer.compose(
        {
            "llm": [{"model_type": "openai", "provider": {"name": "direct"}, "model": "main"}],
            "hook": [{"hook_id": "prompt_contribution", "function": _reference()}],
        }
    )
    assert omitted.single("hook").helper_llms == []
    configured = composer.compose(
        {
            "hook": [
                {
                    "hook_id": "prompt_contribution",
                    "function": _reference(),
                    "helper_llms": [
                        {
                            "model_type": "openai",
                            "provider": {"name": "direct"},
                            "model": "helper",
                            "temperature": 0.2,
                        }
                    ],
                }
            ]
        }
    )
    helper = configured.single("hook").helper_llms[0]
    assert isinstance(helper, ChatOpenAI)
    assert helper.model == "helper"
    assert helper.temperature == 0.2
