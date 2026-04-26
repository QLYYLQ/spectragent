"""ConfigComposer — turn a validated YAML/dict into fully-wired runtime instances.

conscribe 1.0.0 gives us four pieces:

1. ``build_composed_config(inline_wiring=True)``: produces a top-level
   Pydantic model whose wired fields are *inlined* as nested discriminated
   unions of the target layer's per-key models.
2. ``parse_wiring(cls)``: normalized wiring specs (Mode 1/2/3).
3. ``WiredField`` descriptor: raises a clear ``AttributeError`` on read if
   the framework forgot to inject.
4. ``resolve_wiring(cls)``: reports whether each wiring entry is an
   ``__init__`` param or a setattr-injected receptor.

ConfigComposer stitches them together. The algorithm is three passes:
validate → leaves-first instantiation (recursive into inlined child
configs) → wired setattr. Multi-instance receptors (``dict[str, X]`` or
``list[X]`` annotations) are detected by :func:`typing.get_type_hints`.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from functools import cached_property
from typing import Any, get_origin

from conscribe import build_composed_config, discover
from conscribe.wiring import WiredField, parse_wiring
from pydantic import BaseModel


class ComposeError(Exception):
    """Base class for Composer failures."""


class InstantiationError(ComposeError):
    """Failed to construct a class instance from its config entry."""


class UnresolvedWiringError(ComposeError):
    """A wired receptor could not be populated."""


@dataclass(frozen=True)
class ComposedRun:
    """Fully assembled, setattr-injected object graph ready for AgentRuntime.

    The raw source of truth is ``by_layer``: ``{layer_name: {registry_key: instance}}``.
    Convenience accessors (``agent``, ``loop``, ``env``, ``llm``, ``memory``,
    ``hooks``) assume the standard spectragent layer naming and look up
    through ``by_layer`` — preferring a value wired on the agent, falling
    back to a lone top-level entry.

    ``top_level_cfg`` is the validated Pydantic model the Composer built
    from raw YAML. It defaults to ``None`` for tests that assemble a
    ``ComposedRun`` by hand without going through Pydantic validation.
    """

    by_layer: dict[str, dict[str, Any]]
    top_level_cfg: BaseModel | None = None

    # -- Raw accessors --
    def instances(self, layer: str) -> list[Any]:
        return list(self.by_layer.get(layer, {}).values())

    def single(self, layer: str) -> Any:
        items = self.instances(layer)
        if len(items) != 1:
            raise ComposeError(f"Layer {layer!r}: expected exactly 1 instance, got {len(items)}.")
        return items[0]

    def optional_single(self, layer: str) -> Any | None:
        items = self.instances(layer)
        if len(items) > 1:
            raise ComposeError(f"Layer {layer!r}: expected at most 1 instance, got {len(items)}.")
        return items[0] if items else None

    # -- Conventional accessors (standard spectragent layers) --
    @property
    def agent(self) -> Any:
        return self.single("agent")

    @property
    def loop(self) -> Any:
        return _pick_wired_or_single(self.agent, "agent_loop", self, "agent_loop")

    @property
    def env(self) -> Any | None:
        if "environment" not in self.by_layer:
            return None
        return _pick_wired_or_single(self.agent, "env", self, "environment")

    @property
    def llm(self) -> Any:
        return _pick_wired_or_single(self.agent, "llm", self, "llm")

    @property
    def memory(self) -> Any:
        return _pick_wired_or_single(self.agent, "memory", self, "memory")

    @property
    def hooks(self) -> list[Any]:
        return self.instances("hook")


def _pick_wired_or_single(
    agent: Any,
    attr: str,
    run: ComposedRun,
    layer: str,
) -> Any:
    try:
        val = getattr(agent, attr)
    except AttributeError:
        val = None
    if val is not None:
        return val
    return run.optional_single(layer)


class ConfigComposer:
    """Validates a dict against the composed schema and materialises instances.

    Example::

        composer = ConfigComposer(
            registrars={
                "agent": AgentRegistrar,
                "agent_loop": AgentLoopRegistrar,
                "llm": LLMRegistrar,
                "memory": MemoryRegistrar,
                "capability": CapabilityRegistrar,
                "hook": HookRegistrar,
            },
        )
        run = composer.compose(yaml.safe_load(Path("cfg.yaml").read_text()))
        result = await AgentRuntime.from_composed(run).execute(run, task="...")
    """

    def __init__(
        self,
        *,
        registrars: dict[str, Any],
        discover_roots: tuple[str, ...] = (
            "spectragent.agents",
            "spectragent.core",
            "spectragent.schemas",
        ),
    ) -> None:
        # Discover concrete implementation modules to populate registries.
        # ``spectragent.generated`` is intentionally excluded: its
        # ``_composed.py`` is a codegen artefact that re-declares conscribe
        # models and can fail to import mid-regeneration.
        for root in discover_roots:
            discover(root)
        if not registrars:
            raise ComposeError("ConfigComposer requires at least one registrar.")
        for layer, reg in registrars.items():
            if not reg.keys():
                raise ComposeError(
                    f"Layer {layer!r} has no registered implementations — "
                    f"``build_composed_config`` cannot build a Union of no types. "
                    f"Either register at least one subclass or omit this layer."
                )
        self._registrars = registrars
        self._composed = build_composed_config(registrars, inline_wiring=True)
        # Reverse index: per-key Pydantic model → (layer_name, registry_key).
        self._model_to_lk: dict[type[BaseModel], tuple[str, str]] = {}
        for layer, result in self._composed.layer_results.items():
            for key, model in result.per_key_models.items():
                self._model_to_lk[model] = (layer, key)

    @cached_property
    def top_level_type(self) -> type[BaseModel]:
        return self._composed.top_level_type

    @property
    def dependency_order(self) -> list[str]:
        return list(self._composed.dependency_order)

    # -- Main entry point --------------------------------------------

    def compose(self, raw: dict | BaseModel) -> ComposedRun:
        """Validate, instantiate (leaves-first), inject, assemble."""
        cfg = raw if isinstance(raw, BaseModel) else self.top_level_type.model_validate(raw)
        by_layer: dict[str, dict[str, Any]] = {layer: {} for layer in self._registrars}

        for layer in self._composed.dependency_order:
            entries = getattr(cfg, layer, None) or []
            for idx, entry in enumerate(entries):
                self._instantiate(
                    entry,
                    by_layer,
                    path=f"{layer}[{idx}]",
                )

        return ComposedRun(by_layer=by_layer, top_level_cfg=cfg)

    # -- Per-instance assembly ---------------------------------------

    def _instantiate(
        self,
        cfg: BaseModel,
        by_layer: dict[str, dict[str, Any]],
        *,
        path: str,
    ) -> Any:
        layer, key = self._lookup_layer_key(cfg, path)

        # Dedupe: if we've already built this (layer, key), return it.
        cached = by_layer[layer].get(key)
        if cached is not None:
            return cached

        cls = self._registrars[layer].get(key)
        discriminator = self._registrars[layer].discriminator_field
        init_params = _init_param_names(cls)
        wiring_by_name = {spec.param_name: spec for spec in parse_wiring(cls)}
        class_hints = _safe_type_hints(cls)

        init_kwargs: dict[str, Any] = {}
        wired_setters: dict[str, Any] = {}

        for field_name in type(cfg).model_fields:
            if field_name == discriminator:
                continue
            value = getattr(cfg, field_name)

            spec = wiring_by_name.get(field_name)
            if spec is not None and spec.registry_name in self._registrars:
                # Inject only if the field is a real receptor on the class:
                # an __init__ param, a class-level annotation, or a
                # ``WiredField`` descriptor that conscribe's metaclass
                # attached for setattr injection.  Pure declarative
                # constraints (e.g. ``agent.observation``: Pydantic keeps
                # the Literal for validation, runtime has no slot to fill)
                # are skipped here.
                is_receptor = (
                    field_name in init_params
                    or field_name in class_hints
                    or isinstance(
                        getattr(cls, field_name, None), WiredField
                    )
                )
                if not is_receptor:
                    continue

                resolved = self._resolve_wired(
                    cls,
                    field_name,
                    value,
                    spec,
                    by_layer,
                    path=f"{path}.{field_name}",
                )
                if field_name in init_params:
                    init_kwargs[field_name] = resolved
                else:
                    wired_setters[field_name] = resolved
                continue

            # Regular config field.
            if field_name not in init_params:
                continue  # conscribe sometimes adds declarative-only fields
            init_kwargs[field_name] = _unwrap_nested_model(value)

        try:
            instance = cls(**init_kwargs)
        except Exception as exc:
            raise InstantiationError(f"{path}: failed to construct {cls.__name__}: {exc}") from exc

        for name, val in wired_setters.items():
            setattr(instance, name, val)

        by_layer[layer][key] = instance
        return instance

    # -- Wired value resolution --------------------------------------

    def _resolve_wired(
        self,
        owner_cls: type,
        field_name: str,
        value: Any,
        spec: Any,  # WiringSpec
        by_layer: dict[str, dict[str, Any]],
        *,
        path: str,
    ) -> Any:
        annotation = _safe_type_hints(owner_cls).get(field_name)
        origin = get_origin(annotation) if annotation is not None else None

        if origin is dict:
            items = self._as_instance_list(value, by_layer, path)
            return {_registry_key_of(inst, path): inst for inst in items}

        if origin is list:
            return self._as_instance_list(value, by_layer, path)

        # Single.
        if isinstance(value, BaseModel):
            return self._instantiate(value, by_layer, path=path)
        # An unresolved WiredField value can happen if the nested config
        # was omitted in YAML and the field is Optional in Pydantic.
        raise UnresolvedWiringError(
            f"{path}: expected nested config for single-instance wired "
            f"field, got {type(value).__name__}={value!r}"
        )

    def _as_instance_list(
        self,
        value: Any,
        by_layer: dict[str, dict[str, Any]],
        path: str,
    ) -> list[Any]:
        if isinstance(value, BaseModel):
            # Single inlined config promoted to a one-element list.
            return [self._instantiate(value, by_layer, path=path)]
        if isinstance(value, list):
            return [
                self._instantiate(v, by_layer, path=f"{path}[{i}]")
                if isinstance(v, BaseModel)
                else v
                for i, v in enumerate(value)
            ]
        raise UnresolvedWiringError(
            f"{path}: expected list of nested configs or a single nested "
            f"config, got {type(value).__name__}={value!r}"
        )

    # -- Helpers -----------------------------------------------------

    def _lookup_layer_key(
        self,
        cfg: BaseModel,
        path: str,
    ) -> tuple[str, str]:
        try:
            return self._model_to_lk[type(cfg)]
        except KeyError as exc:
            raise InstantiationError(
                f"{path}: config type {type(cfg).__name__} is not registered in any known layer."
            ) from exc


# -- Module-level helpers --------------------------------------------


def _init_param_names(cls: type) -> set[str]:
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return set()
    return {p.name for p in sig.parameters.values() if p.name != "self"}


def _safe_type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except Exception:
        hints: dict[str, Any] = {}
        for klass in cls.__mro__:
            hints.update(getattr(klass, "__annotations__", {}))
        return hints


def _unwrap_nested_model(value: Any) -> Any:
    """Flatten nested Pydantic models back to dicts for __init__ consumption."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_unwrap_nested_model(v) for v in value]
    if isinstance(value, dict):
        return {k: _unwrap_nested_model(v) for k, v in value.items()}
    return value


def _registry_key_of(instance: Any, path: str) -> str:
    """Extract a registry key from an instance for dict-form wiring."""
    reg_key = getattr(instance, "__registry_key__", None)
    if isinstance(reg_key, str):
        return reg_key
    for attr in (
        "capability_id",
        "agent_id",
        "env_type",
        "loop_id",
        "memory_id",
        "hook_id",
    ):
        val = getattr(instance, attr, None)
        if isinstance(val, str):
            return val
    raise UnresolvedWiringError(
        f"{path}: cannot determine registry key for "
        f"{type(instance).__name__} — no __registry_key__ or *_id property."
    )


__all__ = [
    "ComposedRun",
    "ComposeError",
    "ConfigComposer",
    "InstantiationError",
    "UnresolvedWiringError",
]
