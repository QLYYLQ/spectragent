"""Author ordinary YAML from selected components and their live configuration schema.

The small browser recipe belongs to AF. Field defaults, descriptions, constraints
and receptor shapes come from Conscribe's public catalog and introspection API.
This module never creates an alternate runtime configuration path.
"""

from __future__ import annotations

import copy
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

import yaml
from conscribe.introspect import wiring_slots
from pydantic import BaseModel

from spectragent.core.composer import ConfigComposer
from spectragent.core.configuration_catalog import ComponentDefinition, ConfigurationCatalog
from spectragent.schemas.recording import RecordingPlan
from spectragent.scripts.generate_all import _collect_registrars

TemplatePath = tuple[str | int, ...]


@dataclass(frozen=True)
class TemplateChoices:
    """Optional authoring choices; omitted choices preserve an input skeleton."""

    agent: str | None = None
    loop: str | None = None
    llm: str | None = None
    model: str | None = None
    environment: str | None = None
    backend: str | None = None
    recording: tuple[str, ...] | None = None
    replayability: str | None = None


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
    """
    composer = composer or ConfigComposer(registrars=_collect_registrars())
    catalog = composer.catalog
    raw = copy.deepcopy(dict(source)) if source is not None else _browser_recipe(catalog)
    origins: dict[TemplatePath, str] = {}
    _mark_origins(raw, (), origins, "input YAML" if source is not None else "AF template default")
    _apply_choices(raw, choices, catalog, origins)

    # Validate before omitting negotiation-only fields: a populated declaration
    # must be rejected, never silently removed by this authoring tool.
    run = composer.compose(raw)
    assert run.top_level_cfg is not None
    definitions = {
        component.config_model: component
        for entries in catalog.components.values()
        for component in entries.values()
    }
    comments: dict[TemplatePath, list[str]] = {}
    expanded = _expand_model(
        run.top_level_cfg,
        (),
        definitions,
        catalog,
        origins,
        comments,
        root_fields=tuple(raw),
        pooled_actions=bool(raw.get("action")),
    )
    # Full expansion can change the set of explicitly set fields (notably nested
    # provider options). Recompose the emitted values, not only the input skeleton.
    composer.compose(expanded)
    header = [
        "# Ordinary Spectragent YAML; edit this file and run config check before execution.",
        "# Selected components and their fields are expanded from the live schema.",
        "# Optional components remain unselected; no browser or model was launched.",
        "# metrics records reported tokens, call counts and timings, not monetary cost.",
        "# replayability, network_source and trace_writer.archive are independent choices.",
        "",
    ]
    return "\n".join(header + _render_mapping(expanded, (), comments, 0)) + "\n"


def _selector(catalog: ConfigurationCatalog, layer: str, key: str) -> dict[str, Any]:
    try:
        definition = catalog.components[layer][key]
    except KeyError:
        available = ", ".join(catalog.components.get(layer, {}))
        raise ValueError(f"Unknown {layer} implementation {key!r}; choose {available}") from None
    return _constant_fields(definition.config_model)


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


def _browser_recipe(catalog: ConfigurationCatalog) -> dict[str, Any]:
    """Visible AF authoring defaults, independent of registry insertion order."""
    return {
        "agent": [
            {
                **_selector(catalog, "agent", "browser_use"),
                "id": "main",
                "agent_loop": _selector(catalog, "agent_loop", "react"),
                "llm": _selector(catalog, "llm", "openai.direct"),
                "memory": _selector(catalog, "memory", "simple"),
            }
        ],
        "environment": [
            {
                **_selector(catalog, "environment", "browser"),
                "id": "browser_env",
                "capabilities": [
                    {
                        **_selector(catalog, "capability", "browser"),
                        "mouse_trajectory": _selector(catalog, "mouse_trajectory", "instant"),
                        "typing_rhythm": _selector(catalog, "typing_rhythm", "instant"),
                        "dom_cleaner": _selector(catalog, "dom_cleaner", "browser_use"),
                        "dom_serializer": _selector(catalog, "dom_serializer", "text"),
                        "replayability": _selector(catalog, "replayability", "none"),
                        "network_source": _selector(catalog, "network_source", "live"),
                        "backend": _selector(catalog, "browser_provider", "local_chrome"),
                    }
                ],
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
    field: str,
    layer: str,
    key: str,
    catalog: ConfigurationCatalog,
    path: TemplatePath,
    origins: dict[TemplatePath, str],
    flag: str,
) -> None:
    selector = _selector(catalog, layer, key)
    previous = owner.get(field)
    if isinstance(previous, dict) and _matches_selector(previous, selector):
        _mark_origins(selector, path, origins, flag)
        return
    # Changing an implementation resets its knobs. Shared receptors and identity
    # survive so changing an Agent does not choose a different model or Loop.
    slots = wiring_slots(catalog.components[layer][key].implementation)
    retained = (
        {
            name: value
            for name, value in (previous or {}).items()
            if name == "id" or (name in slots and not slots[name].declare_only)
        }
        if isinstance(previous, dict)
        else {}
    )
    owner[field] = {**selector, **retained}
    for origin_path in tuple(origins):
        if origin_path[: len(path)] == path and (
            len(origin_path) == len(path) or origin_path[len(path)] not in retained
        ):
            del origins[origin_path]
    _mark_origins(selector, path, origins, flag)


def _matches_selector(previous: dict[str, Any], selector: dict[str, Any]) -> bool:
    return all(
        isinstance(previous.get(key), dict) and _matches_selector(previous[key], value)
        if isinstance(value, dict)
        else previous.get(key) == value
        for key, value in selector.items()
    )


def _apply_choices(
    raw: dict[str, Any],
    choices: TemplateChoices,
    catalog: ConfigurationCatalog,
    origins: dict[TemplatePath, str],
) -> None:
    if any(
        value is not None for value in (choices.agent, choices.loop, choices.llm, choices.model)
    ):
        agent, path = _single(raw, "agent")
        if choices.agent is not None:
            holder = {"selected": agent}
            _replace_component(
                holder, "selected", "agent", choices.agent, catalog, path, origins, "--agent"
            )
            raw["agent"][0] = agent = holder["selected"]
        for field, layer, key, flag in (
            ("agent_loop", "agent_loop", choices.loop, "--loop"),
            ("llm", "llm", choices.llm, "--llm"),
        ):
            if key is not None:
                _replace_component(agent, field, layer, key, catalog, (*path, field), origins, flag)
        if choices.model is not None:
            if not isinstance(agent.get("llm"), dict):
                raise ValueError("--model requires the selected agent's inline llm block")
            agent["llm"]["model"] = choices.model
            origins[(*path, "llm", "model")] = "--model"
    if any(
        value is not None for value in (choices.environment, choices.backend, choices.replayability)
    ):
        environment, path = _single(raw, "environment")
        if choices.environment is not None:
            holder = {"selected": environment}
            _replace_component(
                holder,
                "selected",
                "environment",
                choices.environment,
                catalog,
                path,
                origins,
                "--environment",
            )
            raw["environment"][0] = environment = holder["selected"]
        if choices.backend is not None or choices.replayability is not None:
            capabilities = environment.get("capabilities", [])
            browsers = [
                (index, capability)
                for index, capability in enumerate(capabilities)
                if isinstance(capability, dict) and capability.get("capability_id") == "browser"
            ]
            if len(browsers) != 1:
                raise ValueError(
                    "Browser selection flags require exactly one inline browser capability"
                )
            index, browser = browsers[0]
            browser_path = (*path, "capabilities", index)
            for field, layer, key, flag in (
                ("backend", "browser_provider", choices.backend, "--backend"),
                ("replayability", "replayability", choices.replayability, "--replayability"),
            ):
                if key is not None:
                    _replace_component(
                        browser, field, layer, key, catalog, (*browser_path, field), origins, flag
                    )
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


def _expand_model(
    model: BaseModel,
    path: TemplatePath,
    definitions: Mapping[type[BaseModel], ComponentDefinition],
    catalog: ConfigurationCatalog,
    origins: dict[TemplatePath, str],
    comments: dict[TemplatePath, list[str]],
    *,
    root_fields: tuple[str, ...],
    pooled_actions: bool,
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
        browser_actions = (
            definition is not None
            and definition.layer == "capability"
            and definition.key == "browser"
            and name == "actions"
        )
        # Optional, unselected receptors are not expanded into arbitrary
        # implementations. Browser's own default action set is the domain rule.
        if (
            slot is not None
            and value is None
            and name not in model.model_fields_set
            and not browser_actions
        ):
            continue
        if browser_actions and value is None and not pooled_actions:
            assert slot is not None
            value = [
                catalog.components["action"][key].config_model.model_validate({})
                for key in slot.spec.allowed_keys or ()
            ]
            origins[field_path] = "Browser capability's declared default action set"
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


def _render_mapping(
    value: dict[str, Any], path: TemplatePath, comments: dict[TemplatePath, list[str]], indent: int
) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, child in value.items():
        child_path = (*path, key)
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
