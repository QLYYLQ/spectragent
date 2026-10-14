"""Every verb UI-TARS's prompt teaches must reach a real action, executable.

The Skyvern sibling of this file (``test_skyvern_prompt_action_vocabulary``)
pins template → parser → guard. UI-TARS needs one more link, because its
failure was one layer deeper: the verbs *did* reach the guard and then
died inside the action class on a parameter contract nobody had checked.

``browser.type`` was declared required, activated by negotiation and
100% unexecutable on two independent counts — the parser emitted
``{content, point}`` so ``TypeAction`` answered ``Missing param:
'index'``, and even with an index it would have typed the empty string,
because the agent wrote ``content`` where the action reads ``text``. Four
more prompt-taught verbs (``hotkey``, ``drag``, ``wait``, plus the mobile
set) were rejected by ``guarded_act`` outright, once per step, silently.

So the chain here is the full one:

    shipped .md → rendered prompt → scraped call → parse_react_output
      → guarded_act → the real ActionBase subclass

with the fixtures derived at every stage. The sample call for each verb
is built by substituting concrete values into the *signature the prompt
shows the model* — so a signature that teaches an argument the action
cannot use fails here rather than in production.

Every parse happens after the agent was shown a screenshot, as in a real
step: a point is placed with the screenshot's geometry, and a reply that
names one with no measured screenshot behind it is refused. The shown
screenshot is DPR 1, so the numbers substituted below are the numbers the
action receives.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.prompts import agent_prompt_variants
from spectragent.agents.ui_tars import (
    _ACTION_TO_VERB,
    _VERB_TABLE,
    UITarsAgent,
)
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities.browser.actions.wait import WaitAction
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.assembly import make_agent, make_env
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeScope,
)

#: Every shipped template. All three render the same negotiated set, and
#: all three used to teach verbs the run rejected. Derived from the
#: directory rather than spelled out: the names are the on-disk stems now,
#: and a case-folded copy of them passed here only because this repo's
#: filesystem is case-insensitive.
_VARIANTS = list(agent_prompt_variants("ui_tars"))

#: A rendered action block line: ``- click(point='(x, y)')``.
_TAUGHT_CALL = re.compile(r"^- (\w+\(.*\))$", re.MULTILINE)

#: Placeholders the prompt shows → a value the action can actually use.
#: Anything the signature already spells concretely (``'ctrl+a'``) is
#: left alone.
_PLACEHOLDERS = {
    "'(x, y)'": "'(10, 20)'",
    "'(x1, y1)'": "'(1, 2)'",
    "'(x2, y2)'": "'(3, 4)'",
    "'up|down|left|right'": "'down'",
}


def _concrete(call: str) -> str:
    for placeholder, value in _PLACEHOLDERS.items():
        call = call.replace(placeholder, value)
    return call


def _fake_browser() -> FakeCapability:
    """A capability stub with a focused tab and an empty selector map.

    Empty on purpose: UI-TARS negotiates no DOM fragment, so an empty
    selector map is what its runs actually have. Any action that needs an
    index is therefore unexecutable for this agent by construction — which
    is the thing that went unnoticed.
    """
    cap = FakeCapability()
    cap._scopes["sid_main"] = FakeScope()
    cap._selector_maps["sid_main"] = {}
    cap._default_session_id = "sid_main"
    return cap


@pytest.fixture
def harness() -> tuple[UITarsAgent, Any, dict[str, Any]]:
    """Agent + a guard that runs the *real* action classes.

    ``act_fn`` dispatches through ``BrowserCapability``'s own
    ``_actions_by_name``, so the params the parser produces meet the
    executor the capability would have used.
    """
    capability = BrowserCapability()
    # ``wait`` sleeps for its capability-side default; zero keeps the
    # suite fast without changing the path under test.
    capability._actions_by_name["browser.wait"] = WaitAction(default_seconds=0.0)

    env = make_env(BrowserEnvironment, {"browser": capability})
    negotiated = _action_rules(
        agent=extract_wiring(UITarsAgent, "action"),
        env_provides=set(capability.provided_actions),
    )
    descriptors = env.get_action_space(active_actions=negotiated.active_actions)
    fake = _fake_browser()

    async def _execute(action: Action, **_kw: Any) -> ActionResult:
        impl = capability._actions_by_name.get(action.name)
        assert impl is not None, f"{action.name} did not route to a capability action"
        return await impl.execute(action.params, capability=fake)  # type: ignore[arg-type]

    _kept, guard = build_guarded_act(_execute, descriptors, bindings=negotiated.action_bindings)
    agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
    return agent, guard, {"descriptors": descriptors, "scope": fake._scopes["sid_main"]}


def _rendered(agent: UITarsAgent, descriptors: list[Any], variant: Any) -> str:
    """Prepare the agent with *variant* and show it one DPR-1 screenshot.

    ``Any`` because ``_VARIANTS`` is read off the disk at import time, so a
    checker cannot see that its members are the field's Literal — that
    equality is what ``test_prompt_tree`` asserts.
    """
    agent.prompt_template = variant
    prompt = agent.prepare(task="t", action_descriptors=descriptors)[0].content
    assert isinstance(prompt, str)
    agent.build_react_prompt(
        SimpleNamespace(
            platform=SimpleNamespace(operating_system="macos"),
            task=SimpleNamespace(user_request="t"),
            screenshot=ScreenshotObs(
                image_bytes=b"\x89PNG\r\n\x1a\n",
                viewport_width=1280,
                viewport_height=720,
                image_width=1280,
                image_height=720,
            ),
        )
    )
    return prompt


class TestPromptVocabulary:
    def test_every_variant_teaches_a_non_empty_vocabulary(self, harness) -> None:
        """Guards the guard: an empty scrape makes everything below vacuous.

        Both halves of empty count. ``_VARIANTS`` is globbed off disk, so
        a prompt tree that moved (or a wheel that shipped without
        ``prompts/**/*``) turns every ``for variant in _VARIANTS`` below
        into a green no-op — including this one, when it only checked the
        per-variant scrape.
        """
        agent, _guard, extra = harness
        assert _VARIANTS, (
            "no ui_tars templates on disk — agent_prompt_variants('ui_tars') "
            "is empty, so every loop in this file passes without running"
        )
        for variant in _VARIANTS:
            calls = _TAUGHT_CALL.findall(_rendered(agent, extra["descriptors"], variant))
            assert len(calls) == len(extra["descriptors"]), variant

    def test_the_prompt_teaches_the_negotiated_set_and_nothing_else(self, harness) -> None:
        """One vocabulary, not two.

        The templates used to carry a hand-written ``# Output Format``
        block *alongside* the negotiated ``# Available Actions`` list, and
        the two disagreed: the list said ``browser.type: Type text into a
        DOM element``, the block said ``type(content=..., point=...)``.
        """
        agent, _guard, extra = harness
        expected = {d.name.rsplit(".", 1)[-1] for d in extra["descriptors"]}
        for variant in _VARIANTS:
            prompt = _rendered(agent, extra["descriptors"], variant)
            verbs = {c.split("(", 1)[0] for c in _TAUGHT_CALL.findall(prompt)}
            actions = {
                agent.parse_react_output(f"Action: {_concrete(call)}")[0].name
                for call in _TAUGHT_CALL.findall(prompt)
            }
            assert actions == expected, variant
            assert verbs, variant

    def test_the_prompt_carries_the_real_parameter_contract(self, harness) -> None:
        """``_build_system_prompt`` used to drop ``descriptor().params``.

        That is why the model was never told ``browser.type`` wanted
        ``index`` / ``text``: it only ever saw name and description.

        Point params are the one exception, in the other direction: the
        agent converts them, so the executor's unit (CSS px) is exactly
        what the model must *not* be taught. What a point is comes from
        the Coordinates section instead.
        """
        agent, _guard, extra = harness
        for variant in _VARIANTS:
            prompt = _rendered(agent, extra["descriptors"], variant)
            assert "# Coordinates" in prompt, variant
            assert "CSS px" not in prompt, variant
            for descriptor in extra["descriptors"]:
                verb = _VERB_TABLE[_ACTION_TO_VERB[descriptor.name.rsplit(".", 1)[-1]]]
                converted = {key for pair in verb.points for key in pair}
                for param in verb.fills:
                    rendered = f"{param}: {descriptor.params[param]}"
                    if param in converted:
                        assert rendered not in prompt, (variant, descriptor.name, param)
                    else:
                        assert rendered in prompt, (variant, descriptor.name, param)

    def test_what_a_verb_produces_is_what_its_action_declares(self, harness) -> None:
        """The contract that did not exist, and whose absence is the bug.

        Negotiation matches *names*; nothing ever checked that the params
        an agent's parser emits are the params the action reads. So
        ``type`` could require ``index``, receive ``content``, and pass
        every test in the suite.
        """
        _agent, _guard, extra = harness
        for descriptor in extra["descriptors"]:
            verb = _VERB_TABLE[_ACTION_TO_VERB[descriptor.name.rsplit(".", 1)[-1]]]
            declared = set(descriptor.params)
            assert set(verb.fills) <= declared, descriptor.name
            required = {
                key
                for key, spec in descriptor.params.items()
                if "optional" not in str(spec).lower()
            }
            assert required <= set(verb.fills), (
                f"{descriptor.name} requires {sorted(required - set(verb.fills))}, "
                f"which the {verb!r} verb never produces"
            )

    async def test_every_taught_call_parses_guards_and_executes(self, harness) -> None:
        """The load-bearing assertion, and the one that did not exist.

        No ``Missing param``, no "not in the active action set" — for
        every call the shipped prompt puts in front of the model.
        """
        agent, guard, extra = harness
        for variant in _VARIANTS:
            prompt = _rendered(agent, extra["descriptors"], variant)
            for call in _TAUGHT_CALL.findall(prompt):
                actions = agent.parse_react_output(f"Action: {_concrete(call)}")
                assert len(actions) == 1, call
                result = await guard(actions[0])
                assert result.success, f"{variant}: {call} -> {result.error}"
                assert "Missing param" not in (result.error or "")

    async def test_typing_carries_the_text_not_the_empty_string(self, harness) -> None:
        """The second, independent breakage: ``content`` vs ``text``."""
        agent, guard, extra = harness
        _rendered(agent, extra["descriptors"], _VARIANTS[0])
        actions = agent.parse_react_output("Action: type(content='hello world', point='(10, 20)')")
        result = await guard(actions[0])
        assert result.success
        event = extra["scope"].emitted[-1]
        assert event.text == "hello world"
        assert (event.coordinate_x, event.coordinate_y) == (10, 20)

    def test_no_mobile_only_verb_survives_in_any_template(self, harness) -> None:
        """``long_press`` / ``open_app`` / ``press_home`` / ``press_back``.

        All four are unimplementable on a browser and were taught by
        ``mobile_use.md``. They belong to an Android capability that does
        not exist yet; until it does, nothing advertises them.
        """
        agent, _guard, extra = harness
        for variant in _VARIANTS:
            prompt = _rendered(agent, extra["descriptors"], variant)
            for verb in ("long_press", "open_app", "press_home", "press_back"):
                assert verb not in prompt, variant
