"""ConfigComposer — turn a validated YAML/dict into fully-wired runtime instances.

conscribe 2.0 builds the typed schema and exposes constructor and receptor
reflection through ``conscribe.introspect``. The Composer constructs instances
and injects them through the slots that reflection describes. Negotiation-only
entries use ``declare_only`` and reject supplied YAML values because they have
no runtime consumer.

**Instance identity is a config-supplied ``id:``**, falling back to
``f"{registry_key}#{ordinal}"``. ``by_layer`` is keyed by that instance
id, so two instances of the same class coexist with their own configs.
The conscribe registry key is only ever used to look the *class* up.

**An omitted receptor is "not set", never "empty".** A list or dict
receptor the config leaves out adopts the matching instances of its
layer's top-level section; when there are none, the owner's own default
stands (an empty container is what ``[]`` says). A receptor targeting an
``inline_only`` registrar is never adopted at all: it is filled from
its own config block or keeps the owner's default, and its layer's
top-level section is refused, naming where the instances go
(``docs/architecture_design.md`` §4.2.1; ``trace_writer.archive`` is the
case: archiving has one switch).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Any, Literal, get_origin

from conscribe import NO_IMPLEMENTATION_ERROR_TYPE, Coverage
from conscribe.introspect import WiringSlot, init_param_names, receptor_slots, wiring_slots
from conscribe.wiring import WiringSpec
from pydantic import BaseModel, ValidationError

from spectragent.agents._registrar import AgentRegistrar
from spectragent.agents.prompts import agent_family
from spectragent.core.actions._registrar import ActionRegistrar, is_loop_action
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.composition_evidence import (
    CompositionEvidence,
    CompositionIssue,
    ConfiguredComponent,
    WiringDecision,
    WiringSource,
    configured_inputs,
    implementation_name,
)
from spectragent.core.configuration_catalog import ConfigurationCatalog, build_configuration_catalog
from spectragent.core.registries import (
    AGENTS_PACKAGE,
    CAPABILITIES_PACKAGE,
    FRAMEWORK_PACKAGES,
    agent_families,
    capability_ids,
    collect_registrars,
    discover_every_implementation,
    discover_implementations,
)


class ComposeError(Exception):
    """Base class for Composer failures."""

    def __init__(self, message: str, *, issue: CompositionIssue | None = None) -> None:
        super().__init__(message)
        self.issue = issue


class InstantiationError(ComposeError):
    """Failed to construct a class instance from its config entry."""


class UnresolvedWiringError(ComposeError):
    """A wired receptor could not be populated."""


class DuplicateInstanceIdError(ComposeError):
    """Two config entries in one layer claimed the same instance id."""


class UnknownPackageError(ComposeError):
    """A configuration names a capability or an agent family that has no package."""


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
    composition: CompositionEvidence | None = None

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
    identity: dict[int, ConfiguredComponent] = field(default_factory=dict)
    wiring: list[WiringDecision] = field(default_factory=list)

    def instance_id_of(self, instance: Any, path: str) -> str:
        try:
            identity = self.identity[id(instance)].instance_id
            assert identity is not None
            return identity
        except KeyError:
            raise UnresolvedWiringError(
                f"{path}: {type(instance).__name__} was not built by this "
                f"Composer, so it has no instance identity."
            ) from None

    def registry_key_of(self, instance: Any) -> str | None:
        entry = self.identity.get(id(instance))
        return entry.key if entry is not None else None

    def record_wiring(
        self,
        path: str,
        entry: WiringSlot,
        source: WiringSource,
        value: Any = None,
        *,
        via: Literal["init", "setattr", "retained"] | None = None,
    ) -> None:
        slot = entry.slot
        if value is None:
            values = []
        elif slot is not None and slot.shape == "dict":
            values = list(value.values())
        elif slot is not None and slot.shape == "list":
            values = list(value)
            if isinstance(value, set | frozenset):
                values.sort(key=lambda instance: implementation_name(type(instance)))
        else:
            values = [value]
        targets = []
        for instance in values:
            target = self.identity.get(id(instance))
            if target is None:
                key = getattr(type(instance), "__registry_key__", "")
                target = ConfiguredComponent(
                    path=None,
                    layer=entry.spec.registry_name,
                    key=key,
                    implementation=implementation_name(type(instance)),
                    instance_id=None,
                    identity_source="owner_default",
                )
            targets.append(target)
        self.wiring.append(
            WiringDecision(
                path=path,
                registry=entry.spec.registry_name,
                source=source,
                via=via,
                shape=slot.shape if slot else None,
                targets=tuple(targets),
            )
        )


class ConfigComposer:
    """Validates a dict against the composed schema and materialises instances.

    Example::

        raw = yaml.safe_load(Path("cfg.yaml").read_text())
        run = ConfigComposer.for_config(raw).compose(raw)
        result = await AgentRuntime.for_agent(run, "main").execute(task="...")
    """

    @classmethod
    def for_config(cls, raw: object) -> ConfigComposer:
        """The Composer for one configuration, importing only what *raw* names.

        Each capability block's ``capability_id`` names a capability package
        and each agent block's ``agent_id`` an agent family
        (``browser_use.dom``: the ``browser_use`` family). The framework's own
        packages and those packages are imported; the schema holds the
        framework axes and the named capabilities' axes, built as conscribe's
        partial build: a declared key with no implementation here is left out,
        for negotiation to decide (``docs/architecture_design.md`` §4.2.1).

        Raises:
            UnknownPackageError: *raw* names a capability or a family that has
                no package. No capability package or family is imported yet.
            ComposeError: *raw* names an agent and no capability (issue
                ``agent_without_capability``); nothing is imported yet either.
        """
        capabilities, agents = _named_packages(raw)
        if agents and not capabilities:
            agent_id, path = next(iter(agents.items()))
            known = ", ".join(capability_ids()) or "none"
            raise ComposeError(
                f"{path}: the agent {agent_id!r} observes and acts through a capability, and "
                f"the configuration names none: add an environment block holding one "
                f"(capability packages: {known}).",
                issue=CompositionIssue(
                    "agent_without_capability",
                    path,
                    f"The agent {agent_id!r} has no capability to observe and act through.",
                    f"Add an environment block holding a capability ({known}).",
                ),
            )
        discover_implementations(
            *FRAMEWORK_PACKAGES,
            capabilities=capabilities,
            families={agent_family(agent_id) for agent_id in agents},
        )
        return cls(registrars=collect_registrars(capabilities), coverage="partial")

    @classmethod
    def for_every_package(cls) -> ConfigComposer:
        """The Composer over the whole catalog: every package imported, every layer collected."""
        discover_every_implementation()
        return cls(registrars=collect_registrars())

    def __init__(self, *, registrars: Mapping[str, Any], coverage: Coverage = "complete") -> None:
        """Build the schema over the layers of *registrars* that have an implementation.

        Discovers nothing: which modules are imported is the caller's choice
        (:meth:`for_config`, :meth:`for_every_package`). A layer with no
        imported implementation is left out instead of refused, so a
        configuration that names no capability composes without one.
        *coverage* is conscribe's: ``"partial"`` when the imported
        implementations are deliberately part of the catalog.
        """
        self._registrars = {
            layer: registrar for layer, registrar in registrars.items() if registrar.get_all()
        }
        self._left_out = frozenset(registrars) - frozenset(self._registrars)
        if not self._registrars:
            raise ComposeError("ConfigComposer requires a registrar with an implementation.")
        self._catalog = build_configuration_catalog(self._registrars, coverage=coverage)
        # Reverse index: per-key Pydantic model → (layer_name, registry_key).
        self._model_to_lk: dict[type[BaseModel], tuple[str, str]] = {}
        for layer, entries in self._catalog.components.items():
            for key, definition in entries.items():
                self._model_to_lk[definition.config_model] = (layer, key)

    @property
    def registrars(self) -> Mapping[str, Any]:
        """The layers this Composer composes against: each one with an implementation."""
        return MappingProxyType(self._registrars)

    @property
    def catalog(self) -> ConfigurationCatalog:
        """The public configuration definitions used by this Composer."""
        return self._catalog

    @cached_property
    def top_level_type(self) -> type[BaseModel]:
        return self._catalog.top_level_type

    @property
    def dependency_order(self) -> list[str]:
        return list(self._catalog.dependency_order)

    # -- Main entry point --------------------------------------------

    def compose(self, raw: dict | BaseModel) -> ComposedRun:
        """Validate, instantiate (leaves-first), inject, assemble."""
        cfg = raw if isinstance(raw, BaseModel) else self.validate(raw)
        inputs = configured_inputs(cfg)
        state = _State(by_layer={layer: {} for layer in self._registrars})

        for layer in self._catalog.top_level_layers:
            entries = getattr(cfg, layer, None) or []
            for idx, entry in enumerate(entries):
                self._instantiate(entry, state, path=f"{layer}[{idx}]")

        self._resolve_deferred(state)
        return ComposedRun(
            by_layer=state.by_layer,
            top_level_cfg=cfg,
            composition=CompositionEvidence(
                inputs=inputs,
                components=tuple(state.identity.values()),
                wiring=tuple(state.wiring),
            ),
        )

    def _refuse_a_layer_left_out(self, raw: Mapping[object, object]) -> None:
        """Name the section whose layer no imported package implements.

        Validation would only say ``Extra inputs are not permitted``: the layer
        is left out of the schema because none of the packages the
        configuration names implements it (``environment`` with no capability).
        """
        written = sorted(self._left_out.intersection(raw))
        if written:
            layer = written[0]
            known = _known_packages()
            raise ComposeError(
                f"{layer}: the configuration writes the {layer!r} section, and none of the "
                f"packages it names implements that layer. Environments, actions and archivers "
                f"come from the package of a capability the configuration names, agents from "
                f"their family's package: name the one this section needs ({known}).",
                issue=CompositionIssue(
                    "no_implementation",
                    layer,
                    f"None of the packages the configuration names implements {layer!r}.",
                    f"Name the capability or agent family that provides it ({known}).",
                ),
            )

    def validate(self, raw: object) -> BaseModel:
        """*raw* validated against this Composer's schema.

        What this build holds no implementation for is refused in plain words
        rather than pydantic's: a written section whose layer was left out,
        and a value in a field a partial build accepts only "nothing" in,
        which pydantic reports as ``no_implementation`` (``trace_writer.archive``
        in a run naming no capability that provides an archiver).
        """
        if isinstance(raw, Mapping):
            self._refuse_a_layer_left_out(raw)
        try:
            return self.top_level_type.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors():
                if error["type"] != NO_IMPLEMENTATION_ERROR_TYPE:
                    continue
                path = _location(error["loc"])
                context = error.get("ctx") or {}
                layer, name = context.get("layer"), context.get("field")
                known = _known_packages()
                raise ComposeError(
                    f"{path}: no package this configuration names provides any {layer!r}; "
                    f"leave {name!r} out, or name the capability or agent family that does "
                    f"({known}).",
                    issue=CompositionIssue(
                        "no_implementation",
                        path,
                        f"No package the configuration names provides any {layer!r}.",
                        f"Leave {name!r} out, or name the package that provides it ({known}).",
                    ),
                ) from exc
            raise

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
        if registrar is ActionRegistrar and is_loop_action(key):
            # Nothing would receive it: a capability's ``actions`` holds its
            # own keys only, and the Loop builds the actions it provides
            # (``docs/architecture_design.md`` §4.5 "Loop 提供的动作").
            raise UnresolvedWiringError(
                f"{path}: {key!r} is an action the Loop provides and executes itself; it takes "
                f"no configuration, so this entry would be silently dropped. Remove it: every "
                f"Loop that provides {key!r} builds it on its own.",
                issue=CompositionIssue(
                    "loop_action",
                    path,
                    f"{key} is provided by the Loop and takes no configuration.",
                    "Remove this entry; the Loop provides the action.",
                ),
            )
        cls = registrar.get(key)
        discriminators = _discriminator_names(registrar)
        init_params = set(init_param_names(cls))
        wiring = wiring_slots(cls)
        model_fields = type(cfg).model_fields

        instance_id = self._claim_instance_id(cfg, state, layer, key, path)

        init_kwargs: dict[str, Any] = {}
        wired_setters: dict[str, Any] = {}
        deferred: list[tuple[str, WiringSpec]] = []
        retained: list[tuple[str, WiringSlot]] = []

        # -- Pass A: wiring-driven receptors (the primary driver) ----
        for name, entry in wiring.items():
            spec = entry.spec
            if entry.declare_only or entry.slot is None:
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
                        f"give {cls.__name__} a receptor slot for it.",
                        issue=CompositionIssue(
                            "negotiation_only",
                            f"{path}.{name}",
                            "This wiring entry declares a negotiation contract and receives no instance.",
                            "Remove this value or add a runtime receptor to the declaring implementation.",
                        ),
                    )
                state.record_wiring(f"{path}.{name}", entry, "declaration_only")
                continue
            value = getattr(cfg, name, None) if name in model_fields else None
            if spec.registry_name in self._left_out and not _is_populated(value):
                # Its layer has no implementation in this run (no named
                # capability provides an archiver), and the value selects
                # nothing: nothing could fill it, so the owner's default stands.
                retained.append((name, entry))
                continue
            if spec.registry_name not in self._registrars:
                raise UnresolvedWiringError(
                    f"{path}.{name}: {cls.__name__} declares a receptor slot wired to "
                    f"registry {spec.registry_name!r}, which is not one of the "
                    f"Composer's layers ({sorted(self._registrars)}).",
                    issue=CompositionIssue(
                        "uncollected_registry",
                        f"{path}.{name}",
                        f"The receptor targets uncollected registry {spec.registry_name!r}.",
                        "Add the registrar to the configuration catalog's input registrars.",
                    ),
                )
            if value is None:
                if self._registrars[spec.registry_name].inline_only:
                    # Filled from its own block or not at all: omitted or
                    # null leaves the owner's default, never the pool.
                    retained.append((name, entry))
                    continue
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
            state.record_wiring(
                f"{path}.{name}",
                entry,
                "empty" if isinstance(resolved, list | dict) and not resolved else "inline",
                resolved,
                via="init" if name in init_params else "setattr",
            )

        # -- Pass B: plain config fields -----------------------------
        for name in model_fields:
            if name in wiring:
                continue
            value = getattr(cfg, name)
            if name in discriminators and not isinstance(value, BaseModel):
                # Scalar discriminator tag (``agent_id``, ``model_type``).
                # A *nested* discriminator (``provider``) carries real
                # config, so it falls through to the splat branch below.
                continue
            if name in init_params:
                init_kwargs[name] = value
            elif isinstance(value, BaseModel):
                # A nested sub-config whose owner exposes it as a
                # read-only property rather than an ``__init__`` param —
                # the ``provider:`` block on every LLM. Splat its
                # explicitly-set fields into the owner's kwargs instead
                # of dropping the whole block on the floor.
                init_kwargs.update(self._splat_subconfig(cls, name, value, init_params, path=path))
            else:
                raise UnresolvedWiringError(
                    f"{path}.{name}: config field has no matching __init__ "
                    f"parameter on {cls.__name__} and is not a nested "
                    f"sub-config, so its value would be silently dropped.",
                    issue=CompositionIssue(
                        "unconsumed_field",
                        f"{path}.{name}",
                        f"No constructor parameter on {implementation_name(cls)} consumes this field.",
                        "Correct the declaration so the validated field reaches its owning constructor.",
                    ),
                )

        # The resolved instance id always wins over whatever the config
        # said, so ``instance.id`` matches the ``by_layer`` key even when
        # the entry relied on the ordinal fallback.
        if "id" in init_params:
            init_kwargs["id"] = instance_id

        try:
            instance = cls(**init_kwargs)
        except Exception as exc:
            raise InstantiationError(
                f"{path}: failed to construct {cls.__name__}: {exc}",
                issue=CompositionIssue(
                    "constructor_failed",
                    path,
                    f"The constructor of {implementation_name(cls)} refused its configuration.",
                    "Check this component's constructor contract; the original exception is retained in Python.",
                ),
            ) from exc

        for name, val in wired_setters.items():
            setattr(instance, name, val)

        state.identity[id(instance)] = ConfiguredComponent(
            path=path,
            layer=layer,
            key=key,
            implementation=implementation_name(cls),
            instance_id=instance_id,
            identity_source="explicit" if getattr(cfg, "id", None) else "ordinal",
        )
        state.by_layer[layer][instance_id] = instance

        for name, entry in retained:
            state.record_wiring(
                f"{path}.{name}",
                entry,
                "owner_default",
                getattr(instance, name, None),
                via="retained",
            )

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
                f"{layer!r}. Give each entry a distinct ``id:``.",
                issue=CompositionIssue(
                    "duplicate_instance_id",
                    path,
                    f"Two entries claim the same instance identity in layer {layer!r}.",
                    "Give each entry in this layer a distinct id.",
                ),
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
        shape = receptor_slots(owner_cls, names=[field_name])[field_name].shape

        if shape == "dict":
            items = self._as_instance_list(value, state, path)
            return {state.instance_id_of(inst, path): inst for inst in items}

        if shape == "list":
            return self._as_instance_list(value, state, path)

        # Single.
        if isinstance(value, BaseModel):
            return self._instantiate(value, state, path=path)
        # An unresolved WiredField value can happen if the nested config
        # was omitted in YAML and the field is Optional in Pydantic.
        raise UnresolvedWiringError(
            f"{path}: expected nested config for single-instance wired "
            f"field, got {type(value).__name__}={value!r}",
            issue=CompositionIssue(
                "receptor_shape",
                path,
                "A single-instance receptor requires a nested component configuration.",
                "Supply a component block with its registry discriminator.",
            ),
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
                self._instantiate(v, state, path=f"{path}[{i}]") if isinstance(v, BaseModel) else v
                for i, v in enumerate(value)
            ]
        raise UnresolvedWiringError(
            f"{path}: expected list of nested configs or a single nested "
            f"config, got {type(value).__name__}={value!r}",
            issue=CompositionIssue(
                "receptor_shape",
                path,
                "A collection receptor requires nested component configurations.",
                "Supply a list of component blocks or an explicit empty list.",
            ),
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
            shape = receptor_slots(item.owner_cls, names=[item.field_name])[item.field_name].shape
            entry = wiring_slots(item.owner_cls)[item.field_name]

            if (
                shape in ("dict", "list")
                and not selected
                and _holds_own_value(item.owner, item.field_name)
            ):
                # Nothing to adopt, so the receptor stays "not set" and the
                # owner's own default stands (a capability's ``actions``:
                # every wired action). An empty container would say "none",
                # which is what ``[]`` in the config says — never omission.
                state.record_wiring(
                    item.path,
                    entry,
                    "owner_default",
                    getattr(item.owner, item.field_name),
                    via="retained",
                )
                continue
            if shape == "dict":
                resolved: Any = selected
            elif shape == "list":
                resolved = list(selected.values())
            elif len(selected) == 1:
                resolved = next(iter(selected.values()))
            else:
                raise UnresolvedWiringError(
                    f"{item.path}: single-instance receptor on "
                    f"{item.owner_cls.__name__} matched {len(selected)} instances "
                    f"in layer {item.spec.registry_name!r} "
                    f"({sorted(selected)}). Declare it explicitly in the config.",
                    issue=CompositionIssue(
                        "ambiguous_receptor" if selected else "unfilled_receptor",
                        item.path,
                        f"A single-instance receptor matched {len(selected)} instances in layer {item.spec.registry_name!r}.",
                        "Supply one inline component block for this receptor.",
                    ),
                )
            setattr(item.owner, item.field_name, resolved)
            state.record_wiring(item.path, entry, "pool", resolved, via="setattr")

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
                merged[sub_name] = getattr(value, sub_name)
            elif get_origin(sub_field.annotation) is Literal:
                continue  # nested discriminator tag, e.g. ``name: 'azure'``
            else:
                orphans.append(sub_name)
        if orphans:
            raise UnresolvedWiringError(
                f"{path}.{field_name}: sub-config fields {sorted(orphans)} have no "
                f"matching __init__ parameter on {owner_cls.__name__}.",
                issue=CompositionIssue(
                    "unconsumed_subconfig",
                    f"{path}.{field_name}",
                    f"Nested fields {sorted(orphans)} have no constructor parameter on {implementation_name(owner_cls)}.",
                    "Correct the nested configuration declaration or its constructor parameters.",
                ),
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
                f"{path}: config type {type(cfg).__name__} is not registered in any known layer.",
                issue=CompositionIssue(
                    "unknown_config_model",
                    path,
                    "The component block was not validated by this Composer's catalog.",
                    "Validate the input against this Composer's top_level_type.",
                ),
            ) from exc


# -- Module-level helpers --------------------------------------------


#: The receptor every environment inherits from ``EnvironmentBase``. An
#: environment's own class lives in its capability's package, which is not
#: imported yet when names are read, so its capability blocks are found here.
_ENVIRONMENT_CAPABILITIES = "capabilities"


def _named_packages(raw: object) -> tuple[dict[str, str], dict[str, str]]:
    """The capability ids and agent ids *raw* names, each with the first place it is written.

    Read before anything is imported, and only where the schema puts those
    blocks: the entries of the top-level ``capability`` and ``agent``
    sections and each environment's ``capabilities``. Only those blocks'
    discriminator fields are read, never a free-form field: an ``agent_id``
    in a hook's ``parameters`` or a provider's ``extra_body`` names nothing.

    Raises:
        UnknownPackageError: a capability, or the family of an agent
            (``browser_use.dom``: ``browser_use``), that has no package.
    """
    sections = raw if isinstance(raw, Mapping) else {}
    capabilities = _ids(
        [
            *_blocks(sections.get("capability"), "capability"),
            *(
                held
                for environment, path in _blocks(sections.get("environment"), "environment")
                for held in _blocks(
                    environment.get(_ENVIRONMENT_CAPABILITIES),
                    f"{path}.{_ENVIRONMENT_CAPABILITIES}",
                )
            ),
        ],
        CapabilityRegistrar.discriminator_field,
    )
    agents = _ids(_blocks(sections.get("agent"), "agent"), AgentRegistrar.discriminator_field)
    families: dict[str, str] = {}
    for agent_id, path in agents.items():
        families.setdefault(agent_family(agent_id), path)
    for named, kind, parent, known, code in (
        (
            capabilities,
            "capability",
            CAPABILITIES_PACKAGE,
            capability_ids(),
            "unknown_capability_package",
        ),
        (families, "agent family", AGENTS_PACKAGE, agent_families(), "unknown_agent_family"),
    ):
        for name, path in named.items():
            if name not in known:
                package = f"{parent}.{name}"
                available = ", ".join(known) or "none"
                raise UnknownPackageError(
                    f"{path}: names the {kind} {name!r}, which has no package {package} "
                    f"(known: {available}).",
                    issue=CompositionIssue(
                        code,
                        path,
                        f"The {kind} {name!r} has no package {package}.",
                        f"Name one that exists ({available}), or add the package.",
                    ),
                )
    return capabilities, agents


def _known_packages() -> str:
    """The capability packages and agent families there are, for a refusal to list."""
    return (
        f"capability packages: {', '.join(capability_ids()) or 'none'}; "
        f"agent families: {', '.join(agent_families()) or 'none'}"
    )


def _location(loc: tuple[int | str, ...]) -> str:
    """A pydantic error location written as a config path: ``hook[0].archive``."""
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else f".{part}" if path else str(part)
    return path


def _blocks(value: object, path: str) -> list[tuple[Mapping[object, object], str]]:
    """The component blocks written at *path*: one inline block, or each block of a list."""
    if isinstance(value, Mapping):
        return [(value, path)]
    if isinstance(value, list | tuple):
        return [
            (item, f"{path}[{index}]")
            for index, item in enumerate(value)
            if isinstance(item, Mapping)
        ]
    return []


def _ids(blocks: list[tuple[Mapping[object, object], str]], field: str) -> dict[str, str]:
    """Each string value of *field* in *blocks*, with the first place it is written."""
    ids: dict[str, str] = {}
    for block, path in blocks:
        value = block.get(field)
        if isinstance(value, str):
            ids.setdefault(value, f"{path}.{field}")
    return ids


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


def _allowed_keys(spec: WiringSpec) -> frozenset[str] | None:
    """Registry keys a wiring spec accepts (``None`` = every key)."""
    if spec.allowed_keys is None and spec.optional_keys is None:
        return None
    return frozenset(spec.allowed_keys or ()) | frozenset(spec.optional_keys or ())


def _holds_own_value(owner: Any, name: str) -> bool:
    """Whether *owner* already answers *name* itself — its constructor's default.

    An unfilled receptor is conscribe's ``WiredField``, which raises
    ``AttributeError`` on read.
    """
    try:
        getattr(owner, name)
    except AttributeError:
        return False
    return True


__all__ = [
    "ComposeError",
    "ComposedRun",
    "ConfigComposer",
    "DuplicateInstanceIdError",
    "InstantiationError",
    "UnknownPackageError",
    "UnresolvedWiringError",
]
