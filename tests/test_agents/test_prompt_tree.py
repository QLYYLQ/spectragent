"""One agent prompt tree, keyed by registry key, resolved without a path constant.

The evaluator tree got this treatment first (``test_evaluator/test_prompt_tree``);
the agent tree kept three hand-written
``PROMPT_PATH / "browser" / "agents" / <name>`` constants instead, and with
no loader there was nowhere to put the invariants a loader enforces. What
grew in that vacuum:

- an unreachable ``code_use_system_prompt.md`` that ``str.format`` could
  not even parse, shipped in every wheel;
- three prompts that were handed ``max_actions`` and never rendered it,
  so the fine-tuned browser-use model was never told the cap the loop
  enforces;
- ``content_hash=""`` on every recorded agent prompt, making "was this
  trajectory produced by the prompt I am reading now?" unanswerable;
- a ``browser/`` directory segment above ``ui_tars/``, whose three
  templates contain no browser vocabulary at all.

The load-bearing test here is
``test_template_variables_are_exactly_what_the_agent_supplies``. A
template variable nobody supplies is a crash at ``prepare()``; a supplied
param no template renders is worse — it is silent, and it is how the
negotiated action set once failed to reach a prompt at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, get_args, get_type_hints

import pytest

from spectragent.agents import AgentRegistrar, BrowserUseAgent, SkyvernAgent, UITarsAgent
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import (
    AGENT_PROMPT_ROOT,
    PROMPT_SUFFIX,
    agent_prompt_variants,
    load_prompt,
    template_variables,
)
from spectragent.prompts import PROMPT_PATH, PromptNotFoundError


def _descriptors(*keys: str) -> list[Any]:
    """Real descriptors for a few registered actions.

    Not hand-written stand-ins: ``browser_use`` compiles the descriptors
    into a closed output schema, so a fixture of empty ones would exercise
    a run that can emit nothing — and it refuses to build one, rightly.
    """
    import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers
    from spectragent.core.actions._registrar import ActionRegistrar

    return [ActionRegistrar.get(key)().descriptor() for key in keys]


#: ``prepare()`` arguments per agent. All three take the Loop's
#: ``max_actions_per_step``, because all three advertise a per-step budget.
_PREPARE_KWARGS: dict[str, dict[str, Any]] = {
    "browser_use": {
        "task": "t",
        "action_descriptors": _descriptors("browser.navigate", "browser.done"),
        "max_actions_per_step": 3,
    },
    "skyvern": {"task": "t", "action_descriptors": [], "max_actions_per_step": 3},
    "ui_tars": {"task": "t", "action_descriptors": [], "max_actions_per_step": 3},
}

#: Every ``(agent_id, kwargs)`` that reaches a *distinct* template. For
#: ``browser_use`` this is the config matrix its selector branches on; for
#: the other two it is the ``prompt_template`` Literal. Derived below into
#: "which file did that actually load", so a selector change shows up as a
#: reachability failure rather than as a silently stale list.
_CONFIGURATIONS: list[tuple[str, dict[str, Any]]] = [
    ("browser_use", {"is_browser_use_model": True, "flash_mode": True}),
    (
        "browser_use",
        {"is_browser_use_model": True, "flash_mode": False, "use_thinking": True},
    ),
    (
        "browser_use",
        {"is_browser_use_model": True, "flash_mode": False, "use_thinking": False},
    ),
    ("browser_use", {"flash_mode": True, "model_name": "claude-opus-4.5"}),
    ("browser_use", {"flash_mode": True, "is_anthropic": True}),
    ("browser_use", {"flash_mode": True}),
    ("browser_use", {"flash_mode": False, "use_thinking": True}),
    ("browser_use", {"flash_mode": False, "use_thinking": False}),
    # ``observation_mode`` is orthogonal to the template selector: it picks
    # the surface fragment spliced in, not the file. Both axes are walked
    # so a mode that renders a fragment nothing supplies — or supplies one
    # nothing renders — fails here rather than at a run's first step.
    ("browser_use", {"flash_mode": False, "observation_mode": "dom_vision"}),
    ("browser_use", {"flash_mode": False, "observation_mode": "vision"}),
    ("browser_use", {"flash_mode": True, "observation_mode": "vision"}),
    ("skyvern", {"prompt_template": "system_prompt"}),
    ("ui_tars", {"prompt_template": "computer_use"}),
    ("ui_tars", {"prompt_template": "grounding"}),
    ("ui_tars", {"prompt_template": "mobile_use"}),
]

#: ``Any``, not ``Callable[..., AgentBase]``: ``prepare`` and
#: ``system_prompt`` are Loop-slot / Protocol surface on the concrete
#: agents, not on the base, and this file walks all three uniformly.
_FACTORIES: dict[str, Any] = {
    "browser_use": BrowserUseAgent,
    "skyvern": SkyvernAgent,
    "ui_tars": UITarsAgent,
}


def _rendered(agent_id: str, config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Drive one configuration through ``prepare()``; report what it rendered.

    Spying on ``AgentBase.render`` rather than calling a helper directly:
    the params an agent *supplies* are only meaningful if they are the
    ones the production path supplies, and ``prepare()`` is that path.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    original = AgentBase.render

    def spy(self: AgentBase, name: str, params: dict[str, Any]):
        calls.append((name, dict(params)))
        return original(self, name, params)

    agent = _FACTORIES[agent_id](**config)
    try:
        AgentBase.render = spy  # type: ignore[method-assign]
        agent.prepare(**_PREPARE_KWARGS[agent_id])
    finally:
        AgentBase.render = original  # type: ignore[method-assign]

    assert len(calls) == 1, f"{agent_id}{config} rendered {len(calls)} templates"
    return calls[0]


def _reachable() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for agent_id, config in _CONFIGURATIONS:
        name, params = _rendered(agent_id, config)
        yield agent_id, name, params


_REACHED: list[tuple[str, str, dict[str, Any]]] = list(_reachable())


def _literal_options(cls: type, field: str) -> tuple[str, ...]:
    """The ``Literal`` members of an ``Annotated[Literal[...], Field(...)]``."""
    annotated = get_type_hints(cls, include_extras=True)[field]
    return get_args(get_args(annotated)[0])


# ── Layout ────────────────────────────────────────────────────────────


def test_the_invented_browser_domain_segment_is_gone():
    """No agent prompt lives under a domain it does not belong to.

    UI-TARS drives desktops and phones; its prompts sat under
    ``prompts/browser/`` purely because the first agent to need a prompt
    happened to be a browser agent.
    """
    assert not (PROMPT_PATH / "browser").exists()


def test_agent_prompts_mirror_the_evaluator_tree_layout():
    assert AGENT_PROMPT_ROOT == PROMPT_PATH / "agents"
    assert AGENT_PROMPT_ROOT.is_dir()


def test_every_prompt_directory_is_exactly_a_registry_key():
    on_disk = {
        child.name
        for child in AGENT_PROMPT_ROOT.iterdir()
        if child.is_dir() and not child.name.startswith("__")
    }
    assert on_disk <= set(AgentRegistrar.keys()), (
        f"Prompt directories {sorted(on_disk - set(AgentRegistrar.keys()))} "
        f"match no registry key, so an agent would need a path constant."
    )
    assert on_disk == set(_PREPARE_KWARGS)


@pytest.mark.parametrize("agent_id", sorted(AgentRegistrar.keys()))
def test_prompt_dir_is_derived_from_the_registry_key(agent_id):
    """The evaluator twin, on the agent side (``test_evaluator/test_registrar``).

    Reads the property an agent resolves prompts through rather than the
    module function, so ``AgentBase.prompt_dir`` has a caller — and asserts
    the directory exists, which is what catches an agent registered with
    no prompts at all (the key-set check above only runs the other way).
    """
    agent = AgentRegistrar.get(agent_id)()
    assert agent.prompt_dir == AGENT_PROMPT_ROOT / agent_id
    assert agent.prompt_dir.is_dir()


def test_no_agent_carries_its_own_prompt_directory_constant():
    """The regression this module exists to prevent: a fourth literal path."""
    import spectragent.agents.browser_use as browser_use
    import spectragent.agents.skyvern as skyvern
    import spectragent.agents.ui_tars as ui_tars

    for module in (browser_use, skyvern, ui_tars):
        assert not hasattr(module, "_PROMPT_DIR"), module.__name__


# ── Loading ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id,name",
    sorted({(a, n) for a, n, _ in _REACHED}),
)
def test_each_prompt_loads_and_hashes(agent_id, name):
    text, ref = load_prompt(agent_id, name)
    assert text.strip()
    assert ref.path == f"agents/{agent_id}/{name}{PROMPT_SUFFIX}"
    assert len(ref.content_hash) == 64


def test_missing_prompt_names_the_directory_and_alternatives():
    """The bare ``FileNotFoundError`` this replaces printed a path and nothing else.

    Not spelled ``COMPUTER_USE``: this repo's own filesystem is
    case-insensitive, so a case-only miss would find the file here and
    fail on the CI box instead.
    """
    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("ui_tars", "computer-use")
    message = str(excinfo.value)
    assert "computer-use" in message
    assert "computer_use" in message


def test_every_agent_records_a_real_content_hash():
    """``content_hash=""`` made a recorded trajectory unattributable."""
    for agent_id, config in _CONFIGURATIONS:
        agent = _FACTORIES[agent_id](**config)
        agent.prepare(**_PREPARE_KWARGS[agent_id])
        snapshot = agent.system_prompt
        assert snapshot is not None, agent_id
        assert len(snapshot.ref.content_hash) == 64, agent_id
        assert snapshot.ref.path.startswith(f"agents/{agent_id}/"), agent_id


# ── The detector ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id,name,params",
    [(a, n, p) for a, n, p in _REACHED],
    ids=[f"{a}-{n}" for a, n, _ in _REACHED],
)
def test_template_variables_are_exactly_what_the_agent_supplies(agent_id, name, params):
    """Both directions, and the second one is why this file exists.

    A referenced-but-unsupplied variable is a ``KeyError`` (or a silently
    empty Jinja block) at ``prepare()`` time. A supplied-but-unrendered
    param is the quiet one: the agent computes the negotiated action set,
    hands it to the template, and the template never mentions it — which
    is exactly how a shipped agent came to advertise nothing it could
    execute.
    """
    template, _ = load_prompt(agent_id, name)
    engine = _FACTORIES[agent_id].__prompt_engine__
    referenced = template_variables(engine, template)
    supplied = set(params)

    assert not referenced - supplied, (
        f"{agent_id}/{name} reads {sorted(referenced - supplied)}, which "
        f"nothing supplies — that is a crash at prepare()"
    )
    assert not supplied - referenced, (
        f"{agent_id}/{name} is handed {sorted(supplied - referenced)} and "
        f"renders none of it — the agent computed something the model "
        f"never sees"
    )


# ── Reachability: the field, the selector, and the disk agree ─────────


@pytest.mark.parametrize("agent_id", sorted(_PREPARE_KWARGS))
def test_every_file_on_disk_is_reachable_from_its_agent(agent_id):
    """A prompt nothing can load is dead weight that still ships."""
    reached = {name for a, name, _ in _REACHED if a == agent_id}
    assert reached == set(agent_prompt_variants(agent_id))


@pytest.mark.parametrize(
    "cls,agent_id",
    [(SkyvernAgent, "skyvern"), (UITarsAgent, "ui_tars")],
)
def test_prompt_template_literal_matches_the_directory(cls, agent_id):
    """The hand-written ``Literal`` is the cheap version of a computed one.

    conscribe renders ``Literal[tuple(...)]`` correctly, but a computed
    Literal costs a pyright error per field. Writing it out and asserting
    it here buys the same narrowing for free — and moves a typo'd
    ``prompt_template`` from a ``FileNotFoundError`` inside ``prepare()``
    to a Pydantic validation error at compose time.
    """
    assert set(_literal_options(cls, "prompt_template")) == set(
        agent_prompt_variants(agent_id)
    )


def test_browser_use_selects_rather_than_naming_its_template():
    """Its choice is a function of the config, so there is no field to type.

    Adding one would let ``flash_mode: true`` and
    ``prompt_template: system_prompt`` disagree in the same YAML.
    """
    assert "prompt_template" not in get_type_hints(BrowserUseAgent, include_extras=True)
    assert BrowserUseAgent(flash_mode=True).select_prompt_variant() == (
        "system_prompt_flash"
    )


# ── Engine ────────────────────────────────────────────────────────────


def test_the_engine_is_declared_not_sniffed():
    """Declared per class, so a stray ``{%`` cannot re-interpret a file."""
    assert BrowserUseAgent.__prompt_engine__ == "format"
    assert UITarsAgent.__prompt_engine__ == "format"
    assert SkyvernAgent.__prompt_engine__ == "jinja2"


@pytest.mark.parametrize(
    "agent_id,name",
    sorted({(a, n) for a, n, _ in _REACHED}),
)
def test_format_templates_hold_no_jinja_and_vice_versa(agent_id, name):
    """A mis-declared engine renders without error and drops everything."""
    template, _ = load_prompt(agent_id, name)
    if _FACTORIES[agent_id].__prompt_engine__ == "jinja2":
        assert "{{" in template, f"{name} declares jinja2 and substitutes nothing"
    else:
        assert "{%" not in template, f"{name} declares format but carries a jinja tag"


# ── Surface fragments ─────────────────────────────────────────────────

_SURFACE_DIR = AGENT_PROMPT_ROOT / "browser_use" / "_surface"


def test_surface_fragments_are_not_mistaken_for_template_variants():
    """They live one level down precisely so the glob cannot see them.

    A fragment is a *value* spliced into a template, not a prompt an agent
    can select; if ``agent_prompt_variants`` listed them,
    ``test_every_file_on_disk_is_reachable_from_its_agent`` would demand a
    config that renders each one on its own.
    """
    assert _SURFACE_DIR.is_dir()
    assert not set(agent_prompt_variants("browser_use")) & {
        p.stem for p in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}")
    }


def test_every_surface_fragment_is_reachable_from_some_mode():
    """The same rule as for templates: a file nothing loads still ships."""
    reached: set[str] = set()
    for mode in ("dom", "dom_vision", "vision"):
        for flash in (True, False):
            agent = BrowserUseAgent(observation_mode=mode, flash_mode=flash)
            agent.prepare(**_PREPARE_KWARGS["browser_use"])
            snapshot = agent.system_prompt
            assert snapshot is not None
            reached.add(snapshot.params["surface"])  # type: ignore[union-attr]
    on_disk = {p.read_text("utf-8").strip() for p in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}")}
    # A rendered surface may carry an appended coordinate paragraph, so
    # match on "the fragment is a prefix of something rendered".
    unused = [text for text in on_disk if not any(r.startswith(text) for r in reached)]
    assert not unused, f"{len(unused)} surface fragment(s) no mode renders"


def test_surface_fragments_substitute_nothing():
    """They are values, and ``str.format`` does not recurse into one.

    A ``{placeholder}`` here would reach the model verbatim — so the one
    part of the surface that *is* configuration (the coordinate grid) is
    composed in Python, not left as a token in the file.
    """
    for path in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}"):
        assert not template_variables("format", path.read_text("utf-8")), path.name

