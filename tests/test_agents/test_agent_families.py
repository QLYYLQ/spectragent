"""Agent families: an abstract root key, members keyed ``<root>.<member>``.

``AgentRegistrar`` has ``key_separator="."``. A family is an abstract,
unregistered root that holds a ``__registry_key__`` (``browser_use``) and
the parts its members share; each member registers under
``<root>.<member>`` and writes its own ``__wiring__``, merged with the
root's through the MRO. Members share their family's prompt directory.

The family here is a throwaway, registered inside the per-test
``conscribe_isolated`` rollback (``tests/conftest.py``), so no shipped
agent changes shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import yaml
from conscribe import build_composed_config, generate_composed_config_source
from pydantic import Field

from spectragent.agents import AgentRegistrar
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import (
    AGENT_KEY_SEPARATOR,
    AGENT_PROMPT_ROOT,
    agent_family,
    agent_prompt_dir,
    agent_prompt_variants,
    load_prompt,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.negotiation._base import extract_wiring
from spectragent.core.registries import collect_registrars

_ROOT = "probe_family"


def _define_family() -> tuple[type[AgentBase], type[AgentBase], type[AgentBase]]:
    """An abstract root and two members, one derived key and one written out."""

    @dataclass
    class ProbeFamily(AgentBase):
        __abstract__ = True
        __registry_key__ = _ROOT
        # Shared by every member: on the root, merged through the MRO.
        __wiring__ = {"agent_loop": ("agent_loop", ["react"])}

        shared_knob: Annotated[int, Field(description="A knob every member has.")] = 1

        @property
        def agent_name(self) -> str:
            return "probe"

        @property
        def system_prompt(self) -> Any:
            return None

        def get_action_space(self) -> list[Any]:
            return []

    @dataclass
    class Dom(ProbeFamily):
        # No ``__registry_key__``: derived from the root, ``probe_family.dom``.
        __wiring__ = {
            "observation": {"registry": "observation", "keys": ["dom"], "declare_only": True},
        }

        member_knob: Annotated[str, Field(description="A knob only this member has.")] = "x"

    @dataclass
    class DomVision(ProbeFamily):
        __registry_key__ = "probe_family.dom_vision"
        __wiring__ = {
            "observation": {
                "registry": "observation",
                "keys": ["dom", "screenshot"],
                "declare_only": True,
            },
        }

    return ProbeFamily, Dom, DomVision


# ── Registration ──────────────────────────────────────────────────────


def test_members_register_under_the_family_root_and_the_root_does_not():
    root, dom, dom_vision = _define_family()

    assert AgentRegistrar.get("probe_family.dom") is dom
    assert AgentRegistrar.get("probe_family.dom_vision") is dom_vision
    assert AgentRegistrar.get_or_none(_ROOT) is None
    assert AgentRegistrar.children(_ROOT) == {
        "probe_family.dom": dom,
        "probe_family.dom_vision": dom_vision,
    }
    assert dom().agent_id == "probe_family.dom"
    assert root.__registry_key__ == _ROOT


def test_no_shipped_agent_key_is_also_a_family_root():
    """A family root is abstract, never a registered agent as well.

    A subclass of a *registered* agent that sets no key of its own is keyed
    below it (``ui_tars`` → ``ui_tars.<name>``), which makes ``ui_tars`` both
    an agent and a family; conscribe's ``tree()`` refuses that shape.
    Splitting an agent into a family means making its root abstract. Only
    shipped classes are checked: other test modules register synthetic
    agents at import time.
    """
    shipped = {
        key
        for key, cls in AgentRegistrar.get_all().items()
        if cls.__module__.startswith("spectragent.")
    }
    assert shipped, "no shipped agents registered"
    both = sorted(
        key
        for key in shipped
        if any(other.startswith(key + AGENT_KEY_SEPARATOR) for other in shipped)
    )
    assert not both, f"{both} are registered agents and family roots at once"


def test_members_merge_the_roots_wiring_and_keep_their_own():
    _root, dom, dom_vision = _define_family()

    for member in (dom, dom_vision):
        assert extract_wiring(member, "agent_loop") == ({"react"}, set())
    assert extract_wiring(dom, "observation") == ({"dom"}, set())
    assert extract_wiring(dom_vision, "observation") == ({"dom", "screenshot"}, set())


def test_keys_without_a_separator_are_unchanged():
    """The undotted shipped agents keep their keys, and a new undotted agent stays undotted."""
    assert AgentRegistrar.get("ui_tars").__registry_key__ == "ui_tars"

    @dataclass
    class StandaloneProbe(AgentBase):
        __registry_key__ = "standalone_probe"

        @property
        def agent_name(self) -> str:
            return "standalone"

        @property
        def system_prompt(self) -> Any:
            return None

        def get_action_space(self) -> list[Any]:
            return []

    assert AgentRegistrar.get("standalone_probe") is StandaloneProbe
    assert agent_family("standalone_probe") == "standalone_probe"
    assert agent_prompt_dir("ui_tars") == AGENT_PROMPT_ROOT / "ui_tars"


# ── Prompts by family root ────────────────────────────────────────────


def test_a_member_reads_its_family_roots_prompt_directory():
    _root, dom, dom_vision = _define_family()

    assert AGENT_KEY_SEPARATOR == "."
    assert agent_family("probe_family.dom") == _ROOT
    assert dom().prompt_dir == dom_vision().prompt_dir == AGENT_PROMPT_ROOT / _ROOT


def test_a_dotted_key_loads_the_family_templates():
    """Same files, same recorded path, whichever member asks."""
    assert agent_prompt_dir("ui_tars.some_member") == AGENT_PROMPT_ROOT / "ui_tars"
    assert agent_prompt_variants("ui_tars.some_member") == agent_prompt_variants("ui_tars")

    text, ref = load_prompt("ui_tars.some_member", "computer_use")
    assert (text, ref) == load_prompt("ui_tars", "computer_use")
    assert ref.path == "agents/ui_tars/computer_use.md"


# ── Composed configuration ────────────────────────────────────────────

_YAML = """
agent:
  - agent_id: probe_family.dom
    shared_knob: 3
    member_knob: y
    agent_loop: {loop_id: react}
    llm: {model_type: openai, model: gpt-4o, provider: {name: direct}}
    memory: {memory_id: simple}
