"""One agent prompt tree, keyed by family root, resolved without a path constant.

A key's family root is the part before its first dot (``browser_use.dom`` →
``browser_use``); an undotted key is its own family. Members of one family
share its directory.

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

from spectragent.agents import (
    AgentRegistrar,
    BrowserUseAgent,
    BrowserUseDomAgent,
    BrowserUseDomVisionAgent,
    SkyvernAgent,
    SkyvernDomAgent,
    SkyvernDomVisionAgent,
    UITarsAgent,
)
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import (
    AGENT_PROMPT_ROOT,
    PROMPT_SUFFIX,
    agent_family,
    agent_prompt_dir,
    agent_prompt_variants,
    load_prompt,
    template_variables,
)
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.prompts import PROMPT_PATH, PromptNotFoundError
from tests._helpers.action_spaces import space_of
from tests._helpers.fragment_formats import dom_formats


def _descriptors(*keys: str) -> list[Any]:
    """Real descriptors for a few registered actions.

    Not hand-written stand-ins: BrowserUse compiles the descriptors
    into a closed output schema, so a fixture of empty ones would exercise
    a run that can emit nothing — and it refuses to build one, rightly.
    """
    import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers
    from spectragent.core.actions._registrar import ActionRegistrar

    return [ActionRegistrar.get(key)().descriptor() for key in keys]


_BROWSER_USE_PREPARE: dict[str, Any] = {
    "task": "t",
    "action_space": space_of(_descriptors("browser.navigate", "browser.done")),
    "execution_limits": ExecutionLimits(max_actions_per_step=3),
    "fragment_formats": dom_formats(),
}

#: ``prepare()`` arguments per agent. All of them take the Loop's
#: ``max_actions_per_step``, because all of them advertise a per-step budget;
#: the DOM agents also take the DOM serializer's format description.
_PREPARE_KWARGS: dict[str, dict[str, Any]] = {
    "browser_use.dom": _BROWSER_USE_PREPARE,
    "browser_use.dom_vision": _BROWSER_USE_PREPARE,
    # Real descriptors, as for ``browser_use``: Skyvern compiles them into
    # its closed reply schema and refuses a run with no action.
    "skyvern.dom": {
        "task": "t",
        "action_space": space_of(_descriptors("browser.click", "browser.done")),
        "execution_limits": ExecutionLimits(max_actions_per_step=3),
        "fragment_formats": dom_formats(),
    },
    "skyvern.dom_vision": {
        "task": "t",
        "action_space": space_of(
            _descriptors("browser.click", "browser.click_coordinate", "browser.done"),
            coordinate_space="screenshot_pixels",
        ),
        "execution_limits": ExecutionLimits(max_actions_per_step=3),
        "fragment_formats": dom_formats(),
    },
    "ui_tars": {
        "task": "t",
        "action_space": space_of([]),
        "execution_limits": ExecutionLimits(max_actions_per_step=3),
    },
}

#: Every ``(agent_id, kwargs)`` that reaches a *distinct* template. For
#: the BrowserUse family this is the config matrix its selector branches on;
#: for the other two it is the ``prompt_template`` Literal. Derived below into
#: "which file did that actually load", so a selector change shows up as a
#: reachability failure rather than as a silently stale list.
_CONFIGURATIONS: list[tuple[str, dict[str, Any]]] = [
    ("browser_use.dom", {"is_browser_use_model": True, "flash_mode": True}),
    (
        "browser_use.dom",
        {"is_browser_use_model": True, "flash_mode": False, "use_thinking": True},
    ),
    (
        "browser_use.dom",
        {"is_browser_use_model": True, "flash_mode": False, "use_thinking": False},
    ),
    ("browser_use.dom", {"flash_mode": True}),
    ("browser_use.dom", {"flash_mode": False, "use_thinking": True}),
    ("browser_use.dom", {"flash_mode": False, "use_thinking": False}),
    # The member is orthogonal to the template selector: it picks the
    # surface fragment spliced in, not the file. Both axes are walked so a
    # member that renders a fragment nothing supplies — or supplies one
    # nothing renders — fails here rather than at a run's first step.
    ("browser_use.dom_vision", {"flash_mode": False}),
    ("browser_use.dom_vision", {"flash_mode": True}),
    ("skyvern.dom", {"prompt_template": "system_prompt"}),
    # The members share the one template; ``dom_vision`` renders its
    # screenshot and coordinate branches, which ``dom`` never reaches.
    ("skyvern.dom_vision", {"prompt_template": "system_prompt"}),
    ("ui_tars", {"prompt_template": "computer_use"}),
    ("ui_tars", {"prompt_template": "grounding"}),
    ("ui_tars", {"prompt_template": "mobile_use"}),
]

#: ``Any``, not ``Callable[..., AgentBase]``: ``prepare`` and
#: ``system_prompt`` are Loop-slot / Protocol surface on the concrete
#: agents, not on the base, and this file walks all three uniformly.
_FACTORIES: dict[str, Any] = {
    "browser_use.dom": BrowserUseDomAgent,
    "browser_use.dom_vision": BrowserUseDomVisionAgent,
    "skyvern.dom": SkyvernDomAgent,
    "skyvern.dom_vision": SkyvernDomVisionAgent,
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


def test_every_prompt_directory_is_exactly_a_family_root():
    on_disk = {
        child.name
        for child in AGENT_PROMPT_ROOT.iterdir()
        if child.is_dir() and not child.name.startswith("__")
    }
    families = {agent_family(key) for key in AgentRegistrar.get_all()}
    assert on_disk <= families, (
        f"Prompt directories {sorted(on_disk - families)} match no agent "
        f"family, so an agent would need a path constant."
    )
    assert on_disk == {agent_family(key) for key in _PREPARE_KWARGS}


@pytest.mark.parametrize("agent_id", sorted(AgentRegistrar.keys()))
def test_prompt_dir_is_derived_from_the_registry_key(agent_id):
    """The evaluator twin, on the agent side (``test_evaluator/test_registrar``).

    Asserts the directory exists, which is what catches an agent registered
    with no prompts at all (the key-set check above only runs the other way).
    """
    directory = agent_prompt_dir(agent_id)
    assert directory == AGENT_PROMPT_ROOT / agent_family(agent_id)
    assert directory.is_dir()


def test_no_agent_carries_its_own_prompt_directory_constant():
    """The regression this module exists to prevent: a fourth literal path."""
    import spectragent.agents.browser_use.dom as browser_use_dom
    import spectragent.agents.browser_use.dom_vision as browser_use_dom_vision
    import spectragent.agents.browser_use.family as browser_use
    import spectragent.agents.skyvern.dom as skyvern_dom
    import spectragent.agents.skyvern.dom_vision as skyvern_dom_vision
    import spectragent.agents.skyvern.family as skyvern
    import spectragent.agents.ui_tars.agent as ui_tars

    for module in (
        browser_use,
        browser_use_dom,
        browser_use_dom_vision,
        skyvern,
        skyvern_dom,
        skyvern_dom_vision,
        ui_tars,
    ):
        assert not hasattr(module, "_PROMPT_DIR"), module.__name__


# ── Loading ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id,name",
    sorted({(a, n) for a, n, _ in _REACHED}),
)
def test_each_prompt_loads_and_hashes(agent_id, name):
    text, ref = load_prompt(agent_id, name)
    assert text.strip()
    assert ref.path == f"agents/{agent_family(agent_id)}/{name}{PROMPT_SUFFIX}"
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
        assert snapshot.ref.path.startswith(f"agents/{agent_family(agent_id)}/"), agent_id


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


@pytest.mark.parametrize("family", sorted({agent_family(key) for key in _PREPARE_KWARGS}))
def test_every_file_on_disk_is_reachable_from_its_family(family):
    """A prompt nothing can load is dead weight that still ships.

    Per family, not per member: the members share one directory, so a file
    counts as reachable when any member's configuration renders it.
    """
    reached = {name for a, name, _ in _REACHED if agent_family(a) == family}
    assert reached == set(agent_prompt_variants(family))


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
    assert set(_literal_options(cls, "prompt_template")) == set(agent_prompt_variants(agent_id))


def test_browser_use_selects_rather_than_naming_its_template():
    """Its choice is a function of the config, so there is no field to type.

    Adding one would let ``flash_mode: true`` and
    ``prompt_template: system_prompt`` disagree in the same YAML.
    """
    for member in (BrowserUseDomAgent, BrowserUseDomVisionAgent):
        assert "prompt_template" not in get_type_hints(member, include_extras=True)
    assert BrowserUseDomAgent(flash_mode=True).select_prompt_variant() == ("system_prompt_flash")


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
    ``test_every_file_on_disk_is_reachable_from_its_family`` would demand a
    config that renders each one on its own.
    """
    assert _SURFACE_DIR.is_dir()
    assert not set(agent_prompt_variants("browser_use")) & {
        p.stem for p in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}")
    }


