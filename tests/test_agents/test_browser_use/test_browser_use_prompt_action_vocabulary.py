"""Everything a BrowserUse prompt teaches must exist in the run it teaches it to.

The Skyvern and UI-TARS siblings of this file pin template → parser →
guard for agents whose prompts *name* their verbs in prose. BrowserUse's
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
- **"uncertain" taught as ``success: false``.** ``done``'s ``success`` is
  whether the task was accomplished, left unset when the agent cannot tell
  (``ActionResult.reported_success`` ``None``); a template that maps doubt
  to ``false`` makes an unknown outcome read as a known failure.

And one the family members introduce: ``browser_use.dom`` shows the model no
image, so a surface that calls a screenshot the ground truth would teach a
channel the run does not have.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from spectragent.agents.browser_use import (
    BrowserUseAgent,
    BrowserUseDomAgent,
    BrowserUseDomVisionAgent,
)
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import (
    negotiate_action,
    negotiate_observation,
    quoted_fragment_formats,
)
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent, make_env

MEMBERS: tuple[type[BrowserUseAgent], ...] = (BrowserUseDomAgent, BrowserUseDomVisionAgent)

#: Every ``(member, config)`` that reaches a distinct template.
_CONFIGURATIONS: list[tuple[type[BrowserUseAgent], dict[str, Any]]] = [
    *[(member, {"flash_mode": True}) for member in MEMBERS],
    *[(member, {"flash_mode": False, "use_thinking": True}) for member in MEMBERS],
    *[(member, {"flash_mode": False, "use_thinking": False}) for member in MEMBERS],
    (BrowserUseDomAgent, {"is_browser_use_model": True, "flash_mode": True}),
    (BrowserUseDomVisionAgent, {"is_browser_use_model": True, "flash_mode": False}),
    (
        BrowserUseDomAgent,
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


def _run(
    member: type[BrowserUseAgent], config: dict[str, Any]
) -> tuple[BrowserUseAgent, str, set[str], Any]:
    """Negotiate and prepare one configuration; return the rendered prompt."""
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    agent = make_agent(member, memory=SimpleMemory(max_messages=10), **config)
    loop = ReactLoop(max_actions_per_step=1)
    obs = negotiate_observation(agent, loop, env)
    actions = negotiate_action(agent, env)
    descriptors = env.get_action_space(
        active_actions=actions.active_actions, fragments=obs.active_fragments
    )
    action_space, guard = build_guarded_act(
        env.act, descriptors, None, bindings=actions.action_bindings
    )
    agent.prepare(
        task="t",
        action_space=space_for(agent, action_space.descriptors),
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        fragment_formats=quoted_fragment_formats(agent, obs, env) or {},
    )
    return (
        agent,
        agent._system_prompt_text,
        {d.name for d in action_space.descriptors},
        action_space.descriptors,
    )


_IDS = [f"{member.__registry_key__}-{sorted(config.items())}" for member, config in _CONFIGURATIONS]


@pytest.mark.parametrize("member,config", _CONFIGURATIONS, ids=_IDS)
class TestEveryRenderedPrompt:
    def test_advertises_exactly_the_negotiated_actions(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        _agent, prompt, names, descriptors = _run(member, config)
        block = _ACTION_BLOCK.search(prompt)
        assert block is not None, "the template no longer renders {action_list}"
        for descriptor in descriptors:
            assert f"`{descriptor.name}(" in block.group(0)
            for param in descriptor.params:
                assert f"{param}:" in block.group(0), descriptor.name

    def test_teaches_no_verb_outside_the_negotiated_set(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        """Prose may not name an action this run did not negotiate.

        ``click_coordinate`` is the live case: real in ``browser_use.dom_vision``,
        absent from ``browser_use.dom``, whose prompt must not mention it.
        """
        _agent, prompt, names, _descriptors = _run(member, config)
        prose = _ACTION_BLOCK.sub("", prompt)
        registered = {key.rsplit(".", 1)[-1] for key in ActionRegistrar.get_all()}
        active = {name.rsplit(".", 1)[-1] for name in names}
        for match in _TAUGHT_VERB.finditer(prose):
            verb = match.group(1) or match.group(2)
            if verb in _NOT_ACTIONS or verb not in registered:
                continue
            assert verb in active, (
                f"{member.__registry_key__}{config}: the prompt teaches {verb!r}, which this run "
                f"did not negotiate — the model cannot emit it and the closed "
                f"schema will refuse it"
            )

    def test_teaches_no_tool_this_framework_does_not_have(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(member, config)
        for tool in _NONEXISTENT_TOOLS:
            assert tool not in prompt, f"{member.__registry_key__}{config} still teaches {tool!r}"

    def test_claims_nothing_is_handled_automatically(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(member, config)
        for claim in _FALSE_CLAIMS:
            assert claim not in prompt.lower(), f"{member.__registry_key__}{config}: {claim!r}"

    def test_names_done_s_answer_field_as_the_action_reads_it(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        _agent, prompt, _names, _descriptors = _run(member, config)
        assert "`text` field" not in prompt
        assert "text field of the `done`" not in prompt
        assert "done action's text field" not in prompt
        assert "files_to_display" not in prompt

    def test_teaches_done_s_success_as_the_action_reads_it(
        self, member: type[BrowserUseAgent], config: dict[str, Any]
    ) -> None:
        """Known unmet or blocked: ``false``; verified: ``true``; cannot tell: unset."""
        _agent, prompt, _names, _descriptors = _run(member, config)
        if "`success`" not in prompt:
            return  # the template leaves ``success`` to the action list's own description
        assert "leave `success` unset" in prompt
        assert "`success=false`" in prompt or "set `success` to `false`" in prompt
        for line in prompt.splitlines():
            doubt = "uncertain" in line or "unverifiable" in line
            assert not (doubt and "false" in line), f"{member.__registry_key__}{config}: {line!r}"


@pytest.mark.parametrize("member", MEMBERS)
def test_every_member_explains_the_index(member: type[BrowserUseAgent]) -> None:
    """The explanation is the DOM serializer's own description, quoted whole."""
    _agent, prompt, _names, _descriptors = _run(member, {"flash_mode": False})
    assert BrowserCapability().dom_serializer.describe_format() in prompt
    assert "[42]<button" in prompt


def test_a_prompt_with_a_coordinate_action_says_what_a_point_is() -> None:
    _agent, prompt, names, _descriptors = _run(BrowserUseDomVisionAgent, {"flash_mode": False})
    assert "browser.click_coordinate" in names
    assert "<coordinates>" in prompt
    assert "top-left" in prompt


def test_the_coordinate_paragraph_follows_the_configured_grid() -> None:
    _agent, pixels, _n, _d = _run(BrowserUseDomVisionAgent, {"flash_mode": False})
    assert "pixels of the screenshot" in pixels
    _agent, grid, _n, _d = _run(
        BrowserUseDomVisionAgent, {"flash_mode": False, "coordinate_space": "normalized_1000"}
    )
    assert "0-1000 grid" in grid


def test_no_prompt_promises_a_channel_its_member_does_not_have() -> None:
    """``browser_use.dom`` shows the model no image, so nothing may call one ground truth."""
    _agent, prompt, _names, _descriptors = _run(BrowserUseDomAgent, {"flash_mode": False})
    assert "screenshot" not in prompt.lower()
