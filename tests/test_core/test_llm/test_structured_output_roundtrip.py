"""Structured output, end to end (F4 / §6.1.1 + §6.1.2 + §6.4.5).

``ReactLoop`` used to compute ``agent.get_react_output_model()`` and then
call ``llm.ainvoke(context, output_format=None)`` — a dead store that made
every provider's structured-output branch unreachable. Turning it on alone
breaks ``parse_react_output``, because the per-run output model narrowed
``action`` to a type the downstream duck-check rejects.

These tests pin the whole chain rather than the pieces:

    agent.get_react_output_model()
        -> ReactLoop passes it to llm.ainvoke as output_format
        -> provider validates the response into that model
        -> agent.parse_react_output(model_instance) -> [Action, ...]

The provider cases drive the *real* providers with faked transports, not
a stand-in for a provider, because the whole area was unexecuted while
``output_format`` was pinned to ``None``.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from spectragent.agents._output_models import BrowserUseOutput
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop, _serialize_llm_output
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

# ── Shared fixtures ───────────────────────────────────────────────


def _descriptors() -> list[ActionDescriptor]:
    return [
        ActionDescriptor(name="browser.click", description="Click.", params={"index": "int"}),
        ActionDescriptor(name="browser.navigate", description="Go.", params={"url": "str"}),
        ActionDescriptor(name="browser.done", description="Finish.", params={"text": "str"}),
    ]


def _prepared_agent() -> BrowserUseAgent:
    agent = BrowserUseAgent()
    agent.prepare(task="t", action_descriptors=_descriptors())
    return agent


_PAYLOAD = {"memory": "m", "action": [{"browser.click": {"index": 1}}]}


# ── 1. The acceptance criterion from the handoff ──────────────────


class TestPerRunModelRoundTrip:
    """§6.1.2 — the per-run model must survive parse_react_output."""

    def test_structured_output_round_trips_to_actions(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        out = model.model_validate(_PAYLOAD)
        assert agent.parse_react_output(out) == [
            Action(name="browser.click", params={"index": 1})
        ]

    def test_per_run_and_base_models_are_wire_identical(self) -> None:
        """Both models must hand ``_items_to_actions`` the same thing.

        This is the bug: ``_PerRunOutput`` subclasses ``BrowserUseOutput``
        so the ``isinstance`` fast path fires, and anything but a plain
        ``dict`` in ``action`` fails the duck-check one frame later.
        """
        agent = _prepared_agent()
        per_run = agent.get_react_output_model()
        assert per_run is not None

        narrow = per_run.model_validate(_PAYLOAD)
        loose = BrowserUseOutput.model_validate(_PAYLOAD)

        assert isinstance(narrow, BrowserUseOutput)
        assert type(narrow.action[0]) is type(loose.action[0]) is dict
        assert narrow.action == loose.action
        assert agent.parse_react_output(narrow) == agent.parse_react_output(loose)

    def test_model_dump_json_has_no_serializer_mismatch(self) -> None:
        """``react.py`` calls this every step — it must not warn.

        A ``RootModel``-wrapped item flattened by a ``model_validator``
        would leave the declared field type disagreeing with the runtime
        value, and Pydantic warns on every dump.
        """
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = model.model_validate(_PAYLOAD).model_dump_json()

        assert [str(w.message) for w in caught] == []
        assert json.loads(dumped)["action"] == [{"browser.click": {"index": 1}}]

    def test_aliases_still_translate_to_canonical_actions(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        out = model.model_validate(
            {"memory": "m", "action": [{"click_element": {"index": 4}}]}
        )
        assert agent.parse_react_output(out) == [
            Action(name="browser.click", params={"index": 4})
        ]

    def test_done_text_is_renamed_on_the_structured_path(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        out = model.model_validate(
            {"memory": "m", "action": [{"browser.done": {"text": "the answer"}}]}
        )
        assert agent.parse_react_output(out) == [
            Action(name="browser.done", params={"extracted_content": "the answer"})
        ]


# ── 2. Hallucinated verbs die at parse time, not at guarded_act ───


class TestHallucinatedVerbRejection:
    """§6.1.1 — the per-run verb set must bite on the text path too."""

    def test_text_path_rejects_hallucinated_verb(self) -> None:
        agent = _prepared_agent()
        raw = json.dumps(
            {"memory": "m", "action": [{"totally_hallucinated_verb": {}}]}
        )
        with pytest.raises(ParseError) as exc:
            agent.parse_react_output(raw)

        detail = exc.value.detail
        assert "totally_hallucinated_verb" in detail
        # The allowed set rides along so the LLM can self-correct next turn.
        assert "browser.click" in detail

    def test_structured_path_rejects_hallucinated_verb(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        with pytest.raises(ValidationError) as exc:
            model.model_validate(
                {"memory": "m", "action": [{"totally_hallucinated_verb": {}}]}
            )
        assert "unknown action 'totally_hallucinated_verb'" in str(exc.value)

    def test_negotiated_out_verb_is_rejected(self) -> None:
        """A real action the capability did not offer this run."""
        agent = BrowserUseAgent()
        agent.prepare(
            task="t",
            action_descriptors=[
                ActionDescriptor(name="browser.click", description="c", params={}),
            ],
        )
        with pytest.raises(ParseError):
            agent.parse_react_output(
                json.dumps({"memory": "m", "action": [{"browser.navigate": {"url": "u"}}]})
            )

    def test_before_prepare_the_loose_model_still_parses(self) -> None:
        """No ``prepare()`` -> no negotiated set -> fall back, do not crash."""
        agent = BrowserUseAgent()
        actions = agent.parse_react_output(json.dumps(_PAYLOAD))
        assert actions == [Action(name="browser.click", params={"index": 1})]


# ── 3. ReactLoop actually routes output_format ────────────────────


class _FakeEnv:
    """Env whose observation carries just enough for a real agent to render."""

    env_type = "fake"

    async def observe(self, *_a: Any, **_kw: Any) -> Any:
        return SimpleNamespace(
            task=SimpleNamespace(user_request="task", follow_ups=[]),
            history=None,
            browser_meta=None,
            dom=None,
        )

    async def act(self, action: Action, **_kw: Any) -> ActionResult:
        return ActionResult(success=True, is_done=True, extracted_content="ok")


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


class _RecordingLLM:
    """Records the ``output_format`` every ainvoke was given."""

    model = "test"

    def __init__(self, completion: Any) -> None:
        self._completion = completion
        self.seen_output_formats: list[Any] = []

    async def ainvoke(self, _messages: Any, output_format: Any = None, **_kw: Any) -> Any:
        self.seen_output_formats.append(output_format)
        return SimpleNamespace(completion=self._completion, usage=None)


class _StubAgent:
    """Minimal ReactLoop-compatible agent with a pluggable output model."""

    __wiring__: dict = {}

    def __init__(self, output_model: type[BaseModel] | None) -> None:
        self._output_model = output_model

    def build_react_prompt(self, _obs: Any) -> list:
        return [UserMessage(content="go")]

    def parse_react_output(self, _llm_output: Any) -> list[Action]:
        return [Action(name="browser.done", params={})]

    def get_react_output_model(self) -> type[BaseModel] | None:
        return self._output_model


def _run_loop(agent: Any, llm: Any) -> Any:
    loop = ReactLoop()
    loop.max_steps = 1
    env = _FakeEnv()
    ctx = {
        "env": env,
        "llm": llm,
        "memory": _FakeMemory(),
        "act_fn": env.act,
        "session_id": None,
        "task_id": "t",
    }
    return asyncio.run(loop._execute(agent, "task", ctx=ctx))


class TestReactLoopRoutesOutputFormat:
    """§6.1.1 — the computed output model must reach the provider."""

    def test_agents_output_model_reaches_ainvoke(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        llm = _RecordingLLM(model.model_validate(  # type: ignore[union-attr]
            {"memory": "m", "action": [{"browser.done": {"text": "ok"}}]}
        ))

        result = _run_loop(agent, llm)

        assert llm.seen_output_formats == [model]
        assert llm.seen_output_formats[0] is not None
        assert result.agent_done.value == "done"

    def test_agent_returning_none_still_uses_the_text_path(self) -> None:
        """UI-TARS style: ``get_react_output_model`` returns ``None``."""
        llm = _RecordingLLM("free text")
        _run_loop(_StubAgent(None), llm)
        assert llm.seen_output_formats == [None]

    def test_agent_without_the_optional_slot_passes_none(self) -> None:
        class _NoSlotAgent:
            __wiring__: dict = {}

            def build_react_prompt(self, _obs: Any) -> list:
                return [UserMessage(content="go")]

            def parse_react_output(self, _o: Any) -> list[Action]:
                return [Action(name="browser.done", params={})]

        llm = _RecordingLLM("free text")
        _run_loop(_NoSlotAgent(), llm)
        assert llm.seen_output_formats == [None]


# ── 4. Serialization of a structured completion ───────────────────


class TestCompletionSerialization:
    """F4 item 5 — the success and ParseError branches must agree."""

    def test_structured_completion_serializes_as_json_not_repr(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        completion = model.model_validate(_PAYLOAD)

        text = _serialize_llm_output(completion)
        assert json.loads(text)["action"] == [{"browser.click": {"index": 1}}]

    def test_text_completion_passes_through(self) -> None:
        assert _serialize_llm_output("plain text") == "plain text"

    def test_both_loop_branches_use_the_same_serialization(self) -> None:
        """Memory must show the LLM its own JSON on success *and* on failure.

        A ``str(model)`` repr is not something the model can imitate, so
        the two branches disagreeing means a parse failure poisons the
        next prompt with a shape the model never emitted.
        """
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        completion = model.model_validate(_PAYLOAD)
        expected = completion.model_dump_json()

        # Success path.
        ok_llm = _RecordingLLM(completion)
        ok_memory = _FakeMemory()
        loop = ReactLoop()
        loop.max_steps = 1
        env = _FakeEnv()
        asyncio.run(
            loop._execute(
                _StubAgent(model),
                "task",
                ctx={
                    "env": env, "llm": ok_llm, "memory": ok_memory,
                    "act_fn": env.act, "session_id": None, "task_id": "t",
                },
            )
        )

        # ParseError path, same completion object.
        class _FailingAgent(_StubAgent):
            def parse_react_output(self, _llm_output: Any) -> list[Action]:
                raise ParseError("nope", raw_output="x")

        bad_memory = _FakeMemory()
        loop2 = ReactLoop()
        loop2.max_steps = 1
        asyncio.run(
            loop2._execute(
                _FailingAgent(model),
                "task",
                ctx={
                    "env": env, "llm": _RecordingLLM(completion), "memory": bad_memory,
                    "act_fn": env.act, "session_id": None, "task_id": "t",
                },
            )
        )

        ok_text = [m.content for m in ok_memory.messages if m.role == "assistant"]
        bad_text = [m.content for m in bad_memory.messages if m.role == "assistant"]
        assert ok_text == bad_text == [expected]
        assert "BrowserUseOutputForRun(" not in ok_text[0]


# ── 5. Real provider round-trips ──────────────────────────────────


def _anthropic_text_message(payload: dict[str, Any]):
    from anthropic.types import Message, TextBlock, Usage

    return Message(
        id="msg_1",
        content=[TextBlock(text=json.dumps(payload), type="text")],
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="end_turn",
        type="message",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _anthropic_tool_message(payload: dict[str, Any]):
    from anthropic.types import Message, ToolUseBlock, Usage

    return Message(
        id="msg_1",
        content=[
            ToolUseBlock(
                id="tu_1", input=payload, name="BrowserUseOutputForRun", type="tool_use"
            )
        ],
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="tool_use",
        type="message",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class _FakeAnthropicClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


class TestAnthropicRoundTrip:
    """Anthropic is the default path since b0e7e52 (native constrained decoding)."""

    def _chat(self, **kwargs: Any):
        from spectragent.core.llm.providers.anthropic import ChatAnthropic

        return ChatAnthropic(model="claude-sonnet-4-5", api_key="test", **kwargs)

    async def test_native_output_config_round_trips(self, monkeypatch) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = self._chat()
        client = _FakeAnthropicClient(_anthropic_text_message(_PAYLOAD))
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=model)

        # Native constrained decoding, not the forced-tool fallback.
        assert "output_config" in client.calls[0]
        assert "tools" not in client.calls[0]
        assert isinstance(result.completion, BrowserUseOutput)
        assert agent.parse_react_output(result.completion) == [
            Action(name="browser.click", params={"index": 1})
        ]

    async def test_native_schema_carries_the_allowed_verbs(self, monkeypatch) -> None:
        """The verb set is Python-side only *except* via ``description``."""
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = self._chat()
        client = _FakeAnthropicClient(_anthropic_text_message(_PAYLOAD))
        monkeypatch.setattr(chat, "get_client", lambda: client)
        await chat.ainvoke([UserMessage(content="hi")], output_format=model)

        schema = client.calls[0]["output_config"]["format"]["schema"]
        assert "$defs" not in schema
        assert "browser.click" in schema["properties"]["action"]["description"]
        # Claude is not a strict-mode family — the required list stays honest.
        assert schema["required"] == ["action"]

    async def test_forced_tool_fallback_round_trips(self, monkeypatch) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = self._chat(use_native_structured_output=False)
        client = _FakeAnthropicClient(_anthropic_tool_message(_PAYLOAD))
        monkeypatch.setattr(chat, "get_client", lambda: client)

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=model)

        assert "tools" in client.calls[0]
        assert client.calls[0]["tool_choice"]["name"] == "BrowserUseOutputForRun"
        assert agent.parse_react_output(result.completion) == [
            Action(name="browser.click", params={"index": 1})
        ]

    async def test_fenced_json_is_unwrapped(self, monkeypatch) -> None:
        from anthropic.types import Message, TextBlock, Usage

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        fenced = f"```json\n{json.dumps(_PAYLOAD)}\n```"
        response = Message(
            id="m", content=[TextBlock(text=fenced, type="text")],
            model="claude-sonnet-4-5", role="assistant", stop_reason="end_turn",
            type="message", usage=Usage(input_tokens=1, output_tokens=1),
        )
        chat = self._chat()
        monkeypatch.setattr(chat, "get_client", lambda: _FakeAnthropicClient(response))

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=model)
        assert agent.parse_react_output(result.completion)

    async def test_provider_rejects_hallucinated_verb_before_the_agent_sees_it(
        self, monkeypatch
    ) -> None:
        """Validation happens in the provider — the loop never gets a bad model."""
        from spectragent.core.llm.exceptions import ModelProviderError

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        bad = {"memory": "m", "action": [{"totally_hallucinated_verb": {}}]}
        chat = self._chat()
        monkeypatch.setattr(
            chat, "get_client", lambda: _FakeAnthropicClient(_anthropic_text_message(bad))
        )

        with pytest.raises(ModelProviderError) as exc:
            await chat.ainvoke([UserMessage(content="hi")], output_format=model)
        assert "totally_hallucinated_verb" in str(exc.value)


class TestOllamaRoundTrip:
    """Local grammar backend — schema must be flat and self-contained."""

    async def test_round_trips_and_ships_a_flat_schema(self, monkeypatch) -> None:
        from spectragent.core.llm.providers.ollama import ChatOllama

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        calls: list[dict[str, Any]] = []

        class _Client:
            async def chat(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(_PAYLOAD)),
                    prompt_eval_count=1,
                    eval_count=1,
                )

        chat = ChatOllama(model="llama3.1")
        monkeypatch.setattr(chat, "get_client", lambda: _Client())

        result = await chat.ainvoke([UserMessage(content="hi")], output_format=model)

        assert "$defs" not in calls[0]["format"]
        assert agent.parse_react_output(result.completion) == [
            Action(name="browser.click", params={"index": 1})
        ]


class TestBedrockToolSchemaRoundTrip:
    def test_action_array_survives_the_converse_tool_schema(self) -> None:
        from spectragent.core.llm.providers.bedrock import ChatBedrock

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = ChatBedrock(model="anthropic.claude-3-5-sonnet-20240620-v1:0")
        schema = chat._format_tools_for_request(model)[0]["toolSpec"]["inputSchema"]["json"]

        # The array must keep its ``items`` — Bedrock used to strip them.
        assert schema["properties"]["action"]["type"] == "array"
        assert schema["properties"]["action"]["items"]["type"] == "object"
        assert "$defs" not in schema

    def test_tool_input_dict_round_trips(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        # Bedrock hands back the tool input as a dict, not a JSON string.
        assert agent.parse_react_output(model.model_validate(_PAYLOAD)) == [
            Action(name="browser.click", params={"index": 1})
        ]


# ── 6. Known gap surfaced by turning output_format on ─────────────


class TestOpenAIStrictModeGap:
    """The *ingredients* of the GPT-family + BrowserUseAgent strict-mode gap.

    ``action`` is an open ``{"<verb>": {<params>}}`` map, which compiles to
    an object node with no ``properties``. ``SchemaOptimizer`` deliberately
    leaves such nodes open (closing them was the tool-call-argument
    data-loss bug), and the GPT family genuinely does support the strict
    dialect — so, taken separately, ``"strict": true`` and the open node are
    each correct. Shipping them *together* is what OpenAI rejects.

    These three tests pin the ingredients and are unchanged. What the fix
    changed is the combination, which is pinned by
    :class:`TestOpenAIStrictModeDowngrade` below: ``_use_strict_schema`` now
    takes the output model and answers for that payload, so the request
    downgrades to ``strict: false`` instead of shipping an invalid schema.
    ``_use_strict_schema()`` with no argument still answers the
    model-capability question alone, which is what these tests ask.

    ``configs/browser_use_local.yaml`` pins ``gpt-4o``, so this is the
    documented path.
    """

    def _response_format(self, model: type[BaseModel]) -> dict[str, Any]:
        from spectragent.core.llm.providers.openai import ChatOpenAI
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        chat = ChatOpenAI(model="gpt-4o", api_key="test")
        strict = chat._use_strict_schema()
        return {
            "strict": strict,
            "schema": SchemaOptimizer.create_optimized_json_schema(model, strict=strict),
        }

    def test_strict_is_requested_for_gpt(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        assert self._response_format(model)["strict"] is True

    def test_open_action_map_is_not_strict_compatible(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        node = self._response_format(model)["schema"]["properties"]["action"]["items"]
        assert node["type"] == "object"
        # Both of these are what OpenAI strict requires and what an open map
        # cannot supply. The fix downgrades the *request*, never the schema —
        # re-closing this node is the tool-call-argument data-loss bug.
        assert "properties" not in node
        assert "additionalProperties" not in node

    def test_the_same_gap_predates_the_per_run_narrowing(self) -> None:
        """Not caused by F4 — the loose base model has the identical shape."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        base = SchemaOptimizer.create_optimized_json_schema(BrowserUseOutput, strict=True)
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        narrow = SchemaOptimizer.create_optimized_json_schema(model, strict=True)

        assert base["properties"]["action"]["items"] == narrow["properties"]["action"]["items"]


