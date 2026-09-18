"""ReactLoop — ParseError handling as framework-level control flow.

Parse failures are NOT dispatched through guarded_act. Instead,
ReactLoop catches ``ParseError``, feeds the error detail back into
the next prompt, and terminates after ``max_consecutive_parse_errors``
consecutive failures.
"""

from __future__ import annotations

import asyncio
from typing import Any

from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


def _make_loop(
    max_steps: int = 100,
    max_consecutive_parse_errors: int = 3,
) -> ReactLoop:
    """Build a ReactLoop with overridden config fields."""
    loop = ReactLoop()
    loop.max_steps = max_steps
    loop.max_consecutive_parse_errors = max_consecutive_parse_errors
    return loop


# ── Fixtures ─────────────────────────────────────────────────


class _DummyObs:
    pass


class _FakeEnv:
    env_type = "fake"

    async def observe(self, *_a: Any, **_kw: Any) -> _DummyObs:
        return _DummyObs()

    async def act(self, action: Action, **kw: Any) -> ActionResult:
        if action.name == "done":
            return ActionResult(
                success=True,
                is_done=True,
                extracted_content=action.params.get("extracted_content", "ok"),
            )
        return ActionResult(success=True)

    def get_action_space(self, **kw: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="done", description="Done")]


class _FakeLLM:
    model = "test"

    def __init__(self, completions: list[str]) -> None:
        self._completions = list(completions)
        self._idx = 0

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        text = self._completions[min(self._idx, len(self._completions) - 1)]
        self._idx += 1

        class R:
            completion = text

        return R()


class _FakeMemory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def add_messages(self, msgs: Any) -> None:
        self.messages.extend(msgs)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


# ── Tests ────────────────────────────────────────────────────


class TestParseErrorRecovery:
    """A transient parse error followed by a successful parse should work."""

    def test_single_parse_error_then_success(self) -> None:
        call_count = 0

        class _Agent:
            __wiring__: dict = {}

            def build_react_prompt(self, obs: Any) -> list:
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, llm_output: Any) -> list[Action]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ParseError("bad output", raw_output="garbage")
                return [Action(name="done", params={"extracted_content": "ok"})]

            def get_react_output_model(self) -> None:
                return None

        loop = _make_loop(max_steps=10, max_consecutive_parse_errors=3)
        ctx = {
            "env": _FakeEnv(),
            "llm": _FakeLLM(["bad", "good"]),
            "memory": _FakeMemory(),
            "act_fn": _FakeEnv().act,
            "task_id": "t",
        }
        result = asyncio.run(loop._execute(_Agent(), "test", ctx=ctx))
        assert result.agent_done.value == "done"
        assert call_count == 2

    def test_parse_error_resets_streak_on_success(self) -> None:
        """Error → success → error → success should NOT terminate."""
        call_count = 0

        class _Agent:
            __wiring__: dict = {}

            def build_react_prompt(self, obs: Any) -> list:
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, llm_output: Any) -> list[Action]:
                nonlocal call_count
                call_count += 1
                # Alternate: fail, succeed(no-op), fail, succeed(done)
                if call_count in (1, 3):
                    raise ParseError("bad", raw_output="x")
                if call_count == 4:
                    return [Action(name="done", params={"extracted_content": "ok"})]
                # Non-terminating action — loop continues.
                return [Action(name="click", params={})]

            def get_react_output_model(self) -> None:
                return None

        loop = _make_loop(max_steps=10, max_consecutive_parse_errors=2)
        ctx = {
            "env": _FakeEnv(),
            "llm": _FakeLLM(["a"] * 10),
            "memory": _FakeMemory(),
            "act_fn": _FakeEnv().act,
            "task_id": "t",
        }
        result = asyncio.run(loop._execute(_Agent(), "test", ctx=ctx))
        # Should complete successfully because the streak resets.
        assert result.agent_done.value == "done"
        assert call_count == 4


class TestParseErrorTermination:
    """Consecutive parse errors beyond the limit terminate the run."""

    def test_consecutive_errors_terminate(self) -> None:
        class _AlwaysFail:
            __wiring__: dict = {}

            def build_react_prompt(self, obs: Any) -> list:
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, llm_output: Any) -> list[Action]:
                raise ParseError("always bad")

            def get_react_output_model(self) -> None:
                return None

        loop = _make_loop(max_steps=10, max_consecutive_parse_errors=3)
        ctx = {
            "env": _FakeEnv(),
            "llm": _FakeLLM(["x"] * 10),
            "memory": _FakeMemory(),
            "act_fn": _FakeEnv().act,
            "task_id": "t",
        }
        result = asyncio.run(loop._execute(_AlwaysFail(), "test", ctx=ctx))
        assert result.agent_done.value == "error"
        assert "Parse failed 3 consecutive times" in (result.error or "")

    def test_error_feedback_injected_into_memory(self) -> None:
        mem = _FakeMemory()

        class _FailOnce:
            __wiring__: dict = {}

            def __init__(self) -> None:
                self._calls = 0

            def build_react_prompt(self, obs: Any) -> list:
                return [{"role": "user", "content": "go"}]

            def parse_react_output(self, llm_output: Any) -> list[Action]:
                self._calls += 1
                if self._calls == 1:
                    raise ParseError("bad format")
                return [Action(name="done", params={"extracted_content": "ok"})]

            def get_react_output_model(self) -> None:
                return None

        loop = _make_loop(max_steps=10)
        ctx = {
            "env": _FakeEnv(),
            "llm": _FakeLLM(["x", "y"]),
            "memory": mem,
            "act_fn": _FakeEnv().act,
            "task_id": "t",
        }
        asyncio.run(loop._execute(_FailOnce(), "test", ctx=ctx))
        # Memory should contain the parse error feedback message.
        feedback_msgs = [
            m for m in mem.messages if hasattr(m, "content") and "[SYSTEM]" in str(m.content)
        ]
        assert len(feedback_msgs) == 1
        assert "bad format" in str(feedback_msgs[0].content)

    def test_step_record_has_empty_actions_on_parse_error(self) -> None:
        """Parse error steps should record an empty action list."""

        class _FailThenDone:
            __wiring__: dict = {}

            def __init__(self) -> None:
                self._calls = 0

            def build_react_prompt(self, obs: Any) -> list:
                return []

            def parse_react_output(self, llm_output: Any) -> list[Action]:
                self._calls += 1
                if self._calls == 1:
                    raise ParseError("bad")
                return [Action(name="done", params={"extracted_content": "ok"})]

            def get_react_output_model(self) -> None:
                return None

        loop = _make_loop(max_steps=10)
        ctx = {
            "env": _FakeEnv(),
            "llm": _FakeLLM(["x", "y"]),
            "memory": _FakeMemory(),
            "act_fn": _FakeEnv().act,
            "task_id": "t",
        }
        result = asyncio.run(loop._execute(_FailThenDone(), "test", ctx=ctx))
        # Step 0 was parse error → empty actions, Step 1 was done.
        assert result.metrics.steps == 2