"""


def test_a_dotted_agent_id_stays_a_flat_discriminator_value():
    """The member key is one ``agent_id`` value, not a nested block.

    Unlike the LLM layer's ``model_type`` + ``provider`` (two discriminator
    fields), the agent layer has one discriminator field, so the separator
    changes only the key's spelling.
    """
    _define_family()
    composer = ConfigComposer(registrars=collect_registrars())

    result = build_composed_config(
        collect_registrars(), inline_wiring=True, top_level_extra="forbid"
    )
    source = generate_composed_config_source(result)
    assert "agent_id: Literal['probe_family.dom'] = 'probe_family.dom'" in source
    assert "agent_id: Literal['probe_family.dom_vision'] = 'probe_family.dom_vision'" in source

    raw = yaml.safe_load(_YAML)
    config = composer.top_level_type.model_validate(raw)
    dumped = config.model_dump(mode="json", exclude_unset=True)
    assert dumped["agent"][0]["agent_id"] == "probe_family.dom"
    assert composer.top_level_type.model_validate(dumped).model_dump(mode="json") == (
        config.model_dump(mode="json")
    )

    run = composer.compose(raw)
    assert run.ids("agent") == ["probe_family.dom#0"]
    agent = run.single("agent")
    assert type(agent) is AgentRegistrar.get("probe_family.dom")
    assert agent.agent_id == "probe_family.dom"
    assert (agent.shared_knob, agent.member_knob) == (3, "y")
    assert agent.prompt_dir == AGENT_PROMPT_ROOT / _ROOT