class TestOpenAIStrictModeDowngrade:
    """The fix: ``strict`` is decided by the compiled schema, not the model name.

    ``configs/browser_use_local.yaml`` pins ``gpt-4o`` and ``output_format`` is
    live, so an un-downgraded ``"strict": true`` alongside ``BrowserUseAgent``'s
    open ``action`` map is a request OpenAI rejects outright.

    The downgrade happens on the *request*. Re-closing the open node instead
    would reintroduce the bug where ``list[dict[str, Any]]`` compiled to a
    schema whose only legal value is ``[{}]``, discarding every tool call's
    arguments — so these tests also assert the schema stays open.
    """

    def _chat(self, model_name: str = "gpt-4o"):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        return ChatOpenAI(model=model_name, api_key="test")

    def test_capability_question_is_unchanged(self) -> None:
        """No argument = "does this model support strict at all"."""
        assert self._chat()._use_strict_schema() is True
        assert self._chat("meta-llama/Llama-3.3-70B")._use_strict_schema() is False

    def test_open_action_map_downgrades_strict(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        assert self._chat()._use_strict_schema(model) is False

    def test_closed_model_keeps_strict(self) -> None:
        """The downgrade is payload-specific, not a blanket disable."""

        class _Closed(BaseModel):
            a: str
            b: int

        assert self._chat()._use_strict_schema(_Closed) is True

    def test_non_strict_family_stays_false_even_for_closed_models(self) -> None:
        class _Closed(BaseModel):
            a: str

        assert self._chat("meta-llama/Llama-3.3-70B")._use_strict_schema(_Closed) is False

    async def test_request_ships_strict_false_with_an_open_schema(self) -> None:
        """End to end: the emitted response_format is internally consistent."""
        captured: dict[str, Any] = {}

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(_PAYLOAD), tool_calls=None
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = self._chat()
        chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        await chat.ainvoke([UserMessage(content="hi")], model)

        response_format = captured["response_format"]["json_schema"]
        assert response_format["strict"] is False

        node = response_format["schema"]["properties"]["action"]["items"]
        assert node["type"] == "object"
        assert "properties" not in node
        # The open map must stay open — closing it is the data-loss bug.
        assert "additionalProperties" not in node

    async def test_closed_model_still_ships_strict_true(self) -> None:
        captured: dict[str, Any] = {}

        class _Closed(BaseModel):
            a: str

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content='{"a": "x"}', tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        chat = self._chat()
        chat.get_client = lambda: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        await chat.ainvoke([UserMessage(content="hi")], _Closed)

        assert captured["response_format"]["json_schema"]["strict"] is True


class TestIsStrictCompatible:
    """``SchemaOptimizer.is_strict_compatible`` — the predicate behind the fix."""

    def test_closed_object_is_compatible(self) -> None:
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert SchemaOptimizer.is_strict_compatible(
            {"type": "object", "properties": {"a": {"type": "string"}}}
        )

    def test_open_map_is_not(self) -> None:
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible({"type": "object"})

    def test_typed_map_is_not(self) -> None:
        """``dict[str, int]`` compiles to a dict-valued additionalProperties."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(
            {"type": "object", "additionalProperties": {"type": "integer"}}
        )

    def test_nested_open_map_is_detected(self) -> None:
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            }
        )

    def test_required_list_is_not_walked_as_a_schema(self) -> None:
        """``required`` holds property *names*, not sub-schemas."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert SchemaOptimizer.is_strict_compatible(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
            }
        )
