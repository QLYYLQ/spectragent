"""ConfigComposer — turn a validated YAML/dict into fully-wired runtime instances.

conscribe gives us four pieces:

1. ``build_composed_config(inline_wiring=True)``: produces a top-level
   Pydantic model whose wired fields are *inlined* as nested discriminated
   unions of the target layer's per-key models.
2. ``parse_wiring(cls)``: normalized wiring specs (Mode 1/2/3).
3. ``WiredField`` descriptor: raises a clear ``AttributeError`` on read if
   the framework forgot to inject.
4. ``resolve_wiring(cls)``: reports whether each wiring entry is an
   ``__init__`` param or a setattr-injected receptor.

ConfigComposer stitches them together.

**Injection is driven by ``parse_wiring(cls)``, not by the config model.**
The config model only *supplies values*. A wiring entry becomes a runtime
receptor if — and only if — the owning class declares a slot for it: an
``__init__`` parameter or a class-level annotation. That single rule
separates real receptors (``agent.llm``, ``env.capabilities``,
``capability.backend``) from pure negotiation declarations
(``agent.observation``, ``agent.action``), which live in ``__wiring__``
so conscribe can constrain them in the schema but have no runtime slot
to fill. Receptor-ness is therefore a property of the *class*, never of
the caller's registrar dict.

**Instance identity is a config-supplied ``id:``**, falling back to
``f"{registry_key}#{ordinal}"``. ``by_layer`` is keyed by that instance
id, so two instances of the same class coexist with their own configs.
The conscribe registry key is only ever used to look the *class* up.
"""

from __future__ import annotations

import inspect
import typing
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar, Literal, get_args, get_origin

from conscribe import build_composed_config, discover
from conscribe.wiring import WiringSpec, parse_wiring
from pydantic import BaseModel


class ComposeError(Exception):
    """Base class for Composer failures."""


class InstantiationError(ComposeError):
    """Failed to construct a class instance from its config entry."""


class UnresolvedWiringError(ComposeError):
    """A wired receptor could not be populated."""


class DuplicateInstanceIdError(ComposeError):
    """Two config entries in one layer claimed the same instance id."""


@dataclass(frozen=True)
class ComposedRun:
    """Fully assembled, setattr-injected object graph ready for AgentRuntime.

    The only source of truth is ``by_layer``:
    ``{layer_name: {instance_id: instance}}``. There are deliberately no
    singular ``agent`` / ``loop`` / ``llm`` / ``memory`` accessors — a run
    may hold several agents, and each agent's loop/llm/memory hang off
    that agent's own wired receptors. Use
    :meth:`~spectragent.core.runtime.AgentRuntime.for_agent` to build a
    runtime around one specific agent instance.

    ``top_level_cfg`` is the validated Pydantic model the Composer built
    from raw YAML. It defaults to ``None`` for tests that assemble a
    ``ComposedRun`` by hand without going through Pydantic validation.
    """

    by_layer: dict[str, dict[str, Any]]
    top_level_cfg: BaseModel | None = None

    # -- Raw accessors --
    def instances(self, layer: str) -> list[Any]:
        return list(self.by_layer.get(layer, {}).values())

    def ids(self, layer: str) -> list[str]:
        return list(self.by_layer.get(layer, {}))

    def instance(self, layer: str, instance_id: str) -> Any:
        """Look one instance up by layer + instance id."""
        try:
            return self.by_layer[layer][instance_id]
        except KeyError:
            raise ComposeError(
                f"Layer {layer!r} has no instance {instance_id!r}. "
                f"Available: {sorted(self.by_layer.get(layer, {}))}"
            ) from None

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

    @property
    def hooks(self) -> list[Any]:
        return self.instances("hook")


# -- Compose-time bookkeeping ----------------------------------------


@dataclass
class _Deferred:
    """A wired receptor with no config field — resolved from ``by_layer``.

    This is the "declarative multi-instance receptor" case: the class
    declares e.g. ``capabilities: dict[str, BaseCapability]`` in
    ``__wiring__`` but the per-key config model exposes no matching
    field, so the value can only come from the pool of instances the
    rest of the config built. Resolution is deferred to a second pass so
    it does not depend on layer ordering.
    """

    owner: Any
    owner_cls: type
    field_name: str
    spec: WiringSpec
    path: str


@dataclass
class _State:
    """Mutable bookkeeping threaded through one ``compose()`` call."""

    by_layer: dict[str, dict[str, Any]]
    ordinals: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    deferred: list[_Deferred] = field(default_factory=list)
    #: ``id(instance) -> (layer, registry_key, instance_id)``
    identity: dict[int, tuple[str, str, str]] = field(default_factory=dict)

    def instance_id_of(self, instance: Any, path: str) -> str:
        try:
            return self.identity[id(instance)][2]
        except KeyError:
            raise UnresolvedWiringError(
                f"{path}: {type(instance).__name__} was not built by this "
                f"Composer, so it has no instance identity."
            ) from None

    def registry_key_of(self, instance: Any) -> str | None:
        entry = self.identity.get(id(instance))
        return entry[1] if entry is not None else None


