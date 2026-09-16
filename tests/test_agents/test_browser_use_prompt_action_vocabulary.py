"""Everything browser_use's prompt teaches must exist in the run it teaches it to.

The Skyvern and UI-TARS siblings of this file pin template → parser →
guard for agents whose prompts *name* their verbs in prose. browser_use's
prompts do not: the verb list is generated from the negotiated descriptors
(``{action_list}``), so the interesting failures are the ones prose can
still cause around it.

Three of them shipped, in five templates inherited from upstream:

- **tools that do not exist here.** ``search_page``, ``find_elements``,
  ``evaluate``, ``read_file``, ``replace_file_str``, a ``todo.md`` file
  system, ``files_to_display`` on ``done``. Under a closed schema a model
  *cannot* emit them, so teaching them buys nothing and spends reasoning
  on a plan it will then fail to execute.
- **a name for ``done``'s answer that the action does not read.** The
  templates said ``text``; ``DoneAction`` reads ``extracted_content``, and
  a rename inside the parser bridged the two where nothing could see it.
- **"CAPTCHAs are automatically solved by the browser."** They are not,
  by anything in this framework, and the claim directly contradicts the
  anti-bot work these prompts are used for.

And one the modes introduce: a ``vision`` run has no element list and no
index-addressed action, so a template that still talks about ``[index]``
teaches a way of acting the run cannot perform.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import negotiate_action, negotiate_observation
from tests._helpers.assembly import make_agent, make_env

MODES = ("dom", "dom_vision", "vision")

#: Every ``(observation_mode, config)`` that reaches a distinct template.
#: The fine-tuned three are DOM-only by construction, so they are paired
#: with DOM modes alone.
_CONFIGURATIONS: list[tuple[str, dict[str, Any]]] = [
    *[(mode, {"flash_mode": True}) for mode in MODES],
    *[(mode, {"flash_mode": True, "is_anthropic": True}) for mode in MODES],
    *[(mode, {"flash_mode": True, "model_name": "claude-opus-4.5"}) for mode in MODES],
    *[(mode, {"flash_mode": False, "use_thinking": True}) for mode in MODES],
    *[(mode, {"flash_mode": False, "use_thinking": False}) for mode in MODES],
    ("dom", {"is_browser_use_model": True, "flash_mode": True}),
    ("dom_vision", {"is_browser_use_model": True, "flash_mode": False}),
    (
        "dom",
        {"is_browser_use_model": True, "flash_mode": False, "use_thinking": False},
    ),
]

#: Tools no capability in this tree provides. Matched as words so that
#: ordinary English ("re-evaluate the task") does not trip the gate.
_NONEXISTENT_TOOLS = (
    "search_page",
    "find_elements",
    "read_file",
    "write_file",
    "replace_file_str",
    "files_to_display",
    "todo.md",
    "available_file_paths",
    "read_state",
)

#: Claims about automatic handling that nothing in the framework does.
_FALSE_CLAIMS = (
    "solved automatically",
    "automatically solved",
    "auto-solved",
    "handled automatically",
)

#: Vocabulary that only means something when an element list exists.
#: Deliberately the *format tokens* rather than the bare word: the vision
#: surface says "there is no element index in this run", which is exactly
#: the sentence a bare-word gate would ban.
_INDEX_WORDS = ("[index]", "Interactive Elements", "*[", "selector map", "selector_map")

#: A verb taught as a call or in backticks, e.g. ``click(index)`` or
#: `` `evaluate` ``. The generated ``{action_list}`` block is excluded
#: before this runs, so what is left is prose.
_TAUGHT_VERB = re.compile(r"`([a-z_]+)`|\b([a-z_]+)\(")
_ACTION_BLOCK = re.compile(r"<actions_available>.*?</actions_available>", re.DOTALL)

#: Verbs that are prose vocabulary, not action names: the output schema's
#: own field, and the two plan fields the template teaches.
_NOT_ACTIONS = frozenset(
    {
        "action",
        "memory",
        "thinking",
        "next_goal",
        "evaluation_previous_goal",
        "current_plan_item",
        "plan_update",
        "extracted_content",
        "success",
        "max_steps",
        "browser_state",
        "user_request",
        "agent_history",
        "step_info",
        "true",
        "false",
    }
)


def _run(mode: str, config: dict[str, Any]) -> tuple[BrowserUseAgent, str, set[str], Any]:
    """Negotiate and prepare one configuration; return the rendered prompt."""
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    agent = make_agent(
        BrowserUseAgent,
        memory=SimpleMemory(max_messages=10),
        observation_mode=mode,
        **config,
    )
    loop = ReactLoop(max_actions_per_step=1)
    obs = negotiate_observation(agent, loop, env)
    actions = negotiate_action(agent, env)
    descriptors = env.get_action_space(
        active_actions=actions.active_actions, fragments=obs.active_fragments
    )
    descriptors, guard = build_guarded_act(
        env.act, descriptors, None, bindings=actions.action_bindings
    )
    agent.prepare(task="t", action_descriptors=descriptors)
    return agent, agent._system_prompt_text, {d.name for d in descriptors}, descriptors


_IDS = [f"{mode}-{sorted(config.items())}" for mode, config in _CONFIGURATIONS]


@pytest.mark.parametrize("mode,config", _CONFIGURATIONS, ids=_IDS)
class TestEveryRenderedPrompt:
    def test_advertises_exactly_the_negotiated_actions(
        self, mode: str, config: dict[str, Any]
    ) -> None:
        _agent, prompt, names, descriptors = _run(mode, config)
        block = _ACTION_BLOCK.search(prompt)
        assert block is not None, "the template no longer renders {action_list}"
        for descriptor in descriptors:
            assert f"`{descriptor.name}(" in block.group(0)
            for param in descriptor.params:
                assert f"{param}:" in block.group(0), descriptor.name

    def test_teaches_no_verb_outside_the_negotiated_set(
        self, mode: str, config: dict[str, Any]
    ) -> None:
        """Prose may not name an action this run did not negotiate.

        ``extract_content`` is the live case: real everywhere except
        ``vision``, where the prompt must stop mentioning it.
        """
        _agent, prompt, names, _descriptors = _run(mode, config)
        prose = _ACTION_BLOCK.sub("", prompt)
        registered = {key.rsplit(".", 1)[-1] for key in ActionRegistrar.get_all()}
        active = {name.rsplit(".", 1)[-1] for name in names}
        for match in _TAUGHT_VERB.finditer(prose):
            verb = match.group(1) or match.group(2)
            if verb in _NOT_ACTIONS or verb not in registered:
                continue
            assert verb in active, (
                f"{mode}{config}: the prompt teaches {verb!r}, which this run "
                f"did not negotiate — the model cannot emit it and the closed "
                f"schema will refuse it"
            )

    def test_teaches_no_tool_this_framework_does_not_have(
        self, mode: str, config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(mode, config)
        for tool in _NONEXISTENT_TOOLS:
            assert tool not in prompt, f"{mode}{config} still teaches {tool!r}"

    def test_claims_nothing_is_handled_automatically(
        self, mode: str, config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(mode, config)
        for claim in _FALSE_CLAIMS:
            assert claim not in prompt.lower(), f"{mode}{config}: {claim!r}"

    def test_names_done_s_answer_field_as_the_action_reads_it(
        self, mode: str, config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(mode, config)
        assert "`text` field" not in prompt
        assert "text field of the `done`" not in prompt
        assert "done action's text field" not in prompt
        assert "files_to_display" not in prompt


@pytest.mark.parametrize(
    "config",
    [c for mode, c in _CONFIGURATIONS if mode == "vision"],
    ids=[str(sorted(c.items())) for mode, c in _CONFIGURATIONS if mode == "vision"],
)
def test_a_vision_prompt_carries_no_index_vocabulary(config: dict[str, Any]) -> None:
    """No element list exists, so nothing may suggest addressing one."""
    _agent, prompt, _names, descriptors = _run("vision", config)
    for word in _INDEX_WORDS:
        assert word not in prompt, f"vision{config} still says {word!r}"
    # And no *advertised parameter* is an index or a selector, which is the
    # machine-checkable half of the same claim.
    for descriptor in descriptors:
        assert "index" not in descriptor.params, descriptor.name
        assert "selector" not in descriptor.params, descriptor.name


@pytest.mark.parametrize("mode", ["dom", "dom_vision"])
def test_a_dom_prompt_does_explain_the_index(mode: str) -> None:
    """The complement: the gate above must not be satisfied by saying nothing."""
    _agent, prompt, _names, _descriptors = _run(mode, {"flash_mode": False})
    assert "[index]" in prompt
    assert "interactive" in prompt.lower()


def test_a_vision_prompt_says_what_a_point_is() -> None:
    _agent, prompt, _names, _descriptors = _run("vision", {"flash_mode": False})
    assert "<coordinates>" in prompt
    assert "top-left" in prompt


def test_the_coordinate_paragraph_follows_the_configured_grid() -> None:
    _agent, pixels, _n, _d = _run("vision", {"flash_mode": False})
    assert "pixels of the screenshot" in pixels

    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    agent = make_agent(
        BrowserUseAgent,
        memory=SimpleMemory(max_messages=10),
        observation_mode="vision",
        coordinate_space="normalized_1000",
        flash_mode=False,
    )
    obs = negotiate_observation(agent, ReactLoop(), env)
    actions = negotiate_action(agent, env)
    agent.prepare(
        task="t",
        action_descriptors=env.get_action_space(
            active_actions=actions.active_actions, fragments=obs.active_fragments
        ),
    )
    assert "0-1000 grid" in agent._system_prompt_text


def test_no_prompt_promises_a_channel_its_mode_does_not_have() -> None:
    """``dom`` shows the model no image, so nothing may call one ground truth."""
    _agent, prompt, _names, _descriptors = _run("dom", {"flash_mode": False})
    assert "screenshot" not in prompt.lower()
