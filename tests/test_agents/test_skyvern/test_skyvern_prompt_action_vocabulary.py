"""The verbs SkyvernAgent's prompt teaches must be verbs the run accepts.

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
for finishing, so the agent could not terminate a task at all. The suite
stayed green because every Skyvern test asserted the code's assumptions back
at itself: an empty action list, hand-written lower-case verbs, never the
shipped template.

So the fixtures here are deliberately *derived*, never hand-written:

- the action set comes from ``SkyvernAgent.__wiring__`` negotiated against a
  real ``BrowserCapability``, through the same ``_action_rules`` /
  ``get_action_space`` / ``build_guarded_act`` path ``AgentRuntime.prepare``
  uses;
- the verb list is scraped back out of the *rendered* system prompt.

A hand-written list of verbs here would re-create the exact blind spot this
file exists to close.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.skyvern.output_models import SkyvernActionItem, SkyvernOutput
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.action_spaces import space_of
from tests._helpers.assembly import make_agent, make_env

#: Anything that looks like an action name where the prompt could be
#: teaching one: the rendered descriptor list (``- `browser.click(...)```),
#: prose references (``` `browser.done` ```), and any literal
#: ``"action_type": "..."`` in a JSON example. Case-insensitive on purpose —
#: an UPPERCASE verb sneaking back into the template is precisely the
#: regression, so the scraper has to see it in order for the assertions
#: below to reject it.
_BACKTICKED = re.compile(r"`([A-Za-z_]+\.[A-Za-z_]+)[(`]")
_ACTION_TYPE_LITERAL = re.compile(r'"action_type"\s*:\s*"([^"]+)"')


def _negotiated() -> tuple[list[ActionDescriptor], Mapping[str, str]]:
    """What ``AgentRuntime.prepare`` would hand ``prepare()`` and the guard.

    Both halves, because both are negotiation output: the descriptors the
    prompt advertises *and* the short → fully-qualified bindings the guard
    resolves against. Building the guard without the bindings would test a
    configuration the runtime never assembles.
    """
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    result = _action_rules(
        agent=extract_wiring(SkyvernAgent, "action"),
        env_provides=set(capability.provided_actions),
    )
    return (
        env.get_action_space(active_actions=result.active_actions),
        result.action_bindings,
    )


def _taught_verbs(prompt: str) -> set[str]:
    return set(_BACKTICKED.findall(prompt)) | set(_ACTION_TYPE_LITERAL.findall(prompt))


@pytest.fixture
def prepared() -> tuple[SkyvernAgent, str, list[ActionDescriptor], Any]:
    """A prepared agent plus the real guarded_act built over its action set."""
    descriptors, bindings = _negotiated()
    agent = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=100))
    prompt = agent.prepare(
        task="t",
        action_space=space_of(descriptors),
        execution_limits=ExecutionLimits(max_actions_per_step=1),
    )[0].content
    assert isinstance(prompt, str)

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
        assert _taught_verbs(prompt), (
            "scraped no action names out of the rendered prompt — either the "
            "template stopped rendering the action space, or the scraper "
            "regexes no longer match its format"
        )
        assert len(descriptors) >= 3

    def test_every_negotiated_action_reaches_the_prompt(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Nothing the run can execute is hidden from the model.

        The template used to reference twelve variables the agent never
        supplied and ignore ``action_space`` outright, so the negotiated
        set reached zero of the 76 shipped templates.
        """
        _agent, prompt, descriptors, _guard = prepared
        assert {d.name for d in descriptors} <= _taught_verbs(prompt)

    async def test_every_taught_verb_survives_parse_and_guard(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """The load-bearing assertion: template → parser → guard, per verb."""
        agent, prompt, _descriptors, guarded_act = prepared
        for verb in sorted(_taught_verbs(prompt)):
            actions = agent.parse_react_output(
                SkyvernOutput(
                    actions=[SkyvernActionItem(action_type=verb, index=1, reasoning="probe")]
                )
            )
            assert len(actions) == 1, verb
            result = await guarded_act(actions[0])
            assert result.success, (
                f"the prompt teaches {verb!r}, which parses to "
                f"{actions[0].name!r} — and guarded_act rejects it: "
                f"{result.error}"
            )

    async def test_the_agent_can_finish(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Termination is a verb like any other and gets the same treatment.

        The old template's only way to finish was a ``"COMPLETE"`` action,
        which parsed to ``browser.COMPLETE`` and was rejected — the agent
        was structurally incapable of ending a task. The template now
        finishes through ``is_done``; this pins that it lands on an action
        the guard accepts, with the answer intact.
        """
        agent, prompt, _descriptors, guarded_act = prepared
        assert "is_done" in prompt and "extracted_content" in prompt

        actions = agent.parse_react_output(
            SkyvernOutput(actions=[], is_done=True, extracted_content="42")
        )
        assert len(actions) == 1
        result = await guarded_act(actions[0])
        assert result.success
        assert actions[0].params["extracted_content"] == "42"

    def test_the_terminal_action_is_named_from_the_negotiated_set(self) -> None:
        """The template names ``done``'s provider by rendering it, not by prose.

        The rewritten template still spelled ``browser.done`` into its
        prose and told the model that ``action_type`` carries a
        ``browser.`` prefix — while ``{{ action_space }}`` right above it
        renders whatever was negotiated. Two vocabularies in one prompt,
        one of them hardcoded, is the defect this file was created for;
        it survived the rewrite in the paragraphs around the list.
        """
        agent = make_agent(SkyvernAgent, memory=SimpleMemory(max_messages=100))
        elsewhere = [
            ActionDescriptor(
                name="desktop.click",
                description="Click.",
                params={"index": "int"},
            ),
            ActionDescriptor(name="desktop.done", description="Finish."),
        ]
        prompt = agent.prepare(
            task="t",
            action_space=space_of(elsewhere),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )[0].content
        assert isinstance(prompt, str)

        assert "`desktop.done`" in prompt
        assert "browser.done" not in prompt
        assert "`browser.` prefix" not in prompt

    def test_no_param_key_is_advertised_that_the_output_model_cannot_carry(
        self, prepared: tuple[SkyvernAgent, str, list[ActionDescriptor], Any]
    ) -> None:
        """Required params must be expressible in ``SkyvernActionItem``.

        Advertising ``browser.send_keys(keys: str)`` while the output model
        has no ``keys`` field is the same defect one layer down: the model
        emits the action, the guard passes it, and ``SendKeysAction`` returns
        ``Missing param: 'keys'`` every single time.
        """
        _agent, _prompt, descriptors, _guard = prepared
        carriable = set(SkyvernActionItem.model_fields) - {"action_type", "reasoning"}
        # ``done`` is invoked through the top-level fields, not an item.
        for descriptor in descriptors:
            if descriptor.name == "browser.done":
                continue
            required = {
                key
                for key, spec in descriptor.params.items()
                if "optional" not in str(spec).lower()
            }
            assert required <= carriable, (
                f"{descriptor.name} needs {sorted(required - carriable)}, which "
                f"SkyvernActionItem cannot express — the model is being asked "
                f"for a param it has no field for"
            )
