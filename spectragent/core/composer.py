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
(``docs/architecture_design.md`` §4.2.1: ``trace_writer.archive``, the
capability's strategies, the agent's ``agent_loop`` and ``memory``). An
inline-only layer that no block the configuration can write holds is left
out.

**A run's capability blocks are merged before they are validated.** The
benchmark's part of a capability's configuration (its block's ``capability``
list, and what it computes in code) is merged field by field into the run
configuration's block of that capability (:mod:`spectragent.core.capability_merge`,
``docs/architecture_design.md`` §4.10.1 "capability 配置合并"); a conflict is
refused naming both sides, and a required setting neither writes is refused
naming it. The merged blocks are what the run validates, constructs and
records.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, get_origin

from conscribe import NO_IMPLEMENTATION_ERROR_TYPE, Coverage, LayerRegistrar
from conscribe.introspect import WiringSlot, init_param_names, receptor_slots, wiring_slots
from conscribe.wiring import WiringSpec
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from spectragent.agents._registrar import AgentRegistrar
from spectragent.agents.prompts import agent_family
from spectragent.benchmarks._registrar import BenchmarkRegistrar
from spectragent.core.actions._registrar import ActionRegistrar, is_loop_action
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.capability_merge import (
    CAPABILITY_SECTION,
    ENVIRONMENT_SECTION,
    CapabilityMerge,
    CapabilityMergeError,
    Contribution,
)
from spectragent.core.composition_evidence import (
    CompositionEvidence,
    CompositionIssue,
    ConfiguredComponent,
    WiringDecision,
    WiringSource,
    configured_inputs,
    implementation_name,
    safe_error_message,
)
from spectragent.core.configuration_catalog import ConfigurationCatalog, build_configuration_catalog
from spectragent.core.registries import (
    AGENTS_PACKAGE,
    BENCHMARKS_PACKAGE,
    CAPABILITIES_PACKAGE,
    FRAMEWORK_PACKAGES,
    agent_families,
    benchmark_ids,
    capability_ids,
    collect_registrars,
    discover_every_implementation,
    discover_implementations,
    unnamed_packages,
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

    def run_configuration(self) -> BaseModel | None:
        """The validated configuration a run records as its own: the root without verifiers.

        A benchmark's verifiers judge runs and never change one, so they are
        no part of the configuration a run's trace records, nor of its
        ``config_hash`` (``docs/architecture_design.md`` §5.3 "实验身份"):
        the same sweep judged by another judge is the same experiment, and
        each verdict records its verifier itself. Every benchmark block that
        takes verifiers is recorded with none listed, whatever it lists.
        """
        cfg = self.top_level_cfg
        blocks = getattr(cfg, _BENCHMARK_SECTION, None) if cfg is not None else None
        if cfg is None or not blocks:
            return cfg
        unjudged = [
            block.model_copy(update={_VERIFIERS: None})
            if _VERIFIERS in type(block).model_fields
            else block
            for block in blocks
        ]
        return cfg.model_copy(update={_BENCHMARK_SECTION: unjudged})


#: The benchmark section of a run root, and its blocks' verifier receptor
#: (``spectragent/benchmarks/_registrar.py``).
_BENCHMARK_SECTION = "benchmark"
_VERIFIERS = "verifiers"


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

        Each capability block's ``capability_id`` names a capability package,
        each agent block's ``agent_id`` an agent family (``browser_use.dom``:
        the ``browser_use`` family) and each benchmark block's
        ``benchmark_id`` a benchmark package. The framework's own packages and
        those packages are imported; the schema holds the framework axes and
        the named capabilities' axes, built as conscribe's partial build: a
        declared key with no implementation here is left out, for negotiation
        to decide (``docs/architecture_design.md`` §4.2.1).

        Raises:
            UnknownPackageError, ComposeError: what :func:`named_packages`
                refuses, before any capability package, family or benchmark
                is imported.
        """
        named = named_packages(raw)
        families = {agent_family(agent_id) for agent_id in named.agents}
        discover_implementations(
            *FRAMEWORK_PACKAGES,
            capabilities=named.capabilities,
            families=families,
            benchmarks=named.benchmarks,
        )
        return cls(
            registrars=collect_registrars(named.capabilities),
            coverage="partial",
            unnamed=unnamed_packages(named.capabilities, families, named.benchmarks),
        )

    @classmethod
    def for_every_package(cls) -> ConfigComposer:
        """The Composer over the whole catalog: every package imported, every layer collected."""
        discover_every_implementation()
        return cls(registrars=collect_registrars())

    def __init__(
        self,
        *,
        registrars: Mapping[str, Any],
        coverage: Coverage = "complete",
        unnamed: Collection[str] = (),
    ) -> None:
        """Build the schema over the layers of *registrars* that have an implementation.

        Discovers nothing: which modules are imported is the caller's choice
        (:meth:`for_config`, :meth:`for_every_package`). A layer with no
        imported implementation is left out instead of refused, so a
        configuration that names no capability composes without one.
        *coverage* is conscribe's: ``"partial"`` when the imported
        implementations are deliberately part of the catalog. An
        implementation defined in one of the *unnamed* packages (a run's
        other capability packages and agent families, which an earlier import
        may have left registered) does not keep a layer in: which layers a run
        holds, and so its resolved config and hash, follows what it names.
        An inline-only layer that no block the configuration can write holds
        is left out too (``agent_loop`` in a run that names no agent family).
        """
        implemented = {
            layer: registrar
            for layer, registrar in registrars.items()
            if any(not _inside(cls.__module__, unnamed) for cls in registrar.get_all().values())
        }
        self._registrars = _held(implemented, unnamed)
        #: Inline-only layers with implementations here that no block can hold.
        self._unheld = frozenset(implemented) - frozenset(self._registrars)
        self._left_out = {
            layer: registrar
            for layer, registrar in registrars.items()
            if layer not in self._registrars
        }
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

    def validate(self, raw: object) -> BaseModel:
        """*raw* validated against this Composer's schema.

        What this build holds no implementation for is refused in plain words
        rather than pydantic's: a written section whose layer was left out
        (``environment`` with no capability: validation would only say
        ``Extra inputs are not permitted``), and a value in a field a partial
        build accepts only "nothing" in, which pydantic reports as
        ``no_implementation`` (``trace_writer.archive`` in a run naming no
        capability that provides an archiver). Such a refusal also lists every
        other problem of the configuration, so one attempt shows them all; a
        configuration with only ordinary problems gets pydantic's own error.
        """
        problems: list[_Problem] = []
        sections = raw
        if isinstance(raw, Mapping):
            problems = [
                self._left_out_problem(str(layer))
                for layer in sorted(raw, key=str)
                if layer in self._left_out
            ]
            sections = self._merged(
                {str(key): value for key, value in raw.items() if key not in self._left_out}
            )
        try:
            cfg = self.top_level_type.model_validate(sections)
        except ValidationError as exc:
            problems += [_Problem.of(error, sections) for error in exc.errors()]
            if not any(problem.code for problem in problems):
                raise
            raise _refusal(problems) from exc
        if problems:
            raise _refusal(problems)
        return cfg

    def _merged(self, sections: dict[str, object]) -> dict[str, object]:
        """*sections* with the benchmark's parts merged into the run's capability blocks.

        Only a run that starts an environment (it writes ``environment`` or
        ``capability``) takes a benchmark's parts; any other root (an export's,
        an evaluation's) has nothing to merge them into and leaves them. The
        benchmark blocks are validated first: what a benchmark computes in code
        reads its validated block.

        Raises:
            ValidationError: A benchmark block is invalid.
            ComposeError: A part breaks its capability's schema, names a
                capability the run has no block of, or writes a field the run
                configuration or another part writes differently.
        """
        benchmarks = sections.get(BENCHMARK_LAYER)
        if not (
            isinstance(benchmarks, list)
            and benchmarks
            and BENCHMARK_LAYER in self._registrars
            and (ENVIRONMENT_SECTION in sections or CAPABILITY_SECTION in sections)
        ):
            return sections
        validated = getattr(
            self.top_level_type.model_validate({BENCHMARK_LAYER: benchmarks}), BENCHMARK_LAYER
        )
        contributions: list[Contribution] = []
        for index, (block, config) in enumerate(zip(benchmarks, validated, strict=True)):
            benchmark_id = block.get(BenchmarkRegistrar.discriminator_field)
            who = f"the benchmark {benchmark_id}"
            contributions += [
                Contribution(part, who, f"{BENCHMARK_LAYER}[{index}].capability[{position}]")
                for position, part in enumerate(block.get("capability") or ())
            ]
            computed = BenchmarkRegistrar.get(benchmark_id).computed_capability(config)
            contributions += [
                Contribution(part, f"{who}, computed in code", f"{BENCHMARK_LAYER}[{index}]")
                for part in computed
            ]
        if not contributions:
            return sections
        merge = CapabilityMerge(self._catalog, self._registrars)
        try:
            for contribution in contributions:
                merge.check(contribution)
            return merge.merge(sections, contributions)
        except CapabilityMergeError as exc:
            remedy = _MERGE_REMEDIES[exc.code]
            raise _refusal(
                [
                    _Problem(problem.path, problem.message, exc.code, remedy)
                    for problem in exc.problems
                ]
            ) from None

    def _left_out_problem(self, layer: str) -> _Problem:
        """Why a written section of *layer*, which this run left out, has nowhere to go."""
        if self._left_out[layer].inline_only:
            # Naming a capability that provides it would only meet the
            # inline_only_layer refusal: say where its instances are written.
            where = self._receptors_of(layer)
            if layer in self._unheld or not where:
                return _Problem(
                    layer,
                    f"the {layer!r} layer takes no top-level section, and nothing this "
                    f"configuration names takes it",
                    _NO_IMPLEMENTATION,
                    "Leave it out, or name the agent family, capability or benchmark whose block "
                    "holds it",
                )
            return _Problem(
                layer,
                f"the {layer!r} layer takes no top-level section: its instances are written "
                f"under {where}, and come from the package of a capability the configuration "
                f"names",
                _NO_IMPLEMENTATION,
                f"Leave it out, or write its instances under {where} and name the capability "
                f"that provides them",
            )
        return _Problem(
            layer,
            f"the configuration writes the {layer!r} section, and none of the packages it "
            f"names implements that layer. Environments, actions and archivers come from the "
            f"package of a capability the configuration names, agents from their family's "
            f"package: name the one this section needs",
            _NO_IMPLEMENTATION,
        )

    def _receptors_of(self, layer: str) -> str:
        """Where this schema's components hold instances of *layer*: ``hook[trace_writer].archive``."""
        return ", ".join(
            f"{owner}[{key}].{name}"
            for owner, entries in self._catalog.components.items()
            for key, definition in entries.items()
            for name, entry in wiring_slots(definition.implementation).items()
            if entry.slot is not None
            and not entry.declare_only
            and entry.spec.registry_name == layer
        )

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
            if spec.registry_name in self._left_out:
                # Its layer has no implementation in this run (no named
                # capability provides an archiver). A value that selects
                # nothing leaves the owner's default. After an earlier import
                # left implementations registered, the field takes their bare
                # keys; a value naming one is refused as a fresh process
                # refuses it at validation.
                if _is_populated(value):
                    unimplemented = _unimplemented(spec.registry_name, name)
                    raise _refusal([_Problem(f"{path}.{name}", unimplemented, _NO_IMPLEMENTATION)])
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


