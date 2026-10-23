"""The Skyvern family: an abstract root ``skyvern`` and two registered members.

``docs/architecture_design.md`` §4.1 "Agent 家族". The root is never an agent
of its own; each member writes its complete observation and action wiring,
and the parts they share (the Loop choice, the LLM, the memory) come from the
root through the MRO. The two members differ only in what reaches the model:
``skyvern.dom_vision`` also sees the screenshot and may click by point.
"""

from __future__ import annotations

from typing import get_type_hints

import yaml

from spectragent.agents import AgentRegistrar
from spectragent.agents.prompts import AGENT_PROMPT_ROOT
from spectragent.agents.skyvern import SkyvernAgent, SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.core.composer import ConfigComposer
from spectragent.core.loops.coordinate_frames import declared_space
from spectragent.core.negotiation._base import extract_wiring
from spectragent.core.registries import collect_registrars

_INDEX_ACTIONS = {"click", "type", "done"}
_OPTIONAL_ACTIONS = {
    "scroll",
    "send_keys",
    "go_back",
    "go_forward",
    "select_dropdown",
    "extract_content",
}
_OBSERVATION = {"dom", "screenshot", "platform", "task", "browser_meta", "history"}


def test_the_root_is_abstract_and_the_members_are_registered() -> None:
    assert AgentRegistrar.get_or_none("skyvern") is None
    assert AgentRegistrar.children("skyvern") == {
        "skyvern.dom": SkyvernDomAgent,
        "skyvern.dom_vision": SkyvernDomVisionAgent,
    }
    for member in (SkyvernDomAgent, SkyvernDomVisionAgent):
        assert issubclass(member, SkyvernAgent)
        assert member().agent_id == member.__registry_key__


def test_there_is_no_vision_only_member() -> None:
    """Without the element tree nothing is Skyvern's own; computer-use agents cover it."""
    for member in AgentRegistrar.children("skyvern").values():
        required, _optional = extract_wiring(member, "observation")
        assert "dom" in required, member.__registry_key__


def test_the_shared_wiring_comes_from_the_root() -> None:
    for member in (SkyvernDomAgent, SkyvernDomVisionAgent):
        assert extract_wiring(member, "agent_loop") == ({"react", "branch"}, set())
        assert "observation" not in SkyvernAgent.__wiring__
        assert "action" not in SkyvernAgent.__wiring__


def test_both_members_capture_the_screenshot() -> None:
    """Required in both: the trace keeps it and evaluators score runs from it."""
    for member in (SkyvernDomAgent, SkyvernDomVisionAgent):
        assert extract_wiring(member, "observation") == (
            _OBSERVATION,
            {"step_info", "capture_state"},
        )


def test_dom_asks_for_index_addressed_actions_only() -> None:
    assert extract_wiring(SkyvernDomAgent, "action") == (_INDEX_ACTIONS, _OPTIONAL_ACTIONS)


def test_dom_vision_also_offers_click_coordinate() -> None:
    assert extract_wiring(SkyvernDomVisionAgent, "action") == (
        _INDEX_ACTIONS,
        _OPTIONAL_ACTIONS | {"click_coordinate"},
    )


def test_only_dom_vision_declares_a_grid() -> None:
    """The member's whole part in coordinates is the grid it declares; the framework does the rest."""
    assert declared_space(SkyvernDomAgent()) is None
    assert declared_space(SkyvernDomVisionAgent()) == "screenshot_pixels"
    assert "coordinate_space" not in get_type_hints(SkyvernDomAgent)
    assert "vision_detail" not in get_type_hints(SkyvernDomAgent)


def test_members_share_the_family_prompt_directory() -> None:
    assert SkyvernDomAgent().prompt_dir == SkyvernDomVisionAgent().prompt_dir
    assert SkyvernDomAgent().prompt_dir == AGENT_PROMPT_ROOT / "skyvern"


def test_a_member_composes_from_yaml_with_its_own_knobs() -> None:
    raw = yaml.safe_load(
        """
agent:
  - agent_id: skyvern.dom_vision
    coordinate_space: normalized_1000
    vision_detail: high
    max_result_chars: 1200
    agent_loop: {loop_id: react}
    llm: {model_type: openai, model: gpt-4o, provider: {name: direct}}
    memory: {memory_id: sliding_window, window_size: 3}
"""
    )
    agent = ConfigComposer(registrars=collect_registrars()).compose(raw).single("agent")
    assert type(agent) is SkyvernDomVisionAgent
    assert (agent.coordinate_space, agent.vision_detail, agent.max_result_chars) == (
        "normalized_1000",
        "high",
        1200,
    )