def test_every_surface_fragment_is_reachable_from_some_member():
    """The same rule as for templates: a file nothing loads still ships."""
    reached: set[str] = set()
    for member in (BrowserUseDomAgent, BrowserUseDomVisionAgent):
        for flash in (True, False):
            agent = member(flash_mode=flash)
            agent.prepare(**_BROWSER_USE_PREPARE)
            snapshot = agent.system_prompt
            assert snapshot is not None
            reached.add(snapshot.params["surface"])  # type: ignore[union-attr]
    # A DOM fragment's one slot holds the serializer's description, as the
    # agent fills it; a rendered surface may also carry an appended
    # coordinate paragraph, so match on "the filled fragment is a prefix of
    # something rendered".
    dom_format = _BROWSER_USE_PREPARE["fragment_formats"]["dom"]
    on_disk = {
        p.read_text("utf-8").strip().replace("{dom_format}", dom_format)
        for p in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}")
    }
    unused = [text for text in on_disk if not any(r.startswith(text) for r in reached)]
    assert not unused, f"{len(unused)} surface fragment(s) no member renders"
    assert not any("{dom_format}" in r for r in reached), "a slot reached the model unfilled"


def test_surface_fragments_substitute_only_the_dom_format():
    """They are values, and ``str.format`` does not recurse into one.

    So the agent fills the one slot every surface has itself: ``{dom_format}``,
    the run's DOM serializer's own description of its text. Any other
    ``{placeholder}`` would reach the model verbatim — the other part of the
    surface that *is* configuration (the coordinate grid) is composed in
    Python, not left as a token in the file.
    """
    for path in _SURFACE_DIR.glob(f"*{PROMPT_SUFFIX}"):
        assert template_variables("format", path.read_text("utf-8")) == {"dom_format"}, path.name