#: The layer of the benchmark blocks whose capability parts a run merges.
BENCHMARK_LAYER = "benchmark"

#: The receptor every environment inherits from ``EnvironmentBase``. An
#: environment's own class lives in its capability's package, which is not
#: imported yet when names are read, so its capability blocks are found here.
_ENVIRONMENT_CAPABILITIES = "capabilities"


class NamedPackages(NamedTuple):
    """The ids a configuration names, each with the first place it is written."""

    capabilities: dict[str, str]
    agents: dict[str, str]
    benchmarks: dict[str, str]


def named_packages(raw: object) -> NamedPackages:
    """The capability, agent and benchmark ids *raw* names, each with the first place it is written.

    Read before anything is imported, and only where the schema puts those
    blocks: the entries of the top-level ``capability``, ``agent`` and
    ``benchmark`` sections and each environment's ``capabilities``. Only
    those blocks' discriminator fields are read, never a free-form field: an
    ``agent_id`` in a hook's ``parameters`` or a provider's ``extra_body``
    names nothing. Its refusals are the configuration's errors, not the
    schema's: ``config check`` reports them at its validation stage.

    Raises:
        UnknownPackageError: a capability, the family of an agent
            (``browser_use.dom``: ``browser_use``) or a benchmark that has no
            package.
        ComposeError: an agent and no capability (issue
            ``agent_without_capability``).
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
            # A benchmark's part of a capability's configuration (§4.10.1).
            *(
                held
                for benchmark, path in _blocks(sections.get("benchmark"), "benchmark")
                for held in _blocks(benchmark.get("capability"), f"{path}.capability")
            ),
        ],
        CapabilityRegistrar.discriminator_field,
    )
    agents = _ids(_blocks(sections.get("agent"), "agent"), AgentRegistrar.discriminator_field)
    benchmarks = _ids(
        _blocks(sections.get("benchmark"), "benchmark"), BenchmarkRegistrar.discriminator_field
    )
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
        (benchmarks, "benchmark", BENCHMARKS_PACKAGE, benchmark_ids(), "unknown_benchmark_package"),
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
    return NamedPackages(capabilities, agents, benchmarks)


def _inside(module: str, packages: Collection[str]) -> bool:
    """Whether *module* is one of *packages* or lies below one."""
    return any(module == package or module.startswith(f"{package}.") for package in packages)


def _held(
    registrars: dict[str, type[LayerRegistrar[object]]], unnamed: Collection[str]
) -> dict[str, type[LayerRegistrar[object]]]:
    """*registrars* without the inline-only layers no block the configuration can write holds.

    conscribe refuses a build holding an inline-only layer that no path from
    the top level reaches, so the paths are followed here: a top-level section
    takes every key of its layer, and a receptor the keys it allows. Only
    implementations outside the *unnamed* packages count, as only they keep a
    layer in. Without a named agent whose loop may be ``branch``, no block
    holds the ``restorer``.
    """
    named = {
        layer: {
            key: cls
            for key, cls in registrar.get_all().items()
            if not _inside(cls.__module__, unnamed)
        }
        for layer, registrar in registrars.items()
    }
    reached = {
        (layer, key)
        for layer, registrar in registrars.items()
        if not registrar.inline_only
        for key in named[layer]
    }
    pending = list(reached)
    while pending:
        layer, key = pending.pop()
        for entry in wiring_slots(named[layer][key]).values():
            target = entry.spec.registry_name
            if entry.slot is None or entry.declare_only or target == layer or target not in named:
                continue
            allowed = _allowed_keys(entry.spec)
            for target_key in named[target]:
                block = (target, target_key)
                if (allowed is None or target_key in allowed) and block not in reached:
                    reached.add(block)
                    pending.append(block)
    held = {layer for layer, _ in reached}
    return {
        layer: registrar
        for layer, registrar in registrars.items()
        if not registrar.inline_only or layer in held
    }


def _unimplemented(layer: object, name: object) -> str:
    return (
        f"no package this configuration names provides any {layer!r}; leave {name!r} out, "
        f"or name the capability, agent family or benchmark that does"
    )


#: The issue of an implementation this run does not hold.
_NO_IMPLEMENTATION = "no_implementation"
#: The issue of a required capability setting neither the run configuration nor a benchmark writes.
_SETTING_MISSING = "capability_setting_missing"
_MISSING_MESSAGE = "required, and neither the run configuration nor a benchmark writes it"
_MISSING_REMEDY = (
    "Write it in the run configuration's capability block, or select the benchmark whose "
    "block writes it"
)


@dataclass(frozen=True)
class _Problem:
    """One problem of a configuration, at the path its author wrote."""

    path: str
    message: str
    #: The issue it raises a refusal with; ``""`` for an ordinary problem,
    #: listed beside one but never raising one.
    code: str = ""
    remedy: str = "Leave it out, or name the package that provides it"

    @classmethod
    def of(cls, error: ErrorDetails, raw: object) -> _Problem:
        loc = error["loc"]
        if error["type"] == "missing" and loc:
            # The field is not written, so the walk would leave its name out.
            path = f"{_location(loc[:-1], raw)}.{loc[-1]}"
        else:
            path = _location(loc, raw)
        if error["type"] == "missing" and _in_capability_block(path):
            return cls(path, _MISSING_MESSAGE, _SETTING_MISSING, _MISSING_REMEDY)
        if error["type"] != NO_IMPLEMENTATION_ERROR_TYPE:
            return cls(path, safe_error_message(error))
        context = error.get("ctx") or {}
        return cls(
            path, _unimplemented(context.get("layer"), context.get("field")), _NO_IMPLEMENTATION
        )


def _refusal(problems: list[_Problem]) -> ComposeError:
    """One refusal listing every problem, in its text and in its issue.

    ``config check`` shows the issue, not the text. The issue's code, path and
    remedy are the first coded problem's. The packages that exist are listed
    only when some problem is an implementation the run does not hold.
    """
    first = next(problem for problem in problems if problem.code)
    unimplemented = any(problem.code == _NO_IMPLEMENTATION for problem in problems)
    known = f" ({_known_packages()})" if unimplemented else ""
    if len(problems) == 1:
        message = f"{first.path}: {first.message}{known}."
        summary = f"{first.message[0].upper()}{first.message[1:]}."
    else:
        listed = "\n".join(f"  {problem.path}: {problem.message}" for problem in problems)
        message = f"{len(problems)} problems in the configuration{known}:\n{listed}"
        summary = f"{len(problems)} problems in the configuration:\n{listed}"
    remedy = (
        f"{first.remedy} ({_known_packages()})."
        if first.code == _NO_IMPLEMENTATION
        else (f"{first.remedy}.")
    )
    return ComposeError(message, issue=CompositionIssue(first.code, first.path, summary, remedy))


#: How to put right each kind of merge problem (``CapabilityMergeError.code``).
_MERGE_REMEDIES = {
    "capability_setting_conflict": "Leave the setting out of one side, or write the same value on both",
    "capability_without_block": (
        "Add a block of that capability to the run configuration's environment, or select a "
        "benchmark for the capabilities the run has"
    ),
    "capability_contribution_invalid": (
        "Correct the benchmark's part of the capability configuration"
    ),
}


def _in_capability_block(path: str) -> bool:
    """Whether *path* (as :func:`_location` writes it) is inside one of the run's capability blocks."""
    return (
        re.match(
            rf"^({CAPABILITY_SECTION}|{ENVIRONMENT_SECTION}\[\d+\]\.capabilities)(\[\d+\])?\.", path
        )
        is not None
    )


def _known_packages() -> str:
    """The capability packages, agent families and benchmarks there are, for a refusal to list."""
    return (
        f"capability packages: {', '.join(capability_ids()) or 'none'}; "
        f"agent families: {', '.join(agent_families()) or 'none'}; "
        f"benchmarks: {', '.join(benchmark_ids()) or 'none'}"
    )


def _location(loc: tuple[int | str, ...], raw: object) -> str:
    """A pydantic error location as the configuration wrote it: ``hook[0].archive``.

    pydantic puts a discriminated union's tag in the location
    (``hook[0].trace_writer.archive``); no block has a key of that name, so
    walking *raw* along the location leaves the tag out.
    """
    path, node = "", raw
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
            node = node[part] if isinstance(node, list | tuple) and part < len(node) else None
        elif isinstance(node, Mapping) and part not in node:
            continue
        else:
            path += f".{part}" if path else part
            node = node.get(part) if isinstance(node, Mapping) else None
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
    "NamedPackages",
    "named_packages",
]