class ConfigComposer:
    """Validates a dict against the composed schema and materialises instances.

    Example::

        composer = ConfigComposer(registrars=_collect_registrars())
        run = composer.compose(yaml.safe_load(Path("cfg.yaml").read_text()))
        result = await AgentRuntime.for_agent(run, "main").execute(task="...")
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
        state = _State(by_layer={layer: {} for layer in self._registrars})

        for layer in self._composed.dependency_order:
            entries = getattr(cfg, layer, None) or []
            for idx, entry in enumerate(entries):
                self._instantiate(entry, state, path=f"{layer}[{idx}]")

        self._resolve_deferred(state)
        return ComposedRun(by_layer=state.by_layer, top_level_cfg=cfg)

    # -- Per-instance assembly ---------------------------------------

    def _instantiate(
        self,
        cfg: BaseModel,
        state: _State,
        *,
        path: str,
    ) -> Any:
        layer, key = self._lookup_layer_key(cfg, path)

        registrar = self._registrars[layer]
        cls = registrar.get(key)
        discriminators = _discriminator_names(registrar)
        init_params = _init_param_names(cls)
        slots = _receptor_slots(cls)
        wiring_by_name = {spec.param_name: spec for spec in parse_wiring(cls)}
        model_fields = type(cfg).model_fields

        instance_id = self._claim_instance_id(cfg, state, layer, key, path)

        init_kwargs: dict[str, Any] = {}
        wired_setters: dict[str, Any] = {}
        deferred: list[tuple[str, WiringSpec]] = []

        # -- Pass A: wiring-driven receptors (the primary driver) ----
        for name, spec in wiring_by_name.items():
            if name not in slots:
                # Pure negotiation declaration: conscribe keeps it in the
                # schema so illegal combinations fail Pydantic validation,
                # but the class has no attribute to receive it. Injecting
                # here is what used to setattr a stray ClickAction onto
                # every agent and capability.
                #
                # Declaring it is fine; *populating* it is not. Pass B
                # skips every wiring name, so a filled-in block for a
                # slotless entry would fall through both passes and
                # vanish — the same silent drop the UnresolvedWiringError
                # below exists to prevent, and the reason a user tuning
                # ``action:`` from YAML got a run that ignored it.
                if _is_populated(getattr(cfg, name, None) if name in model_fields else None):
                    raise UnresolvedWiringError(
                        f"{path}.{name}: {cls.__name__} declares {name!r} in "
                        f"__wiring__ as a negotiation-only entry (registry "
                        f"{spec.registry_name!r}) — it has no __init__ "
                        f"parameter and no class-level annotation to receive a "
                        f"value, so this config block would be silently "
                        f"dropped. Either remove it and declare the instance "
                        f"in the top-level {spec.registry_name!r} section, or "
                        f"give {cls.__name__} a receptor slot for it."
                    )
                continue
            if spec.registry_name not in self._registrars:
                raise UnresolvedWiringError(
                    f"{path}.{name}: {cls.__name__} declares a receptor slot wired to "
                    f"registry {spec.registry_name!r}, which is not one of the "
                    f"Composer's layers ({sorted(self._registrars)})."
                )
            value = getattr(cfg, name, None) if name in model_fields else None
            if value is None:
                # Declarative multi-instance receptor: either the per-key
                # model exposes no field at all, or the field is optional
                # and the config left it out. Resolve from the pool of
                # built instances once everything exists.
                deferred.append((name, spec))
                continue
            resolved = self._resolve_wired(
                cls,
                name,
                value,
                state,
                path=f"{path}.{name}",
            )
            if name in init_params:
                init_kwargs[name] = resolved
            else:
                wired_setters[name] = resolved

        # -- Pass B: plain config fields -----------------------------
        for name in model_fields:
            if name in wiring_by_name:
                continue
            value = getattr(cfg, name)
            if name in discriminators and not isinstance(value, BaseModel):
                # Scalar discriminator tag (``agent_id``, ``model_type``).
                # A *nested* discriminator (``provider``) carries real
                # config, so it falls through to the splat branch below.
                continue
            if name in init_params:
                init_kwargs[name] = _unwrap_nested_model(value)
            elif isinstance(value, BaseModel):
                # A nested sub-config whose owner exposes it as a
                # read-only property rather than an ``__init__`` param —
                # the ``provider:`` block on every LLM. Splat its
                # explicitly-set fields into the owner's kwargs instead
                # of dropping the whole block on the floor.
                init_kwargs.update(
                    self._splat_subconfig(cls, name, value, init_params, path=path)
                )
            else:
                raise UnresolvedWiringError(
                    f"{path}.{name}: config field has no matching __init__ "
                    f"parameter on {cls.__name__} and is not a nested "
                    f"sub-config, so its value would be silently dropped."
                )

        # The resolved instance id always wins over whatever the config
        # said, so ``instance.id`` matches the ``by_layer`` key even when
        # the entry relied on the ordinal fallback.
        if "id" in init_params:
            init_kwargs["id"] = instance_id

        try:
            instance = cls(**init_kwargs)
        except Exception as exc:
            raise InstantiationError(f"{path}: failed to construct {cls.__name__}: {exc}") from exc

        for name, val in wired_setters.items():
            setattr(instance, name, val)

        state.identity[id(instance)] = (layer, key, instance_id)
        state.by_layer[layer][instance_id] = instance

        for name, spec in deferred:
            state.deferred.append(
                _Deferred(
                    owner=instance,
                    owner_cls=cls,
                    field_name=name,
                    spec=spec,
                    path=f"{path}.{name}",
                )
            )
        return instance

    # -- Instance identity -------------------------------------------

    def _claim_instance_id(
        self,
        cfg: BaseModel,
        state: _State,
        layer: str,
        key: str,
        path: str,
    ) -> str:
        declared = getattr(cfg, "id", None) if "id" in type(cfg).model_fields else None
        if isinstance(declared, str) and declared:
            instance_id = declared
        else:
            ordinal = state.ordinals[layer, key]
            state.ordinals[layer, key] = ordinal + 1
            instance_id = f"{key}#{ordinal}"

        if instance_id in state.by_layer[layer]:
            raise DuplicateInstanceIdError(
                f"{path}: instance id {instance_id!r} is already used in layer "
                f"{layer!r}. Give each entry a distinct ``id:``."
            )
        return instance_id

    # -- Wired value resolution --------------------------------------

    def _resolve_wired(
        self,
        owner_cls: type,
        field_name: str,
        value: Any,
        state: _State,
        *,
        path: str,
    ) -> Any:
        origin = get_origin(_slot_annotation(owner_cls, field_name))

        if origin is dict:
            items = self._as_instance_list(value, state, path)
            return {state.instance_id_of(inst, path): inst for inst in items}

        if origin is list:
            return self._as_instance_list(value, state, path)

        # Single.
        if isinstance(value, BaseModel):
            return self._instantiate(value, state, path=path)
        # An unresolved WiredField value can happen if the nested config
        # was omitted in YAML and the field is Optional in Pydantic.
        raise UnresolvedWiringError(
            f"{path}: expected nested config for single-instance wired "
            f"field, got {type(value).__name__}={value!r}"
        )

    def _as_instance_list(
        self,
        value: Any,
        state: _State,
        path: str,
    ) -> list[Any]:
        if isinstance(value, BaseModel):
            # Single inlined config promoted to a one-element list.
            return [self._instantiate(value, state, path=path)]
        if isinstance(value, list):
            return [
                self._instantiate(v, state, path=f"{path}[{i}]")
                if isinstance(v, BaseModel)
                else v
                for i, v in enumerate(value)
            ]
        raise UnresolvedWiringError(
            f"{path}: expected list of nested configs or a single nested "
            f"config, got {type(value).__name__}={value!r}"
        )

    def _resolve_deferred(self, state: _State) -> None:
        """Fill receptors that have no config field from the instance pool."""
        for item in state.deferred:
            pool = state.by_layer.get(item.spec.registry_name, {})
            allowed = _allowed_keys(item.spec)
            selected = {
                inst_id: inst
                for inst_id, inst in pool.items()
                if allowed is None or state.registry_key_of(inst) in allowed
            }
            origin = get_origin(_slot_annotation(item.owner_cls, item.field_name))

            if origin is dict:
                resolved: Any = selected
            elif origin is list:
                resolved = list(selected.values())
            elif len(selected) == 1:
                resolved = next(iter(selected.values()))
            else:
                raise UnresolvedWiringError(
                    f"{item.path}: single-instance receptor on "
                    f"{item.owner_cls.__name__} matched {len(selected)} instances "
                    f"in layer {item.spec.registry_name!r} "
                    f"({sorted(selected)}). Declare it explicitly in the config."
                )
            setattr(item.owner, item.field_name, resolved)

    # -- Nested non-receptor sub-configs ------------------------------

    def _splat_subconfig(
        self,
        owner_cls: type,
        field_name: str,
        value: BaseModel,
        init_params: set[str],
        *,
        path: str,
    ) -> dict[str, Any]:
        """Merge an explicitly-set nested sub-config into the owner's kwargs.

        conscribe emits the second level of a nested registry key (e.g.
        ``openai.azure``) as a ``provider:`` sub-model, but the owning
        class exposes ``provider`` as a read-only property — so the block
        has no ``__init__`` parameter of its own. Its *fields*, however,
        map one-to-one onto the owner's ``__init__``.

        Only fields the config actually set are splatted, so an omitted
        block never overwrites the owner's own defaults with ``None``.
        """
        merged: dict[str, Any] = {}
        orphans: list[str] = []
        sub_fields = type(value).model_fields
        for sub_name, sub_field in sub_fields.items():
            if sub_name not in value.model_fields_set:
                continue
            if sub_name in init_params:
                merged[sub_name] = _unwrap_nested_model(getattr(value, sub_name))
            elif get_origin(sub_field.annotation) is Literal:
                continue  # nested discriminator tag, e.g. ``name: 'azure'``
            else:
                orphans.append(sub_name)
        if orphans:
            raise UnresolvedWiringError(
                f"{path}.{field_name}: sub-config fields {sorted(orphans)} have no "
                f"matching __init__ parameter on {owner_cls.__name__}."
            )
        return merged

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


def _discriminator_names(registrar: Any) -> frozenset[str]:
    """Every field a registrar uses as a discriminator tag.

    Nested registrars (``llm``: ``model_type.provider``) expose
    ``discriminator_fields``; flat ones expose ``discriminator_field``.
    """
    nested = getattr(registrar, "discriminator_fields", None)
    if nested:
        return frozenset(nested)
    flat = getattr(registrar, "discriminator_field", None)
    return frozenset([flat]) if flat else frozenset()


def _is_populated(value: Any) -> bool:
    """Did the config actually supply something for this field?

    An empty list or dict carries no configuration, so dropping it is
    harmless — only a value that would *change the run* is worth
    refusing.
    """
    if value is None:
        return False
    return not (isinstance(value, list | dict) and not value)


def _init_param_names(cls: type) -> set[str]:
    """Names ``cls.__init__`` accepts *by name*.

    ``*args`` / ``**kwargs`` are excluded. They name no option, so they
    claim none — the same rule ``_setup_keys`` applies to ``setup()``.
    Counting them let a ``def __init__(self, **kwargs)`` class advertise
    a parameter literally called ``kwargs``, which is never what a config
    field is called, while the field the config *did* carry fell through
    to the "no matching __init__ parameter" error.
    """
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return set()
    return {
        p.name
        for p in sig.parameters.values()
        if p.name != "self" and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }


def _safe_type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls)
    except Exception:
        hints: dict[str, Any] = {}
        for klass in cls.__mro__:
            hints.update(getattr(klass, "__annotations__", {}))
        return hints


def _receptor_slots(cls: type) -> set[str]:
    """Names *cls* can actually receive a value on.

    An ``__init__`` parameter or a non-``ClassVar`` class-level
    annotation anywhere in the MRO. This is the single rule that decides
    whether a ``__wiring__`` entry is a runtime receptor or a
    schema-only negotiation declaration.
    """
    names = _init_param_names(cls)
    for klass in cls.__mro__:
        for name, annotation in getattr(klass, "__annotations__", {}).items():
            if name.startswith("__") or _is_classvar(annotation):
                continue
            names.add(name)
    return names


def _is_classvar(annotation: Any) -> bool:
    if isinstance(annotation, str):
        return annotation.startswith(("ClassVar", "typing.ClassVar"))
    return get_origin(annotation) is ClassVar or annotation is ClassVar


def _slot_annotation(cls: type, name: str) -> Any:
    """Resolved annotation for a receptor slot, or ``None`` if untyped."""
    annotation = _safe_type_hints(cls).get(name)
    if annotation is None:
        return None
    if get_origin(annotation) is typing.Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _allowed_keys(spec: WiringSpec) -> frozenset[str] | None:
    """Registry keys a wiring spec accepts (``None`` = every key)."""
    if spec.allowed_keys is None and spec.optional_keys is None:
        return None
    return frozenset(spec.allowed_keys or ()) | frozenset(spec.optional_keys or ())


def _unwrap_nested_model(value: Any) -> Any:
    """Flatten nested Pydantic models back to dicts for __init__ consumption."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_unwrap_nested_model(v) for v in value]
    if isinstance(value, dict):
        return {k: _unwrap_nested_model(v) for k, v in value.items()}
    return value


__all__ = [
    "ComposeError",
    "ComposedRun",
    "ConfigComposer",
    "DuplicateInstanceIdError",
    "InstantiationError",
    "UnresolvedWiringError",
]
