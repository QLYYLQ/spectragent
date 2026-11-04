"""Author ordinary YAML from selected components and their live configuration schema.

The framework's starting choices (the agent, its Loop, model and memory, and
the trace writer) live here. Each capability package declares where its own
block starts and its template flags (``TEMPLATE_BLOCK``, ``TEMPLATE_FLAGS``,
``docs/architecture_design.md`` §4.17 "capability 的模板起点"), so this module
names no capability: the agent's wiring picks the environment, and the
environment's capabilities bring their blocks. Field defaults, descriptions,
constraints and receptor shapes come from Conscribe's public catalog and
introspection API. With a benchmark, the template is composed with it as a run
is, and every capability setting the benchmark supplies is left out of the
YAML, named where it would sit. This module never creates an alternate
runtime configuration path.
"""

from __future__ import annotations

import copy
import json
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, get_args, get_origin

import yaml
from conscribe.introspect import WiringSlot, wiring_slots
from conscribe.wiring import expand_relative_keys
from pydantic import BaseModel, JsonValue

from spectragent.benchmarks.selection import read_benchmark, run_root
from spectragent.core.actions._registrar import ACTION_KEY_SEPARATOR
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.capability_merge import (
    CAPABILITY_SECTION,
    ENVIRONMENT_CAPABILITIES,
    ENVIRONMENT_SECTION,
    SettingPath,
    SuppliedSetting,
    written,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.configuration_catalog import ComponentDefinition, ConfigurationCatalog
from spectragent.core.negotiation._base import extract_wiring
from spectragent.core.negotiation.action import loop_actions
from spectragent.core.negotiation.observation import loop_fragments
from spectragent.core.registries import capability_declarations, template_flags
from spectragent.schemas.observation._registrar import FRAGMENT_KEY_SEPARATOR
from spectragent.schemas.recording import RecordingPlan

TemplatePath = tuple[str | int, ...]

#: The agent a new template starts with; its wiring picks the environment.
STARTING_AGENT = "browser_use.dom"
#: Where a new template starts the agent's receptors, by receptor. An agent
#: whose wiring takes one other key gets that key (M3A's memory).
STARTING_RECEPTORS = MappingProxyType(
    {"agent_loop": "react", "llm": "openai.direct", "memory": "simple"}
)
#: The source comment of every starting choice the template writes.
TEMPLATE_DEFAULT = "AF template default"
#: How long a left-out value's short form may be before it is cut.
_SHOWN = 60


@dataclass(frozen=True)
class TemplateChoices:
    """Optional authoring choices; omitted choices preserve an input skeleton."""

    agent: str | None = None
    loop: str | None = None
    llm: str | None = None
    model: str | None = None
    environment: str | None = None
    recording: tuple[str, ...] | None = None
    #: The capabilities' template flags given, flag → registry key (``--replayability``: ``pinned``).
    capability_flags: Mapping[str, str] = field(default_factory=dict)
    #: ``--benchmark``: a shipped alias or the path of a benchmark YAML.
    benchmark: str | None = None


@dataclass(frozen=True)
class _LeftOut:
    """A setting the benchmark supplies: rendered as a comment where it would sit, never as YAML."""

    note: str


def render_config_template(
    choices: TemplateChoices,
    source: Mapping[str, Any] | None = None,
    *,
    composer: ConfigComposer | None = None,
) -> str:
    """Expand the selected graph, validate composition and return commented YAML.

    No environment is started, model invoked or task evidence opened. Task-specific
    preparation still belongs to ``config check`` / ``run --dry-run``. An input
    skeleton keeps all its values except the explicitly selected component blocks.
    With a benchmark, both validations compose the benchmark too, and the YAML
    leaves out what it supplies.
    """
    composer = composer or ConfigComposer.for_every_package()
    catalog = composer.catalog
    benchmarks = [read_benchmark(choices.benchmark)] if choices.benchmark is not None else []
    origins: dict[TemplatePath, str] = {}
    if source is None:
        raw = _new_template(choices, composer, benchmarks, origins)
        # The new template already holds these selections.
        choices = replace(choices, agent=None, loop=None, llm=None, environment=None)
    else:
        raw = copy.deepcopy(dict(source))
        _mark_origins(raw, (), origins, "input YAML")
    _apply_choices(raw, choices, composer, benchmarks, origins)
    # A benchmark section in the skeleton is refused when a benchmark is
    # selected, as a run refuses one; without --benchmark any root expands.
    root = run_root(raw, *benchmarks) if benchmarks else raw

    # Validate before omitting negotiation-only fields: a populated declaration
    # must be rejected, never silently removed by this authoring tool.
    run = composer.compose(root)
    assert run.top_level_cfg is not None
    supplied = composer.merge_capability_parts(root).supplied
    _mark_suppliers(supplied, origins)
    definitions = {
        component.config_model: component
        for entries in catalog.components.values()
        for component in entries.values()
    }
    pooled_actions = frozenset(
        definitions[type(action)].key for action in getattr(run.top_level_cfg, "action", ()) or ()
    )
    comments: dict[TemplatePath, list[str]] = {}
    expanded = _expand_model(
        run.top_level_cfg,
        (),
        definitions,
        catalog,
        origins,
        comments,
        root_fields=tuple(raw),
        pooled_actions=pooled_actions,
    )
    for setting in supplied:
        _leave_out(expanded, setting, comments)
    # Full expansion can change the set of explicitly set fields (notably nested
    # provider options). Recompose the emitted values, not only the input skeleton.
    emitted = _without_left_out(expanded)
    composer.compose(run_root(emitted, *benchmarks) if benchmarks else emitted)
    header = [
        "# Ordinary Spectragent YAML; edit this file and run config check before execution.",
        "# Selected components and their fields are expanded from the live schema.",
        "# Optional components remain unselected; no environment or model was launched.",
        "# metrics records reported tokens, call counts and timings, not monetary cost.",
    ]
    if benchmarks:
        header += [
            f"# Use it with --benchmark {choices.benchmark}: the capability settings that "
            "benchmark supplies",
            "# are left out, each named in a comment where it would sit.",
        ]
    return "\n".join([*header, "", *_render_mapping(expanded, (), comments, 0)]) + "\n"


# -- choosing ---------------------------------------------------------


def _definition(catalog: ConfigurationCatalog, layer: str, key: str) -> ComponentDefinition:
    try:
        return catalog.components[layer][key]
    except KeyError:
        available = ", ".join(catalog.components.get(layer, {}))
        raise ValueError(f"Unknown {layer} implementation {key!r}; choose {available}") from None


def _selector(catalog: ConfigurationCatalog, layer: str, key: str) -> dict[str, Any]:
    return _constant_fields(_definition(catalog, layer, key).config_model)


def _constant_fields(model: type[BaseModel]) -> dict[str, Any]:
    """Read discriminator constants, including Conscribe's nested selectors."""
    values: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if get_origin(annotation) is Literal and len(get_args(annotation)) == 1:
            values[info.alias or name] = get_args(annotation)[0]
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = _constant_fields(annotation)
            if nested:
                values[info.alias or name] = nested
    return values


def _receptors(owner: type, layer: str) -> list[WiringSlot]:
    """*owner*'s receptors of *layer* that take instances (not negotiation declarations)."""
    return [
        slot
        for slot in wiring_slots(owner).values()
        if slot.slot is not None and not slot.declare_only and slot.spec.registry_name == layer
    ]


def _starting_key(owner: type, receptor: WiringSlot) -> str:
    """The template's starting choice for *receptor* of *owner*: the framework's, else its wiring's only key.

    Raises:
        ValueError: The wiring takes several keys and not the framework's starting one.
    """
    start = STARTING_RECEPTORS.get(receptor.name)
    allowed = receptor.spec.allowed_keys
    if start is not None and (allowed is None or start in allowed):
        return start
    if allowed is not None and len(allowed) == 1:
        return allowed[0]
    raise ValueError(
        f"{owner.__name__} takes {', '.join(allowed or ())} as its {receptor.name}, and the "
        f"template has no starting choice among them; write the one you want in the YAML and "
        f"pass it with --from"
    )


def _environment_for(catalog: ConfigurationCatalog, agent: str, loop: str) -> str:
    """The one environment whose capabilities, with the Loop, provide what *agent* and *loop* require.

    A preselection on class declarations, read as negotiation reads them
    (relative keys expanded by ``expand_relative_keys``): whether the selected
    instances cooperate is still ``prepare()``'s answer (``config check``).

    Raises:
        ValueError: No environment, or more than one, provides it: ``--environment`` decides.
    """
    agent_cls = _definition(catalog, "agent", agent).implementation
    loop_cls = _definition(catalog, "agent_loop", loop).implementation
    axes = {
        "observation": (FRAGMENT_KEY_SEPARATOR, loop_fragments(loop_cls)),
        "action": (ACTION_KEY_SEPARATOR, loop_actions(loop_cls)),
    }
    required = {
        registry: extract_wiring(agent_cls, registry)[0] | extract_wiring(loop_cls, registry)[0]
        for registry in axes
    }
    candidates: list[str] = []
    for environment, definition in catalog.components.get(ENVIRONMENT_SECTION, {}).items():
        capabilities = [
            catalog.components[CAPABILITY_SECTION][key].implementation
            for key in _capability_keys(definition.implementation)
        ]
        served = True
        for registry, (separator, provided) in axes.items():
            universe = set(provided)
            for capability in capabilities:
                declared_required, declared_optional = extract_wiring(capability, registry)
                universe |= declared_required | declared_optional
            served = served and all(
                _served(declared, separator, universe) for declared in required[registry]
            )
        if served:
            candidates.append(environment)
    if len(candidates) == 1:
        return candidates[0]
    known = ", ".join(sorted(catalog.components.get(ENVIRONMENT_SECTION, {})))
    found = (
        f"its wiring can run in {', '.join(sorted(candidates))}"
        if candidates
        else f"no environment's capabilities provide what its wiring requires (environments: {known})"
    )
    raise ValueError(f"--agent {agent}: {found}; choose one with --environment")


def _served(declared: str, separator: str, provided: set[str]) -> bool:
    universe = sorted(provided)
    return any(key in provided for key in expand_relative_keys([declared], universe, separator))


def _capability_keys(environment: type) -> tuple[str, ...]:
    """The capabilities *environment*'s receptor takes, in declared order."""
    return tuple(
        key
        for slot in _receptors(environment, CAPABILITY_SECTION)
        for key in slot.spec.allowed_keys or ()
    )


def _capability_blocks(
    composer: ConfigComposer, environment: str, benchmarks: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One block per capability *environment* takes, each from its package's starting block.

    A setting the benchmarks supply is taken out of the starting block, so the
    template's starting choice never conflicts with it (Android's placeholder
    AVD gives way to AndroidWorld's).
    """
    catalog = composer.catalog
    keys = _capability_keys(_definition(catalog, ENVIRONMENT_SECTION, environment).implementation)
    starts = {item.capability_id: item.template_block for item in capability_declarations(keys)}
    blocks = [
        {**_selector(catalog, CAPABILITY_SECTION, key), **copy.deepcopy(dict(starts[key]))}
        for key in keys
    ]
    if benchmarks:
        # Blocks naming only their capability: everything the benchmark writes
        # is then what it supplies, its strategies' names included.
        named = CapabilityRegistrar.discriminator_field
        bare = {
            ENVIRONMENT_SECTION: [
                {
                    **_selector(catalog, ENVIRONMENT_SECTION, environment),
                    ENVIRONMENT_CAPABILITIES: [{named: key} for key in keys],
                }
            ],
            "benchmark": list(benchmarks),
        }
        for setting in composer.merge_capability_parts(bare).supplied:
            position = setting.block[-1]
            assert isinstance(position, int)
            _drop(blocks[position], setting.path)
    return blocks


def _drop(block: dict[str, Any], path: SettingPath) -> None:
    """Take the setting at *path* out of a starting block, and every block it leaves empty."""
    head, *rest = path
    if not isinstance(head, str) or head not in block:
        return
    if rest:
        inner = block[head]
        if isinstance(inner, dict):
            _drop(inner, tuple(rest))
            if not inner:
                del block[head]
    else:
        del block[head]


def _new_template(
    choices: TemplateChoices,
    composer: ConfigComposer,
    benchmarks: Sequence[Mapping[str, Any]],
    origins: dict[TemplatePath, str],
) -> dict[str, Any]:
    """The starting graph, with the selected agent, Loop, model and environment.

    The agent block starts from the agent's own declared values
    (``__template_block__``); a receptor it leaves out starts from the
    framework's starting choice. ``--loop`` and ``--llm`` then replace their
    receptors as on a skeleton: naming the declared implementation keeps its
    values.
    """
    catalog = composer.catalog
    agent_key = choices.agent or STARTING_AGENT
    agent_cls = _definition(catalog, "agent", agent_key).implementation
    agent: dict[str, Any] = {
        **_selector(catalog, "agent", agent_key),
        "id": "main",
        **_declared_block(agent_cls),
    }
    for receptor in wiring_slots(agent_cls).values():
        if receptor.name in STARTING_RECEPTORS and receptor.slot is not None:
            agent.setdefault(
                receptor.name,
                _selector(catalog, receptor.spec.registry_name, _starting_key(agent_cls, receptor)),
            )
    loop_key = choices.loop or _key_of(catalog, "agent_loop", agent.get("agent_loop"))
    environment_key = choices.environment or _environment_for(catalog, agent_key, loop_key)
    raw: dict[str, Any] = {
        "agent": [agent],
        ENVIRONMENT_SECTION: [
            {
                **_selector(catalog, ENVIRONMENT_SECTION, environment_key),
                "id": f"{environment_key}_env",
                ENVIRONMENT_CAPABILITIES: _capability_blocks(composer, environment_key, benchmarks),
            }
        ],
        "hook": [
            {
                **_selector(catalog, "hook", "trace_writer"),
                "recording": _recording_plan(("metrics",)),
                "archive": [],
            }
        ],
    }
    _mark_origins(raw, (), origins, TEMPLATE_DEFAULT)
    for given, layer, path, flag in (
        (choices.agent, "agent", ("agent", 0), "--agent"),
        (choices.environment, ENVIRONMENT_SECTION, (ENVIRONMENT_SECTION, 0), "--environment"),
    ):
        if given is not None:
            _mark_origins(_selector(catalog, layer, given), path, origins, flag)
    for field_name, key, flag in (
        ("agent_loop", choices.loop, "--loop"),
        ("llm", choices.llm, "--llm"),
    ):
        if key is not None:
            _replace_component(
                agent,
                field_name,
                field_name,
                key,
                composer,
                benchmarks,
                ("agent", 0, field_name),
                origins,
                flag,
            )
    return raw


def _declared_block(owner: type) -> dict[str, Any]:
    """*owner*'s own template starting values (an agent's ``__template_block__``), a fresh copy."""
    return copy.deepcopy(dict(getattr(owner, "__template_block__", {})))


def _key_of(catalog: ConfigurationCatalog, layer: str, block: object) -> str:
    """The implementation of *layer* that *block* names; the framework's starting key without one."""
    if isinstance(block, dict):
        for key, definition in catalog.components.get(layer, {}).items():
            if _matches_selector(block, _constant_fields(definition.config_model)):
                return key
    return STARTING_RECEPTORS[layer]


def _recording_plan(channels: Sequence[str]) -> dict[str, bool]:
    fields = tuple(RecordingPlan.model_fields)
    selected = set(channels)
    unknown = selected - {*fields, "all", "none"}
    if unknown:
        raise ValueError(f"Unknown recording channels: {', '.join(sorted(unknown))}")
    if selected.intersection({"all", "none"}) and len(selected) != 1:
        raise ValueError("--record all or none cannot be combined with other channels")
    return {name: "all" in selected or name in selected for name in fields}


def _single(raw: dict[str, Any], layer: str) -> tuple[dict[str, Any], TemplatePath]:
    entries = raw.get(layer)
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError(
            f"Selection flags require exactly one {layer} block; edit the YAML skeleton"
        )
    return entries[0], (layer, 0)


def _replace_component(
    owner: dict[str, Any],
    field_name: str,
    layer: str,
    key: str,
    composer: ConfigComposer,
    benchmarks: Sequence[Mapping[str, Any]],
    path: TemplatePath,
    origins: dict[TemplatePath, str],
    flag: str,
) -> None:
    catalog = composer.catalog
    selector = _selector(catalog, layer, key)
    previous = owner.get(field_name)
    if isinstance(previous, dict) and _matches_selector(previous, selector):
        _mark_origins(selector, path, origins, flag)
        return
    # Changing an implementation resets its knobs. Shared receptors and identity
    # survive so changing an Agent does not choose a different model or Loop;
    # a receptor value the new implementation's wiring does not take is
    # replaced by the template's starting choice.
    implementation = catalog.components[layer][key].implementation
    slots = wiring_slots(implementation)
    retained = (
        {
            name: value
            for name, value in previous.items()
            if name == "id" or (name in slots and not slots[name].declare_only)
        }
        if isinstance(previous, dict)
        else {}
    )
    restarted = {
        name: _starting_value(composer, benchmarks, implementation, key, slots[name])
        for name, value in retained.items()
        if name != "id" and not _accepted(catalog, slots[name], value)
    }
    retained.update(restarted)
    # What the skeleton does not keep starts from the new implementation's own
    # declared values (an agent's ``__template_block__``).
    declared = {
        name: value
        for name, value in _declared_block(implementation).items()
        if name not in retained
    }
    owner[field_name] = {**selector, **declared, **retained}
    for origin_path in tuple(origins):
        if origin_path[: len(path)] == path and (
            len(origin_path) == len(path)
            or origin_path[len(path)] not in retained
            or origin_path[len(path)] in restarted
        ):
            del origins[origin_path]
    for name, value in {**declared, **restarted}.items():
        _mark_origins(value, (*path, name), origins, TEMPLATE_DEFAULT)
    _mark_origins(selector, path, origins, flag)


def _accepted(catalog: ConfigurationCatalog, receptor: WiringSlot, value: object) -> bool:
    """Whether *receptor*'s wiring takes the block or blocks of *value*."""
    allowed = receptor.spec.allowed_keys
    if allowed is None:
        return True
    implementations = catalog.components.get(receptor.spec.registry_name, {})
    selectors = [
        _constant_fields(implementations[key].config_model)
        for key in allowed
        if key in implementations
    ]
    values = value if isinstance(value, list) else [value]
    return all(
        not isinstance(item, dict) or any(_matches_selector(item, chosen) for chosen in selectors)
        for item in values
    )


def _starting_value(
    composer: ConfigComposer,
    benchmarks: Sequence[Mapping[str, Any]],
    owner: type,
    owner_key: str,
    receptor: WiringSlot,
) -> object:
    """The template's starting choice for *receptor*.

    An environment's capability blocks; else the block *owner* declares for it
    (an agent's ``__template_block__``); else one strategy (:func:`_starting_key`).
    """
    if receptor.spec.registry_name == CAPABILITY_SECTION:
        return _capability_blocks(composer, owner_key, benchmarks)
    declared = _declared_block(owner).get(receptor.name)
    if declared is not None:
        return declared
    return _selector(composer.catalog, receptor.spec.registry_name, _starting_key(owner, receptor))


def _matches_selector(previous: dict[str, Any], selector: dict[str, Any]) -> bool:
    """Whether *previous* is a block of the implementation *selector* names.

    A nested selector the block leaves out (a branch Loop's omittable
    ``restorer``) does not tell implementations apart; one it writes must match.
    """
    return all(
        key not in previous
        or (isinstance(previous[key], dict) and _matches_selector(previous[key], value))
        if isinstance(value, dict)
        else previous.get(key) == value
        for key, value in selector.items()
    )


def _apply_choices(
    raw: dict[str, Any],
    choices: TemplateChoices,
    composer: ConfigComposer,
    benchmarks: Sequence[Mapping[str, Any]],
    origins: dict[TemplatePath, str],
) -> None:
    catalog = composer.catalog
    if any(
        value is not None for value in (choices.agent, choices.loop, choices.llm, choices.model)
    ):
        agent, path = _single(raw, "agent")
        if choices.agent is not None:
            holder = {"selected": agent}
            _replace_component(
                holder,
                "selected",
                "agent",
                choices.agent,
                composer,
                benchmarks,
                path,
                origins,
                "--agent",
            )
            raw["agent"][0] = agent = holder["selected"]
        for field_name, layer, key, flag in (
            ("agent_loop", "agent_loop", choices.loop, "--loop"),
            ("llm", "llm", choices.llm, "--llm"),
        ):
            if key is not None:
                _replace_component(
                    agent,
                    field_name,
                    layer,
                    key,
                    composer,
                    benchmarks,
                    (*path, field_name),
                    origins,
                    flag,
                )
        if choices.model is not None:
            if not isinstance(agent.get("llm"), dict):
                raise ValueError("--model requires the selected agent's inline llm block")
            agent["llm"]["model"] = choices.model
            origins[(*path, "llm", "model")] = "--model"
    if choices.environment is not None or choices.capability_flags:
        environment, path = _single(raw, ENVIRONMENT_SECTION)
        if choices.environment is not None:
            holder = {"selected": environment}
            _replace_component(
                holder,
                "selected",
                ENVIRONMENT_SECTION,
                choices.environment,
                composer,
                benchmarks,
                path,
                origins,
                "--environment",
            )
            raw[ENVIRONMENT_SECTION][0] = environment = holder["selected"]
        _apply_capability_flags(environment, path, choices, composer, benchmarks, origins)
    if choices.recording is not None:
        hooks = raw.setdefault("hook", [])
        writers = [
            (index, hook)
            for index, hook in enumerate(hooks)
            if isinstance(hook, dict) and hook.get("hook_id") == "trace_writer"
        ]
        if len(writers) > 1:
            raise ValueError("--record requires at most one trace_writer; edit the YAML skeleton")
        if not writers:
            hooks.append(_selector(catalog, "hook", "trace_writer"))
            writers = [(len(hooks) - 1, hooks[-1])]
        index, writer = writers[0]
        writer["recording"] = _recording_plan(choices.recording)
        _mark_origins(writer["recording"], ("hook", index, "recording"), origins, "--record")


def _apply_capability_flags(
    environment: dict[str, Any],
    path: TemplatePath,
    choices: TemplateChoices,
    composer: ConfigComposer,
    benchmarks: Sequence[Mapping[str, Any]],
    origins: dict[TemplatePath, str],
) -> None:
    """Each given template flag replaces the strategy in its receptor of its capability's block."""
    owners = {
        flag.flag: (capability_id, flag.receptor)
        for capability_id, flags in template_flags().items()
        for flag in flags
    }
    for flag, key in choices.capability_flags.items():
        if flag not in owners:
            raise ValueError(f"{flag} is no capability's template flag")
        capability_id, receptor = owners[flag]
        held = environment.get(ENVIRONMENT_CAPABILITIES, [])
        blocks = [
            (index, block)
            for index, block in enumerate(held if isinstance(held, list) else [])
            if isinstance(block, dict) and block.get("capability_id") == capability_id
        ]
        if len(blocks) != 1:
            raise ValueError(
                f"{flag} is a template flag of the {capability_id} capability, and it needs "
                f"exactly one inline {capability_id} capability block in the environment "
                f"(found {len(blocks)})"
            )
        index, block = blocks[0]
        implementation = composer.catalog.components[CAPABILITY_SECTION][capability_id]
        layer = wiring_slots(implementation.implementation)[receptor].spec.registry_name
        _replace_component(
            block,
            receptor,
            layer,
            key,
            composer,
            benchmarks,
            (*path, ENVIRONMENT_CAPABILITIES, index, receptor),
            origins,
            flag,
        )


def _mark_origins(
    value: Any, path: TemplatePath, origins: dict[TemplatePath, str], origin: str
) -> None:
    origins[path] = origin
    if isinstance(value, dict):
        for key, child in value.items():
            _mark_origins(child, (*path, key), origins, origin)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _mark_origins(child, (*path, index), origins, origin)


def _mark_suppliers(supplied: Sequence[SuppliedSetting], origins: dict[TemplatePath, str]) -> None:
    """A block the run configuration leaves out and a benchmark fills (``backend``) is sourced to it.

    Only a block of fields: a list (``actions``) a part sets options on is the
    capability's default list, sourced as such when it is expanded.
    """
    for setting in supplied:
        path: TemplatePath = setting.block
        for part, inner in zip(setting.path, setting.path[1:], strict=False):
            if not (isinstance(part, str) and isinstance(inner, str)):
                break
            path = (*path, part)
            origins.setdefault(path, setting.source)


# -- expanding --------------------------------------------------------


def _expand_model(
    model: BaseModel,
    path: TemplatePath,
    definitions: Mapping[type[BaseModel], ComponentDefinition],
    catalog: ConfigurationCatalog,
    origins: dict[TemplatePath, str],
    comments: dict[TemplatePath, list[str]],
    *,
    root_fields: tuple[str, ...],
    pooled_actions: frozenset[str],
) -> dict[str, Any]:
    definition = definitions.get(type(model))
    slots = wiring_slots(definition.implementation) if definition else {}
    serialized = model.model_dump(mode="json", by_alias=True)
    properties = type(model).model_json_schema().get("properties", {})
    result: dict[str, Any] = {}
    fields = type(model).model_fields
    constants = _constant_fields(type(model)) if definition else {}
    ordered = (
        sorted(fields, key=lambda name: (fields[name].alias or name) not in constants)
        if path
        else root_fields
    )
    for name in ordered:
        info = fields[name]
        key = info.alias or name
        if not path and name not in root_fields:
            continue
        slot = slots.get(name)
        if slot is not None and (slot.declare_only or slot.slot is None):
            continue
        value = getattr(model, name)
        field_path = (*path, key)
        # A capability's list of actions, known by its layer, not its name.
        capability_actions = (
            definition is not None
            and definition.layer == CAPABILITY_SECTION
            and slot is not None
            and slot.spec.registry_name == "action"
            and slot.slot is not None
            and slot.slot.shape == "list"
        )
        # Optional, unselected receptors are not expanded into arbitrary
        # implementations. A capability's own default action set is the domain rule.
        if (
            slot is not None
            and value is None
            and name not in model.model_fields_set
            and not capability_actions
        ):
            continue
        if capability_actions:
            assert definition is not None and slot is not None
            default_set = f"{definition.key.capitalize()} capability's declared default action set"
            if value is None and not pooled_actions.intersection(slot.spec.allowed_keys or ()):
                value = [
                    catalog.components["action"][key].config_model.model_validate({})
                    for key in slot.spec.allowed_keys or ()
                ]
                origins[field_path] = default_set
            elif value is not None and field_path not in origins:
                # The default list a benchmark's part was merged into.
                origins[field_path] = default_set
        if (
            definition
            and definition.layer == "hook"
            and definition.key == "trace_writer"
            and name == "recording"
            and value is None
        ):
            value = RecordingPlan()
            origins[field_path] = "TraceWriter's default RecordingPlan"
        lines = [f"Source: {origins.get(field_path, 'schema default')}."]
        if info.description:
            lines.extend(textwrap.wrap(" ".join(info.description.split()), width=88))
        constraints = _constraints(properties.get(key, {}))
        if constraints:
            lines.extend(textwrap.wrap("Constraints: " + str(constraints), width=88))
        if info.is_required():
            lines.append("Required by the selected component.")
        comments[field_path] = lines
        if isinstance(value, BaseModel):
            result[key] = _expand_model(
                value,
                field_path,
                definitions,
                catalog,
                origins,
                comments,
                root_fields=root_fields,
                pooled_actions=pooled_actions,
            )
        elif isinstance(value, list) and any(isinstance(item, BaseModel) for item in value):
            result[key] = [
                _expand_model(
                    item,
                    (*field_path, index),
                    definitions,
                    catalog,
                    origins,
                    comments,
                    root_fields=root_fields,
                    pooled_actions=pooled_actions,
                )
                if isinstance(item, BaseModel)
                else serialized[key][index]
                for index, item in enumerate(value)
            ]
        else:
            result[key] = serialized[key]
    return result


def _constraints(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep scalar constraints, including the alternatives of nullable fields."""
    result = {
        name: schema[name]
        for name in (
            "type",
            "enum",
            "const",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minProperties",
            "maxProperties",
        )
        if name in schema
    }
    for name in ("anyOf", "oneOf", "allOf"):
        alternatives = [_constraints(item) for item in schema.get(name, [])]
        if alternatives and all(alternatives):
            result[name] = alternatives
    return result


# -- leaving out what a benchmark supplies ----------------------------


def _leave_out(
    expanded: dict[str, Any], setting: SuppliedSetting, comments: dict[TemplatePath, list[str]]
) -> None:
    """Replace the expanded value of *setting* by a note naming it, its value and its benchmark.

    An entry of a list (``actions[android.open_app]``) is found by its
    discriminator. The setting's last part is always a field.
    """
    *inner, last = [*setting.block, *setting.path]
    node: object = expanded
    path: TemplatePath = ()
    for part in inner:
        if isinstance(part, tuple):
            field_name, key = part
            entries = node if isinstance(node, list) else []
            part = next(
                (
                    index
                    for index, item in enumerate(entries)
                    if isinstance(item, dict) and item.get(field_name) == key
                ),
                -1,
            )
        # Two branches: each narrows the node for its own kind of index.
        if isinstance(part, int) and isinstance(node, list) and 0 <= part < len(node):  # noqa: SIM114
            node = node[part]
        elif isinstance(part, str) and isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return
        path = (*path, part)
    if not (isinstance(last, str) and isinstance(node, dict) and last in node):
        return
    node[last] = _LeftOut(
        f"{written(setting.path)}: {_short(setting.value)}, from {setting.source}; left out here."
    )
    target = (*path, last)
    for commented in tuple(comments):
        if commented[: len(target)] == target:
            del comments[commented]


def _short(value: JsonValue) -> str:
    if isinstance(value, dict):
        return f"{len(value)} entries"
    if isinstance(value, list):
        return f"{len(value)} items"
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= _SHOWN else f"{text[: _SHOWN - 3]}..."


def _all_left_out(value: object) -> bool:
    """A non-empty block every setting of which a benchmark supplies."""
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(child, _LeftOut) or _all_left_out(child) for child in value.values())
    )


def _left_out_notes(value: dict[str, Any]) -> list[str]:
    return [
        note
        for child in value.values()
        for note in ([child.note] if isinstance(child, _LeftOut) else _left_out_notes(child))
    ]


def _without_left_out(value: Any) -> Any:
    """*value* as the YAML writes it: no left-out setting, and no block emptied by them."""
    if isinstance(value, dict):
        return {
            key: _without_left_out(child)
            for key, child in value.items()
            if not isinstance(child, _LeftOut) and not _all_left_out(child)
        }
    if isinstance(value, list):
        return [_without_left_out(item) for item in value]
    return value


# -- rendering --------------------------------------------------------


def _render_mapping(
    value: dict[str, Any], path: TemplatePath, comments: dict[TemplatePath, list[str]], indent: int
) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, child in value.items():
        child_path = (*path, key)
        if isinstance(child, _LeftOut) or _all_left_out(child):
            notes = [child.note] if isinstance(child, _LeftOut) else _left_out_notes(child)
            lines.extend(prefix + "# " + note for note in notes)
            continue
        lines.extend(prefix + "# " + comment for comment in comments.get(child_path, []))
        if isinstance(child, (dict, list)) and child and "\n" not in str(key):
            label = (
                yaml.safe_dump({key: None}, allow_unicode=True)
                .splitlines()[0]
                .removesuffix(" null")
            )
            lines.append(prefix + label)
            if isinstance(child, dict):
                lines.extend(_render_mapping(child, child_path, comments, indent + 2))
            else:
                for index, item in enumerate(child):
                    if isinstance(item, dict) and item:
                        lines.append(prefix + "  -")
                        lines.extend(
                            _render_mapping(item, (*child_path, index), comments, indent + 4)
                        )
                    else:
                        lines.extend(
                            prefix + "  " + line
                            for line in yaml.safe_dump(
                                [item], allow_unicode=True, sort_keys=False
                            ).splitlines()
                        )
        else:
            lines.extend(
                prefix + line
                for line in yaml.safe_dump(
                    {key: child}, allow_unicode=True, sort_keys=False
                ).splitlines()
            )
    return lines
