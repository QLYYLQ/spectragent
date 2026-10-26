"""Structured output, end to end.

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

import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
from spectragent.agents.browser_use import BrowserUseDomAgent
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.observations import BrowserPlatformObs
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop, serialize_llm_output
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_spaces import space_of
from tests._helpers.fragment_formats import dom_formats
from tests._helpers.llm_clients import FakeClient
from tests._helpers.replies import read_reply
from tests._helpers.trace_builder import as_chat

# ── Shared fixtures ───────────────────────────────────────────────


def _descriptors(*keys: str) -> list[ActionDescriptor]:
    """Real descriptors — the closed union is compiled from ``params_schema``."""
    keys = keys or ("browser.click", "browser.navigate", "browser.done")
    return [ActionRegistrar.get(key)().descriptor() for key in keys]


def _prepared_agent() -> BrowserUseDomAgent:
    agent = BrowserUseDomAgent()
    agent.prepare(
        task="t",
        execution_limits=ReactLoop().execution_limits(),
        fragment_formats=dom_formats(),
        action_space=space_of(_descriptors()),
    )
    return agent


_PAYLOAD = {"memory": "m", "action": [{"browser.click": {"index": 1}}]}


# ── 1. The acceptance criterion ─────────────────────────────────────


class TestPerRunModelRoundTrip:
    """§6.1.2 — the per-run model must survive parse_react_output."""

    def test_structured_output_round_trips_to_actions(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        out = model.model_validate(_PAYLOAD)
        assert agent.parse_react_output(out) == [Action(name="browser.click", params={"index": 1})]

    def test_the_wire_shape_is_upstream_browser_use_s(self) -> None:
        """A validated item is a model now, and dumps back to the same JSON.

        The shape is not an implementation detail: the fine-tuned
        browser-use checkpoints were trained on
        ``{"action":[{"<verb>":{...}}]}``, and it is also the only shape
        whose schema stays inside the intersection of the three provider
        dialects — the verb is a property *name*, so nothing but
        ``object``/``properties``/``required``/``anyOf`` is needed.
        """
        agent = _prepared_agent()
        model = agent.get_react_output_model()

        out = model.model_validate(_PAYLOAD)
        assert json.loads(out.model_dump_json())["action"] == _PAYLOAD["action"]

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

    def test_the_verb_the_schema_permits_is_the_verb_env_act_routes_on(self) -> None:
        """One spelling, end to end — no alias table, no local-id fallback."""
        agent = _prepared_agent()
        model = agent.get_react_output_model()

        for alias in ("click_element", "click"):
            with pytest.raises(ValidationError):
                model.model_validate({"memory": "m", "action": [{alias: {"index": 4}}]})

    def test_done_names_its_answer_field_once(self) -> None:
        """``done.text`` → ``extracted_content`` used to happen in the parser.

        Three surfaces then had three names for one value: the template
        said ``text``, the action read ``extracted_content``, and the
        parser bridged them where nothing could see it.
        """
        agent = _prepared_agent()
        model = agent.get_react_output_model()

        out = model.model_validate(
            {"memory": "m", "action": [{"browser.done": {"extracted_content": "the answer"}}]}
        )
        assert agent.parse_react_output(out) == [
            Action(name="browser.done", params={"extracted_content": "the answer"})
        ]
        with pytest.raises(ValidationError):
            model.model_validate(
                {"memory": "m", "action": [{"browser.done": {"text": "the answer"}}]}
            )


# ── 2. Hallucinated verbs die at parse time, not at guarded_act ───


class TestHallucinatedVerbRejection:
    """§6.1.1 — the per-run verb set must bite on the text path too.

    The text path is a reply the provider refused, which the framework
    validates against the same per-run model (``validated_reply``).
    """

    def test_text_path_rejects_hallucinated_verb(self) -> None:
        agent = _prepared_agent()
        raw = json.dumps({"memory": "m", "action": [{"totally_hallucinated_verb": {}}]})
        with pytest.raises(ParseError) as exc:
            read_reply(agent, raw)

        detail = exc.value.detail
        assert "action 1, totally_hallucinated_verb is not an action this run executes" in detail
        # The allowed set rides along so the LLM can self-correct next turn.
        assert "browser.click" in detail

    def test_structured_path_rejects_hallucinated_verb(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        with pytest.raises(ValidationError) as exc:
            model.model_validate({"memory": "m", "action": [{"totally_hallucinated_verb": {}}]})
        assert "unknown action 'totally_hallucinated_verb'" in str(exc.value)

    def test_negotiated_out_verb_is_rejected(self) -> None:
        """A real action the capability did not offer this run."""
        agent = BrowserUseDomAgent()
        agent.prepare(
            task="t",
            execution_limits=ReactLoop().execution_limits(),
            fragment_formats=dom_formats(),
            action_space=space_of(_descriptors("browser.click")),
        )
        with pytest.raises(ParseError, match="action 1, browser.navigate is not an action"):
            read_reply(
                agent, json.dumps({"memory": "m", "action": [{"browser.navigate": {"url": "u"}}]})
            )

    def test_before_prepare_there_is_nothing_to_validate_against(self) -> None:
        """No ``prepare()`` → no negotiated set → refuse, loudly.

        The loose fallback that used to stand in here is how an open
        ``list[dict[str, Any]]`` schema kept reaching providers unnoticed.
        """
        agent = BrowserUseDomAgent()
        with pytest.raises(RuntimeError, match="before prepare"):
            agent.parse_react_output(_PAYLOAD)  # type: ignore[arg-type]


# ── 3. ReactLoop actually routes output_format ────────────────────


class _FakeEnv:
    """Env whose observation carries just enough for a real agent to render."""

    env_type = "fake"

    async def observe(self, *_a: Any, **_kw: Any) -> Any:
        return SimpleNamespace(
            task=SimpleNamespace(user_request="task", follow_ups=[]),
            platform=BrowserPlatformObs(operating_system="macos"),
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

    inference_kind = "chat"

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

    async def build_react_prompt(self, _obs: Any, *, context: ReactPromptContext) -> list:
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
        "action_space": ActionSpace(),
        "session_id": None,
        "task_id": "t",
    }
    return asyncio.run(loop._execute(agent, "task", ctx=ctx))


class TestReactLoopRoutesOutputFormat:
    """§6.1.1 — the computed output model must reach the provider."""

    def test_agents_output_model_reaches_ainvoke(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        llm = _RecordingLLM(
            model.model_validate(  # type: ignore[union-attr]
                {"memory": "m", "action": [{"browser.done": {"extracted_content": "ok"}}]}
            )
        )

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

            async def build_react_prompt(self, _obs: Any, *, context: ReactPromptContext) -> list:
                return [UserMessage(content="go")]

            def parse_react_output(self, _o: Any) -> list[Action]:
                return [Action(name="browser.done", params={})]

        llm = _RecordingLLM("free text")
        _run_loop(_NoSlotAgent(), llm)
        assert llm.seen_output_formats == [None]


# ── 4. Serialization of a structured completion ───────────────────


class TestCompletionSerialization:
    """The success and ParseError branches must agree."""

    def test_structured_completion_serializes_as_json_not_repr(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        completion = model.model_validate(_PAYLOAD)

        text = serialize_llm_output(completion)
        assert json.loads(text)["action"] == [{"browser.click": {"index": 1}}]

    def test_text_completion_passes_through(self) -> None:
        assert serialize_llm_output("plain text") == "plain text"

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
                    "env": env,
                    "llm": ok_llm,
                    "memory": ok_memory,
                    "act_fn": env.act,
                    "action_space": ActionSpace(),
                    "session_id": None,
                    "task_id": "t",
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
                    "env": env,
                    "llm": _RecordingLLM(completion),
                    "memory": bad_memory,
                    "act_fn": env.act,
                    "action_space": ActionSpace(),
                    "session_id": None,
                    "task_id": "t",
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
            ToolUseBlock(id="tu_1", input=payload, name="BrowserUseOutputForRun", type="tool_use")
        ],
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="tool_use",
        type="message",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class _FakeAnthropicClient(FakeClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
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
        assert isinstance(result.completion, model)
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
            id="m",
            content=[TextBlock(text=fenced, type="text")],
            model="claude-sonnet-4-5",
            role="assistant",
            stop_reason="end_turn",
            type="message",
            usage=Usage(input_tokens=1, output_tokens=1),
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
        # ``items`` is now the closed union, so it is an ``anyOf`` of
        # single-key objects rather than a bare ``{"type": "object"}``.
        assert schema["properties"]["action"]["type"] == "array"
        branches = schema["properties"]["action"]["items"]["anyOf"]
        assert {next(iter(b["properties"])) for b in branches} == {
            "browser.click",
            "browser.navigate",
            "browser.done",
        }
        assert "$defs" not in schema

    def test_tool_input_dict_round_trips(self) -> None:
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        # Bedrock hands back the tool input as a dict, not a JSON string.
        assert agent.parse_react_output(model.model_validate(_PAYLOAD)) == [
            Action(name="browser.click", params={"index": 1})
        ]


# ── 6. The strict-mode gap, now closed ────────────────────────────


class TestOpenAIStrictModeGap:
    """The gap this file was written to document, and its repair.

    ``action`` used to be an open ``{"<verb>": {<params>}}`` map, which
    compiles to an object node with no ``properties``. ``SchemaOptimizer``
    leaves such nodes open (closing them was the tool-call-argument
    data-loss bug), so the GPT family — which genuinely supports strict —
    had to ship ``strict: false``, and Gemini behind an OpenAI-compatible
    gateway, which is *not* asked for strict at all, answered ``[{}]``.

    Nothing about the optimizer changed. The *model* did: ``action`` is a
    closed union of single-key objects, every one of which declares its
    properties, so the compiled schema satisfies the strict dialect and
    both problems disappear at once.
    """

    def _response_format(self, model: type[BaseModel]) -> dict[str, Any]:
        from spectragent.core.llm.providers.openai import ChatOpenAI
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        chat = ChatOpenAI(model="gpt-4o", api_key="test")
        strict = chat.use_strict_schema(model)
        return {
            "strict": strict,
            "schema": SchemaOptimizer.create_optimized_json_schema(model, strict=strict),
        }

    def test_strict_is_requested_for_gpt(self) -> None:
        agent = _prepared_agent()
        assert self._response_format(agent.get_react_output_model())["strict"] is True

    def test_every_action_branch_declares_its_properties(self) -> None:
        agent = _prepared_agent()
        node = self._response_format(agent.get_react_output_model())["schema"]["properties"][
            "action"
        ]["items"]

        assert "oneOf" not in node
        branches = node["anyOf"]
        assert {next(iter(b["properties"])) for b in branches} == {
            "browser.click",
            "browser.navigate",
            "browser.done",
        }
        # Exactly one required property per branch — that is what makes the
        # verb the *key* rather than a value the model has to get right.
        assert all(len(b["required"]) == 1 for b in branches)

    def test_the_whole_schema_is_strict_clean(self) -> None:
        """No ``$ref``, no ``oneOf``, no object without ``properties``."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        agent = _prepared_agent()
        schema = self._response_format(agent.get_react_output_model())["schema"]
        assert SchemaOptimizer.is_strict_compatible(schema)

        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text and "oneOf" not in text


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
        assert self._chat().use_strict_schema() is True
        assert self._chat("meta-llama/Llama-3.3-70B").use_strict_schema() is False

    def test_the_closed_action_union_keeps_strict(self) -> None:
        """The regression guard: reopening ``action`` silently loses strict."""
        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None
        assert self._chat().use_strict_schema(model) is True

    def test_closed_model_keeps_strict(self) -> None:
        """The downgrade is payload-specific, not a blanket disable."""

        class _Closed(BaseModel):
            a: str
            b: int

        assert self._chat().use_strict_schema(_Closed) is True

    def test_non_strict_family_stays_false_even_for_closed_models(self) -> None:
        class _Closed(BaseModel):
            a: str

        assert self._chat("meta-llama/Llama-3.3-70B").use_strict_schema(_Closed) is False

    async def test_request_ships_strict_true_with_the_closed_schema(self) -> None:
        """End to end: the emitted response_format is internally consistent."""
        captured: dict[str, Any] = {}

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(_PAYLOAD), tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        assert model is not None

        chat = self._chat()
        chat.get_client = lambda: FakeClient(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=_Completions())
        )
        await chat.ainvoke([UserMessage(content="hi")], model)

        response_format = captured["response_format"]["json_schema"]
        assert response_format["strict"] is True

        schema = response_format["schema"]
        node = schema["properties"]["action"]
        branches = node["items"]["anyOf"]
        assert all(b["additionalProperties"] is False for b in branches)
        assert all(b["properties"] for b in branches)
        # The request is where a refused keyword would actually cost
        # something: ``minItems`` is not in the strict dialect, and this is
        # the first payload in the codebase that ships ``strict: true``
        # with a list field that has a Pydantic length bound.
        assert "minItems" not in json.dumps(schema)
        assert "minItems" not in node

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
        chat.get_client = lambda: FakeClient(  # type: ignore[method-assign]
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

    def test_one_of_is_not(self) -> None:
        """No structured-output dialect accepts ``oneOf``.

        A Pydantic ``Discriminator`` compiles a union to ``oneOf``, and this
        predicate used to walk straight past it and answer ``True`` — so the
        request went out ``strict: true`` and the API refused it, with
        nothing in the codebase pointing at why. It is also why the closed
        action union is a plain ``Union``.
        """
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(
            {"oneOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}
        )

    @pytest.mark.parametrize(
        "node",
        [
            pytest.param({"type": "array", "minItems": 1}, id="minItems"),
            pytest.param({"type": "array", "maxItems": 3}, id="maxItems"),
            pytest.param({"type": "array", "uniqueItems": True}, id="uniqueItems"),
            pytest.param({"allOf": [{"type": "object", "properties": {}}]}, id="allOf"),
            pytest.param({"not": {"type": "string"}}, id="not"),
            pytest.param(
                {
                    "type": "object",
                    "properties": {},
                    "patternProperties": {"^x": {"type": "string"}},
                },
                id="patternProperties",
            ),
            pytest.param(
                {"type": "object", "properties": {}, "dependentSchemas": {}},
                id="dependentSchemas",
            ),
        ],
    )
    def test_any_keyword_outside_the_allowlist_is_not(self, node: dict[str, Any]) -> None:
        """The predicate is an allowlist, so it fails closed.

        ``oneOf``/``discriminator`` were once handled by name, which meant
        every *other* unsupported keyword answered ``True`` and shipped. An
        unknown keyword now costs constrained decoding (``strict: false``,
        which every endpoint accepts) instead of a 400 on the schema.

        Of the cases below, ``minItems`` is the one a production caller
        cannot reach: both call sites compile with ``strict=True`` first,
        and that compile already removes it (the rejection is fixed in the
        compiler, not here). It is kept as a case because the predicate is
        also a public answer to "is this schema strict-legal" — but the
        keywords this actually rescues in production are the rest, led by
        ``maxItems``/``uniqueItems``, which survive a strict compile
        untouched.
        """
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(node)

    def test_a_property_named_like_a_keyword_is_still_fine(self) -> None:
        """``properties`` keys are names, not keywords.

        The allowlist has to know where it is; checking the contents of a
        ``properties`` map against it would refuse a schema for the crime
        of having a field called ``items``.
        """
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert SchemaOptimizer.is_strict_compatible(
            {
                "type": "object",
                "properties": {
                    "items": {"type": "string"},
                    "oneOf": {"type": "integer"},
                    "browser.click": {"type": "object", "properties": {}},
                },
                "required": ["items"],
            }
        )

    def test_a_discriminator_is_not_either(self) -> None:
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "discriminator": {"propertyName": "a"},
            }
        )

    def test_a_nested_one_of_is_detected(self) -> None:
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert not SchemaOptimizer.is_strict_compatible(
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "array",
                        "items": {"oneOf": [{"type": "object", "properties": {}}]},
                    }
                },
            }
        )

    def test_an_any_of_union_stays_compatible(self) -> None:
        """The shape the closed action union actually compiles to."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        assert SchemaOptimizer.is_strict_compatible(
            {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"browser.click": {"type": "object", "properties": {}}},
                    },
                ]
            }
        )


# ── 8. A reply that does not fit the schema is the model's to fix ──


class TestStructuredValidationIsFedBack:
    """``StructuredOutputValidationError`` rejoins the ordinary parse path.

    Before: a provider that could not validate a reply raised a generic
    ``ModelProviderError``, and ``ReactLoop`` cannot tell that apart from a
    dead endpoint — so it aborted the run at the LLM phase. A model that
    wrote one wrong parameter ended the episode, while the same mistake in
    free text came back as a ``ParseError`` and was fed back for the model
    to fix. Now both go to the model.
    """

    def _chat(self, **kwargs: Any):
        from spectragent.core.llm.providers.openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o", api_key="test", **kwargs)

    def _client(self, content: str) -> Any:
        class _Completions:
            async def create(self, **_kw: Any) -> Any:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content, tool_calls=None),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        return FakeClient(chat=SimpleNamespace(completions=_Completions()))

    async def test_the_provider_raises_the_recoverable_error(self) -> None:
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        agent = _prepared_agent()
        model = agent.get_react_output_model()
        bad = json.dumps({"memory": "m", "action": [{"nope": {}}]})

        chat = self._chat()
        chat.get_client = lambda: self._client(bad)  # type: ignore[method-assign]
        with pytest.raises(StructuredOutputValidationError) as exc:
            await chat.ainvoke([UserMessage(content="hi")], model)

        # It carries the model's own text, so the Loop can word the refusal
        # from it: every mistake located, in the voice of a params refusal.
        assert exc.value.raw_output == bad
        assert exc.value.status_code == 422

    def test_the_loop_feeds_it_back_instead_of_aborting(self) -> None:
        """One step, one correction message, run continues to max_steps."""
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        agent = _prepared_agent()
        bad = json.dumps({"memory": "m", "action": [{"nope": {}}]})
        parsed: list[Any] = []
        parse = agent.parse_react_output

        def spy(reply: Any) -> list[Action]:
            parsed.append(reply)
            return parse(reply)

        agent.parse_react_output = spy  # type: ignore[method-assign]

        class _RaisingLLM:
            inference_kind = "chat"
            model = "gpt-4o"

            async def ainvoke(self, _messages: Any, output_format: Any = None) -> Any:
                raise StructuredOutputValidationError(
                    "schema mismatch", raw_output=bad, model="gpt-4o"
                )

        memory = _FakeMemory()
        loop = ReactLoop()
        loop.max_steps = 1
        env = _FakeEnv()
        result = asyncio.run(
            loop._execute(
                agent,
                "task",
                ctx={
                    "env": env,
                    "llm": _RaisingLLM(),
                    "memory": memory,
                    "act_fn": env.act,
                    "action_space": space_of(_descriptors()),
                    "session_id": None,
                    "task_id": "t",
                },
            )
        )

        # Not an abort at the LLM phase: the loop ran its step, recorded it,
        # and told the model what was wrong.
        assert result.agent_done.value == "max_steps"
        feedback = [
            m.content
            for m in memory.messages
            if getattr(m, "role", "") == "user" and "[SYSTEM]" in str(m.content)
        ]
        assert feedback, "the model was never told its reply did not parse"
        # The framework worded it, as ReAct's name check words an unknown
        # action; the agent's parser never saw the refused reply.
        assert (
            "action 1, nope is not an action this run executes "
            "(its actions: browser.click, browser.done, browser.navigate)"
        ) in str(feedback[-1])
        assert parsed == []

    def test_a_generic_provider_error_still_ends_the_run(self) -> None:
        """The other half of the pair — and the reason the type matters.

        Feeding *every* provider error back would have the loop argue with
        a dead endpoint for max_steps. A 429 is not the model's to fix, so
        it still aborts at the llm phase with nothing said to the model.
        """
        from spectragent.core.llm.exceptions import ModelProviderError

        agent = _prepared_agent()

        class _DeadLLM:
            inference_kind = "chat"
            model = "gpt-4o"

            async def ainvoke(self, _messages: Any, output_format: Any = None) -> Any:
                raise ModelProviderError("rate limited upstream", status_code=429, model="gpt-4o")

        memory = _FakeMemory()
        loop = ReactLoop()
        loop.max_steps = 3
        env = _FakeEnv()
        result = asyncio.run(
            loop._execute(
                agent,
                "task",
                ctx={
                    "env": env,
                    "llm": _DeadLLM(),
                    "memory": memory,
                    "act_fn": env.act,
                    "action_space": ActionSpace(),
                    "session_id": None,
                    "task_id": "t",
                },
            )
        )

        assert result.agent_done.value == "error"
        assert "llm failed at step 0" in (result.error or "")
        assert "ModelProviderError" in (result.error or "")
        assert not [
            m
            for m in memory.messages
            if getattr(m, "role", "") == "user" and "[SYSTEM]" in str(m.content)
        ], "a transport failure was argued back to the model"

    def test_the_failed_call_is_still_in_the_trace(self, tmp_path: Any) -> None:
        """Recoverable does not mean invisible.

        ``after`` advice does not fire when the target raises, so a call
        the run *continues past* would leave a step with no record of it —
        and absent for exactly the steps a researcher wants to read.
        ``around`` advice records it; this walks the real Weaver to prove
        the wiring, not just the handler.
        """
        from spectragent.core.aop.weaver import Weaver
        from spectragent.core.hooks.trace_writer import TraceWriter
        from spectragent.core.llm.exceptions import StructuredOutputValidationError
        from spectragent.core.llm.views import ModelInvokeUsage
        from spectragent.core.step_recorder import StepRecorder
        from spectragent.core.trace.reader import RunTrace

        agent = _prepared_agent()
        bad = json.dumps({"memory": "m", "action": [{"nope": {}}]})

        class _RaisingLLM:
            inference_kind = "chat"
            model = "gpt-4o"

            async def ainvoke(self, _messages: Any, output_format: Any = None) -> Any:
                raise StructuredOutputValidationError(
                    "schema mismatch",
                    raw_output=bad,
                    model="gpt-4o",
                    usage=ModelInvokeUsage(
                        prompt_tokens=10,
                        prompt_cached_tokens=0,
                        prompt_cache_creation_tokens=None,
                        prompt_image_tokens=None,
                        completion_tokens=5,
                        total_tokens=15,
                    ),
                )

        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        weaver = Weaver([writer])
        llm = weaver.wrap(_RaisingLLM(), "llm")

        memory = _FakeMemory()
        loop = ReactLoop()
        loop.max_steps = 1
        env = _FakeEnv()

        async def _run() -> Any:
            await writer.on_run_start(agent=agent, env=env, task="task", config={})
            # The step recorder the runtime wraps under ``role="loop"`` —
            # its ``record_step`` is TraceWriter's commit point.
            recorder = weaver.wrap(StepRecorder(), "loop")
            result = await loop._execute(
                agent,
                "task",
                ctx={
                    "env": env,
                    "llm": llm,
                    "memory": memory,
                    "act_fn": env.act,
                    "action_space": ActionSpace(),
                    "session_id": None,
                    "task_id": "t",
                    "step_recorder": recorder,
                },
            )
            await writer.on_run_end(result=result)
            return result

        result = asyncio.run(_run())

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        # Committed with the step it belongs to, not left trailing.
        assert trace.step_count == 1
        assert trace.trailing_calls == []
        (call,) = trace.llm_messages(0)
        assert as_chat(call).messages, "the prompt that produced the refused reply was not recorded"
        assert call.status == "failed"
        assert as_chat(call).completion is None
        assert call.failure is not None
        assert call.failure.error == "StructuredOutputValidationError"
        assert call.failure.raw_output == bad
        assert call.failure.usage is not None
        assert call.failure.usage.total_tokens == 15
        # And what it cost is counted once on both sides, rather than only
        # by the loop that kept going.
        from spectragent.core.trace.usage import recorded_usage

        assert recorded_usage(trace).calls == 1
        assert recorded_usage(trace).tokens == 15
        assert result.metrics.llm_calls == 1
