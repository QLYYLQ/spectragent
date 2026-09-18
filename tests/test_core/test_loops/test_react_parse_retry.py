"""ReactLoop in-step parse retry — N4, and the F11 invariant it must not break.

Before this, one malformed reply cost a whole step: ``react.py`` only had
the cross-step ``max_consecutive_parse_errors``, so a format mistake spent
a step, a fresh observation and (on a real page) several seconds. An image
challenge expires in about two minutes; that is an episode lost to a
misplaced brace.

``parse_retries_per_step`` re-asks the model **inside** the same step. The
half that is easy to get wrong is the other half of the contract: the
re-ask must *not* open a new observation cycle.

That is not a stylistic preference. F11 was "the step after a parse failure
got the previous step's screenshot, byte for byte" — the browser capability
kept one observe cycle alive until the next ``execute_action``, and a step
that ran no action (a reply that failed to parse) reused it. The fix made
one ``Environment.observe`` call *be* the cycle: asking a capability for a
fragment it has already served is read as the start of the next observation
and re-samples the page
(``BrowserCapability._ObserveCycleCache.served``). So an in-step retry that
called ``observe`` again would silently re-sample — and the model would be
answering a follow-up question about a screenshot it was never shown.

The capability below implements exactly that rule, so these tests fail if
the retry ever reaches for a new observation.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.llm.messages import AssistantMessage
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.step_record import TokenUsage
from tests._helpers.assembly import make_env

_FRAG = "test_retry_screenshot"


class _ScreenshotFrag(FragmentBase):
    __registry_key__ = _FRAG
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    #: Which observe cycle sampled this. The point of the whole file.
    cycle: int


class _CycleCap(CapabilityBase):
    """A capability with ``BrowserCapability``'s observe-cycle rule.

    One cycle serves each fragment once. A fragment asked for a *second*
    time is the next observation, so the cycle counter advances and the
    content changes — which is what re-sampling a live page looks like.
    """

    __registry_key__ = "retry_cycle_test_cap"
    __wiring__ = {"observation": ("observation", [_FRAG])}

    cycles: int
    served: set

    async def resolve_fragment(self, frag_key, **kw):
        if frag_key in self.served:
            self.cycles += 1
            self.served = {frag_key}
        else:
            self.served.add(frag_key)
        return _ScreenshotFrag(cycle=self.cycles)


def _fresh_env() -> tuple[Any, _CycleCap]:
    cap = _CycleCap()
    cap.cycles = 1
    cap.served = set()
    return make_env(EnvironmentBase, {"retry_cycle_test_cap": cap}), cap


_NEGOTIATED = ObservationNegotiationResult(
    active_fragments=frozenset({_FRAG}),
    env_sourced=frozenset({_FRAG}),
    loop_sourced=frozenset(),
)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 4
    total_tokens = 14


class _Completion:
    def __init__(self, text: str, usage: Any = None) -> None:
        self.completion = text
        self.usage = usage


class _LLM:
    """Replies from a script, and remembers what it was asked each time."""

    model = "test-model"

    def __init__(self, replies: list[str], usage: Any = None) -> None:
        self._replies = replies
        self._usage = usage
        self.calls = 0
        #: One snapshot of the context per call.
        self.contexts: list[list[Any]] = []

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        self.contexts.append(list(messages))
        text = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Completion(text, self._usage)


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: Any) -> None:
        self.messages.extend(ms)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _Agent:
    """Rejects any reply that is not ``ok``; records what it was shown.

    ``build_react_prompt`` writes the cycle of the observation it got into
    the prompt, so the LLM's own context says which observation the
    question was about.
    """

    def __init__(self) -> None:
        self.prompts_built = 0
        self.cycles_seen: list[int] = []
        self.parse_calls = 0

    def build_react_prompt(self, obs: Any) -> list:
        self.prompts_built += 1
        cycle = getattr(obs, _FRAG).cycle
        self.cycles_seen.append(cycle)
        return [AssistantMessage(content=f"[OBS cycle={cycle}]")]

    def parse_react_output(self, out: Any) -> list[Action]:
        self.parse_calls += 1
        if out != "ok":
            raise ParseError(f"cannot parse {out!r}", raw_output=str(out))
        return [Action(name="retry_cycle_test_cap.done", params={})]

    def get_react_output_model(self) -> None:
        return None


async def _act_done(action: Action) -> ActionResult:
    return ActionResult(success=True, is_done=True, extracted_content="answer")


async def _act_noop(action: Action) -> ActionResult:
    return ActionResult(success=True)


def _ctx(*, env: Any, llm: Any, memory: Any, act_fn: Any, recorder: Any) -> dict:
    return {
        "env": env,
        "llm": llm,
        "memory": memory,
        "act_fn": act_fn,
        "negotiated_obs": _NEGOTIATED,
        "step_recorder": recorder,
        "task_id": "t-1",
    }


# ── The invariant ────────────────────────────────────────────────────


class TestRetryDoesNotOpenANewObservationCycle:
    """F11, pinned from the Loop side."""

    async def test_retry_reuses_the_step_observation(self) -> None:
        env, cap = _fresh_env()
        agent = _Agent()
        llm = _LLM(["garbage", "ok"])
        loop = ReactLoop(max_steps=5, parse_retries_per_step=1)

        result = await loop._execute(
            agent,
            "t",
            ctx=_ctx(
                env=env,
                llm=llm,
                memory=_Memory(),
                act_fn=_act_done,
                recorder=StepRecorder(),
            ),
        )

        assert result.agent_done.value == "done"
        # The capability never advanced its cycle: no fragment was asked
        # for twice, so the page was sampled exactly once.
        assert cap.cycles == 1
        # One question was posed about that sample, and it was asked twice.
        assert agent.prompts_built == 1
        assert agent.cycles_seen == [1]
        assert llm.calls == 2

    async def test_the_second_ask_carries_the_first_observation(self) -> None:
        """Not just 'observe wasn't called' — the model saw the same thing.

        The retry grows the context by the rejected reply and the error;
        everything the model was already looking at stays put. If the
        observation were re-taken, the ``[OBS cycle=...]`` turn would
        either change or be duplicated.
        """
        env, _cap = _fresh_env()
        llm = _LLM(["garbage", "ok"])
        loop = ReactLoop(max_steps=5, parse_retries_per_step=1)

        await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=llm,
                memory=_Memory(),
                act_fn=_act_done,
                recorder=StepRecorder(),
            ),
        )

        first, second = llm.contexts
        obs_turns = [m for m in second if "[OBS cycle=" in str(getattr(m, "content", ""))]
        assert [str(m.content) for m in obs_turns] == ["[OBS cycle=1]"]
        # The re-ask is a strict extension of the first ask.
        assert [str(getattr(m, "content", "")) for m in second[: len(first)]] == [
            str(getattr(m, "content", "")) for m in first
        ]
        # …extended by exactly the rejected reply and why it was rejected.
        added = [str(getattr(m, "content", "")) for m in second[len(first) :]]
        assert added[0] == "garbage"
        assert "[SYSTEM]" in added[1] and "cannot parse" in added[1]

    async def test_a_spent_step_does_take_a_new_observation(self) -> None:
        """The contrast that makes the invariant meaningful.

        Once the retries are gone the step *is* spent, and the next step
        opens a real new cycle — the F11 fix, still working.
        """
        env, cap = _fresh_env()
        agent = _Agent()
        loop = ReactLoop(max_steps=5, parse_retries_per_step=0)

        await loop._execute(
            agent,
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["garbage", "ok"]),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=StepRecorder(),
            ),
        )

        assert cap.cycles == 2
        assert agent.cycles_seen == [1, 2]


# ── The feature ──────────────────────────────────────────────────────


class TestRetrySavesTheStep:
    async def test_one_bad_reply_no_longer_costs_a_step(self) -> None:
        env, _cap = _fresh_env()
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=5, parse_retries_per_step=1)

        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["garbage", "ok"]),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=recorder,
            ),
        )

        assert result.agent_done.value == "done"
        assert result.metrics.steps == 1
        assert len(recorder.records) == 1
        # The one record is the successful step, not a parse-error no-op.
        assert [a.name for a in recorder.records[0].actions] == ["retry_cycle_test_cap.done"]

    async def test_default_is_the_old_behaviour(self) -> None:
        """``parse_retries_per_step`` defaults to 0: nothing changes."""
        assert ReactLoop().parse_retries_per_step == 0

        env, _cap = _fresh_env()
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=5)

        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["garbage", "ok"]),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=recorder,
            ),
        )

        assert result.agent_done.value == "done"
        assert result.metrics.steps == 2
        assert recorder.records[0].actions == []
        assert recorder.records[0].llm_output == "garbage"

    async def test_retries_are_per_step_not_per_run(self) -> None:
        """A step that used its retries starts the next one with a full budget."""
        env, cap = _fresh_env()
        agent = _Agent()
        loop = ReactLoop(max_steps=5, parse_retries_per_step=1)

        # step 0: bad, good(noop) — step 1: bad, good(done)
        llm = _LLM(["bad", "ok", "bad", "ok"])
        acts = {"n": 0}

        async def act(action: Action) -> ActionResult:
            acts["n"] += 1
            return ActionResult(
                success=True,
                is_done=acts["n"] >= 2,
                extracted_content="answer" if acts["n"] >= 2 else "",
            )

        result = await loop._execute(
            agent,
            "t",
            ctx=_ctx(
                env=env,
                llm=llm,
                memory=_Memory(),
                act_fn=act,
                recorder=StepRecorder(),
            ),
        )

        assert result.agent_done.value == "done"
        assert result.metrics.steps == 2
        assert llm.calls == 4
        # Two steps, two observations, four LLM calls.
        assert cap.cycles == 2
        assert agent.cycles_seen == [1, 2]


class TestCrossStepCounterIsUnchanged:
    async def test_streak_counts_steps_not_attempts(self) -> None:
        """The cross-step counter keeps its own meaning.

        ``max_consecutive_parse_errors`` is "consecutive *steps* that could
        not be parsed". Letting the in-step retries feed it would make the
        two knobs multiply and a single badly-tuned config end runs after
        one bad reply.
        """
        env, cap = _fresh_env()
        recorder = StepRecorder()
        loop = ReactLoop(
            max_steps=10,
            parse_retries_per_step=2,
            max_consecutive_parse_errors=2,
        )
        llm = _LLM(["garbage"])

        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=llm,
                memory=_Memory(),
                act_fn=_act_done,
                recorder=recorder,
            ),
        )

        assert result.agent_done.value == "error"
        assert "Parse failed 2 consecutive times" in (result.error or "")
        # 2 steps × (1 attempt + 2 retries).
        assert llm.calls == 6
        assert len(recorder.records) == 2
        assert cap.cycles == 2

    async def test_a_successful_retry_resets_the_streak(self) -> None:
        """A step rescued by a retry is a clean step."""
        env, _cap = _fresh_env()
        loop = ReactLoop(
            max_steps=10,
            parse_retries_per_step=1,
            max_consecutive_parse_errors=1,
        )
        # Without the retry, reply 1 alone would end the run.
        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["garbage", "ok"]),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=StepRecorder(),
            ),
        )
        assert result.agent_done.value == "done"


class TestAccounting:
    async def test_every_attempt_is_billed_to_the_step(self) -> None:
        """Retries are real LLM calls and must not be free.

        ``StepRecord.token_usage`` used to be the step's single call. If a
        retried step reported only its last attempt, per-step cost reports
        and ``AgentResult.metrics.usage`` would stop adding up.
        """
        env, _cap = _fresh_env()
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=5, parse_retries_per_step=2)

        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["bad", "worse", "ok"], usage=_Usage()),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=recorder,
            ),
        )

        assert result.metrics.llm_calls == 3
        assert result.metrics.steps == 1
        step_usage = recorder.records[0].token_usage
        assert step_usage is not None
        assert step_usage.total_tokens == 42
        assert step_usage.prompt_tokens == 30
        # The run total and the step total are the same three calls.
        assert result.metrics.usage is not None
        assert result.metrics.usage.total_tokens == 42

    async def test_unmeasured_usage_survives_a_retry(self) -> None:
        """``None`` is the "not measured" sentinel, not zero."""
        env, _cap = _fresh_env()
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=5, parse_retries_per_step=1)

        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=env,
                llm=_LLM(["bad", "ok"], usage=None),
                memory=_Memory(),
                act_fn=_act_done,
                recorder=recorder,
            ),
        )
        assert result.metrics.usage is None
        assert recorder.records[0].token_usage is None


class TestRetryPathRespectsRunAbort:
    """N4 sits on top of B5 — the retry loop must not re-swallow a veto."""

    async def test_abort_during_a_retry_propagates(self) -> None:
        env, _cap = _fresh_env()

        class _AbortOnSecondCall(_LLM):
            async def ainvoke(self, messages: Any, **kw: Any) -> Any:
                if self.calls >= 1:
                    raise RunAbort("risk control", source="guard")
                return await super().ainvoke(messages, **kw)

        loop = ReactLoop(max_steps=5, parse_retries_per_step=3)
        with pytest.raises(RunAbort):
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=env,
                    llm=_AbortOnSecondCall(["garbage"]),
                    memory=_Memory(),
                    act_fn=_act_noop,
                    recorder=StepRecorder(),
                ),
            )


class TestTokenUsageMerge:
    """The helper the step accounting rests on, in isolation.

    One thing to know before changing these: on the ReactLoop path both
    operands come from ``TokenUsage.from_llm_usage``
    (``schemas/step_record.py:19``), which builds a *fresh* model out of
    the three declared counters and copies nothing else. A provider's
    ``extra="allow"`` fields are therefore already gone by the time this
    helper is called, and no production call site can produce the input
    the last test uses. That test pins the helper's own arithmetic, not a
    guarantee the pipeline delivers — an earlier version of this file
    claimed the latter, and it was simply untrue.
    """

    def test_none_is_the_unmeasured_sentinel(self) -> None:
        """``None`` means "nobody measured", and must not become zero.

        It is the identity element of the merge in both directions, which
        is what lets the accumulator start at ``None`` and stay there for
        a whole run against a provider that reports no usage.
        """
        from spectragent.core.loops.react import _merge_token_usage

        one = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        assert _merge_token_usage(None, None) is None
        assert _merge_token_usage(None, one) == one
        assert _merge_token_usage(one, None) == one
        # The helper happens to return the operand rather than a copy.
        # That is an optimisation, not a contract — assert values, not
        # identity, so a future rewrite is free to copy.

    def test_extras_on_the_base_are_not_dropped_by_the_sum(self) -> None:
        """Merging adds three counters and leaves everything else alone.

        Unreachable today (see the class docstring), and kept anyway: if
        ``from_llm_usage`` ever starts forwarding provider extras, the sum
        must already be the ``model_copy(update=...)`` that preserves them
        rather than a reconstruction that silently drops them.
        """
        from spectragent.core.loops.react import _merge_token_usage

        # ``model_validate`` rather than a kwarg: ``reasoning_tokens`` is an
        # ``extra="allow"`` field, so the constructor has no such parameter
        # as far as a type checker is concerned.
        base = TokenUsage.model_validate(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "reasoning_tokens": 7,
            }
        )
        merged = _merge_token_usage(
            base, TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7)
        )
        assert merged is not None
        assert (merged.prompt_tokens, merged.completion_tokens, merged.total_tokens) == (
            4,
            5,
            9,
        )
        assert (merged.model_extra or {}).get("reasoning_tokens") == 7

    def test_the_loop_never_hands_this_helper_an_extra(self) -> None:
        """Pin the upstream fact the class docstring rests on.

        If ``TokenUsage.from_llm_usage`` ever starts carrying extras
        through, the two tests above stop being hypothetical and their
        wording needs revisiting.
        """

        class _RichUsage:
            prompt_tokens = 1
            completion_tokens = 2
            total_tokens = 3
            reasoning_tokens = 9

        converted = TokenUsage.from_llm_usage(_RichUsage())
        assert converted is not None
        assert converted.model_extra in (None, {})
