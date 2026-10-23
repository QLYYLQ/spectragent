"""The verbs a Skyvern member's prompt teaches must be verbs the run accepts.

This is the general detector for a whole bug class, not one bug. Three
independent contracts have to agree, and nothing but a test forces them to:

1. the **template** — what vocabulary the model is told to emit;
2. ``parse_react_output`` — what ``Action.name`` that vocabulary becomes;
3. the **negotiated action set** — what ``guarded_act`` will let through.

They drifted completely apart once. The shipped template was upstream
Skyvern's, teaching ``"action_type": "CLICK"`` under an ``id`` key, while the
code emitted ``element_id`` and matched lower-case names. Every action the
model produced came out as ``Action(name='browser.CLICK')`` and was rejected
100% of the time — including ``COMPLETE``, the only verb the template offered
for finishing, so the agent could not terminate a task at all.

So the fixtures here are deliberately *derived*, never hand-written:

- the action set comes from each member's ``__wiring__`` negotiated against a
  real ``BrowserCapability``, through the same ``_action_rules`` /
  ``get_action_space`` / ``build_guarded_act`` path ``AgentRuntime.prepare``
  uses;
- the verb list is scraped back out of the *rendered* system prompt;
- each probe reply is written against the run's own closed reply schema, its
  params filled from the descriptor's ``params_schema``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent, SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.coordinate_frames import declared_space
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.action_spaces import space_of
from tests._helpers.assembly import make_agent, make_env
from tests._helpers.fragment_formats import dom_formats

#: Anything that looks like an action name where the prompt could be
#: teaching one: the rendered descriptor list (``- `browser.click(...)```),
#: prose references (``` `browser.done` ```), and any verb key in a JSON
#: example (``{"browser.click": {...}}``). Case-insensitive on purpose — an
#: UPPERCASE verb sneaking back into the template is precisely the
#: regression, so the scraper has to see it.
_BACKTICKED = re.compile(r"`([A-Za-z_]+\.[A-Za-z_]+)[(`]")
_JSON_VERB_KEY = re.compile(r'"([A-Za-z_]+\.[A-Za-z_]+)"\s*:\s*\{')

MEMBERS = [SkyvernDomAgent, SkyvernDomVisionAgent]


def _negotiated(member: type[SkyvernAgent]) -> tuple[list[ActionDescriptor], Mapping[str, str]]:
    """What ``AgentRuntime.prepare`` would hand ``prepare()`` and the guard."""
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    result = _action_rules(
        agent=extract_wiring(member, "action"),
        env_provides=set(capability.provided_actions),
    )
    return (
        env.get_action_space(active_actions=result.active_actions),
        result.action_bindings,
    )


def _taught_verbs(prompt: str) -> set[str]:
    return set(_BACKTICKED.findall(prompt)) | set(_JSON_VERB_KEY.findall(prompt))


def _probe_value(node: Mapping[str, Any]) -> Any:
    """A valid value for one params-schema node: the first enum member, or one of its type."""
    branches = node.get("anyOf") or [node]
    branch = next(b for b in branches if b.get("type") != "null")
    if branch.get("enum"):
        return branch["enum"][0]
    return {"integer": 1, "number": 1.0, "string": "x", "boolean": True}[branch["type"]]


def _probe_params(descriptor: ActionDescriptor) -> dict[str, Any]:
    """Every required param of *descriptor*, each with a valid value."""
    schema = descriptor.params_schema or {}
    properties = schema.get("properties") or {}
    return {name: _probe_value(properties[name]) for name in schema.get("required") or ()}


def _prepare(agent: SkyvernAgent, descriptors: list[ActionDescriptor]) -> str:
    seed = agent.prepare(
        task="t",
        action_space=space_of(descriptors, coordinate_space=declared_space(agent)),
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        fragment_formats=dom_formats(),
    )
    prompt = seed[0].content
    assert isinstance(prompt, str)
    return prompt


@pytest.fixture(params=MEMBERS, ids=lambda cls: cls.__registry_key__)
def prepared(request: Any) -> tuple[SkyvernAgent, str, list[ActionDescriptor], Any]:
    """A prepared member plus the real guarded_act built over its action set."""
    descriptors, bindings = _negotiated(request.param)
    agent = make_agent(request.param, memory=SimpleMemory(max_messages=100))
    prompt = _prepare(agent, descriptors)

    async def _accept(action: Action, **_kw: Any) -> ActionResult:
        return ActionResult(success=True, extracted_content=action.name)

    _active, guarded_act = build_guarded_act(_accept, descriptors, bindings=bindings)
    return agent, prompt, descriptors, guarded_act


class TestPromptVocabulary:
    def test_the_prompt_teaches_a_non_empty_vocabulary(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Guards the guard: an empty scrape would make everything below vacuous."""
        _agent, prompt, descriptors, _guard = prepared
        assert _taught_verbs(prompt), "scraped no action names out of the rendered prompt"
        assert len(descriptors) >= 3

    def test_every_negotiated_action_reaches_the_prompt(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Nothing the run can execute is hidden from the model."""
        _agent, prompt, descriptors, _guard = prepared
        assert {d.name for d in descriptors} <= _taught_verbs(prompt)

    async def test_every_taught_verb_survives_parse_and_guard(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """The load-bearing assertion: template → closed schema → parser → guard, per verb."""
        agent, prompt, descriptors, guarded_act = prepared
        by_name = {d.name: d for d in descriptors}
        for verb in sorted(_taught_verbs(prompt)):
            assert verb in by_name, f"the prompt teaches {verb!r}, which this run never negotiated"
            reply = {
                "actions": [{"reasoning": "probe", "action": {verb: _probe_params(by_name[verb])}}]
            }
            actions = agent.parse_react_output(agent.get_react_output_model().model_validate(reply))
            assert [a.name for a in actions] == [verb]
            result = await guarded_act(actions[0])
            assert result.success, f"{verb!r} parses, and guarded_act rejects it: {result.error}"

    async def test_the_agent_can_finish(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Finishing is the negotiated ``done`` written as an action, its answer intact.

        The old template's only way to finish was a ``"COMPLETE"`` action,
        which parsed to ``browser.COMPLETE`` and was rejected — the agent was
        structurally incapable of ending a task.
        """
        agent, prompt, _descriptors, guarded_act = prepared
        assert "`browser.done`" in prompt and "extracted_content" in prompt

        (action,) = agent.parse_react_output(
            '{"actions": [{"reasoning": "found it", "action": '
            '{"browser.done": {"extracted_content": "42", "success": false}}}]}'
        )
        assert action == Action(
            name="browser.done", params={"extracted_content": "42", "success": False}
        )
        result = await guarded_act(action)
        assert result.success

    @pytest.mark.parametrize("member", MEMBERS, ids=lambda cls: cls.__registry_key__)
    def test_the_terminal_and_example_actions_are_named_from_the_negotiated_set(
        self, member: type[SkyvernAgent]
    ) -> None:
        """The template names ``done`` and its example verb by rendering them, not by prose.

        Two vocabularies in one prompt, one of them hardcoded, is the defect
        this file was created for.
        """
        agent = make_agent(member, memory=SimpleMemory(max_messages=100))
        elsewhere = [
            ActionDescriptor(
                name="desktop.click",
                description="Click.",
                params={"index": "int"},
                params_schema={
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                },
            ),
            ActionDescriptor(
                name="desktop.done",
                description="Finish.",
                params_schema={"type": "object", "properties": {}, "required": []},
            ),
        ]
        prompt = _prepare(agent, elsewhere)

        assert "`desktop.done`" in prompt
        assert '{"desktop.click": {"index": 42}}' in prompt
        assert "browser." not in prompt
        assert _taught_verbs(prompt) == {"desktop.click", "desktop.done"}

    def test_the_reply_schema_carries_every_negotiated_action_with_its_own_params(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Each advertised action is a branch of the closed union, with exactly its params.

        The flat item this replaces offered ``index``, ``text``, ``direction``
        and ``keys`` to every action: a scroll's ``amount`` could not be said,
        and a click could carry a ``text`` the click does not take.
        """
        agent, _prompt, descriptors, _guard = prepared
        model = agent.get_react_output_model()
        for descriptor in descriptors:
            reply = {"actions": [{"action": {descriptor.name: _probe_params(descriptor)}}]}
            assert model.model_validate(reply), descriptor.name
            stray = {"actions": [{"action": {descriptor.name: {"not_a_param": 1}}}]}
            with pytest.raises(ValueError, match="not_a_param"):
                model.model_validate(stray)
