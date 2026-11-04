"""The registrar catalog shared by composition, inspection and generation.

The framework lists its own axes here. Every subpackage of
``spectragent/core/capabilities/`` is a capability, named by its
``capability_id``, and declares in its own ``declarations`` module what the
framework needs before importing it (``docs/architecture_design.md``
§4.2.1):

``STRATEGY_AXES``           its internal strategy axes, layer name → registrar;
``TASK_SETUP``              its task setup class (§4.10);
``OPTIONAL_EXTRA_MODULES``  modules whose top-level imports need an optional
                            extra, which discovery never imports;
``START_FLAGS``             its single-task start flags, which ``spectragent
                            run`` generates (§4.9 "起始参数");
``TEMPLATE_BLOCK``          where ``spectragent config template`` starts its
                            block in a new template (§4.17);
``TEMPLATE_FLAGS``          its template flags, which ``config template``
                            generates (§4.17).

So adding a capability edits no file here. Every subpackage of
``spectragent/agents/`` is an agent family, named by the part of an
``agent_id`` before its first ``.``, except the framework's own packages
listed below, and every subpackage of ``spectragent/benchmarks/`` is a
benchmark, named by its ``benchmark_id`` (§4.10.1). A run imports the
framework's own packages and only the capability packages, agent families and
benchmarks its configuration names (:func:`discover_implementations` narrowed
to them); generation and the other whole-catalog tools import everything
(:func:`discover_every_implementation`).
Nothing is cached: every call reads the declarations afresh and returns new
objects.
"""

from __future__ import annotations

import copy
import importlib
import pkgutil
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

from conscribe import LayerRegistrar, discover, get_registry
from pydantic import JsonValue

if TYPE_CHECKING:
    from spectragent.schemas.task import SetupBase, StartFlag

#: The package whose every subpackage is one capability.
CAPABILITIES_PACKAGE = "spectragent.core.capabilities"
#: The package whose every subpackage is one agent family, except the framework's own.
AGENTS_PACKAGE = "spectragent.agents"
#: The subpackages of :data:`AGENTS_PACKAGE` that are the framework's, not families:
#: the memory axis and the prompt rendering the families share.
AGENT_FRAMEWORK_PACKAGES = frozenset({"memory", "rendering"})
#: The package whose every subpackage is one benchmark (``docs/architecture_design.md`` §4.10.1).
BENCHMARKS_PACKAGE = "spectragent.benchmarks"
#: Where a run finds the framework's own implementations. The capability packages,
#: agent families and benchmarks below them are walked only when a configuration
#: names them.
FRAMEWORK_PACKAGES = (
    AGENTS_PACKAGE,
    BENCHMARKS_PACKAGE,
    "spectragent.core",
    "spectragent.schemas",
    "spectragent.verifiers",
)
#: The module each capability package declares itself in.
DECLARATIONS_MODULE = "declarations"


@dataclass(frozen=True)
class TemplateFlag:
    """A ``config template`` flag a capability declares in its ``declarations.py`` (``TEMPLATE_FLAGS``).

    ``docs/architecture_design.md`` §4.17 "capability 的模板起点": ``spectragent
    config template`` adds every capability's flags, listed by capability, and
    refuses one whose capability the template's environment holds no block of.
    Its value is a registry key of the receptor's layer, and replaces the
    strategy in that receptor of the capability's block, as ``--agent``
    replaces the agent.

    Attributes:
        flag: The command-line flag, ``--replayability``.
        receptor: The receptor of the capability block it selects, ``replayability``.
        help: What it selects, for ``--help``.
    """

    flag: str
    receptor: str
    help: str


class CapabilityFlag(Protocol):
    """What the CLI reads of a capability's start or template flag to offer it."""

    @property
    def flag(self) -> str: ...
    @property
    def help(self) -> str: ...


@dataclass(frozen=True)
class CapabilityDeclarations:
    """What one capability package declares in its ``declarations`` module."""

    capability_id: str
    #: Layer name → registrar, in declared order.
    strategy_axes: Mapping[str, type]
    task_setup: type[SetupBase]
    #: Fully qualified names, all inside the package.
    optional_extra_modules: tuple[str, ...]
    #: Its single-task start flags, in declared order.
    start_flags: tuple[StartFlag, ...]
    #: Where a new ``config template`` starts its block: a capability block as
    #: YAML writes one, without its ``capability_id``. A starting point the
    #: template writes into the YAML, never a default a run falls back on.
    template_block: Mapping[str, JsonValue]
    #: Its ``config template`` flags, in declared order.
    template_flags: tuple[TemplateFlag, ...]


def capability_ids() -> tuple[str, ...]:
    """Every capability package's ``capability_id``, sorted. Imports no capability package."""
    return _subpackages(CAPABILITIES_PACKAGE)


def agent_families() -> tuple[str, ...]:
    """Every agent family's name, sorted. Imports no family package."""
    return tuple(
        name for name in _subpackages(AGENTS_PACKAGE) if name not in AGENT_FRAMEWORK_PACKAGES
    )


def benchmark_ids() -> tuple[str, ...]:
    """Every benchmark package's ``benchmark_id``, sorted. Imports no benchmark package."""
    return _subpackages(BENCHMARKS_PACKAGE)


def capability_declarations(
    capabilities: Iterable[str] | None = None,
) -> tuple[CapabilityDeclarations, ...]:
    """The declarations of *capabilities* (every capability package when ``None``), in ``capability_id`` order.

    Only each package's ``declarations`` module is imported, and it imports no
    implementation.
    """
    selected = capability_ids() if capabilities is None else sorted(set(capabilities))
    return tuple(_read_declarations(capability_id) for capability_id in selected)


def start_flags() -> dict[str, tuple[StartFlag, ...]]:
    """Every capability package's single-task start flags, by ``capability_id``, in that order.

    The static union ``spectragent run`` offers (``docs/architecture_design.md``
    §4.9 "起始参数"): read from every package's declarations, whatever a run
    names, so ``--help`` and tab completion list them all. Imports no
    implementation.

    Raises:
        ValueError: two packages declare one flag.
    """
    return _flags_by_capability(
        "start flag", {item.capability_id: item.start_flags for item in capability_declarations()}
    )


def template_flags() -> dict[str, tuple[TemplateFlag, ...]]:
    """Every capability package's ``config template`` flags, by ``capability_id``, in that order.

    The static union ``spectragent config template`` offers
    (``docs/architecture_design.md`` §4.17 "capability 的模板起点"), read like
    :func:`start_flags`. Imports no implementation.

    Raises:
        ValueError: two packages declare one flag.
    """
    return _flags_by_capability(
        "template flag",
        {item.capability_id: item.template_flags for item in capability_declarations()},
    )


_Flag = TypeVar("_Flag", bound=CapabilityFlag)


def _flags_by_capability(
    kind: str, declared: Mapping[str, tuple[_Flag, ...]]
) -> dict[str, tuple[_Flag, ...]]:
    """*declared* as given, once no two capabilities declare one flag."""
    owners: dict[str, str] = {}
    for capability_id, flags in declared.items():
        for flag in flags:
            owner = owners.setdefault(flag.flag, capability_id)
            if owner != capability_id:
                raise ValueError(
                    f"capabilities {owner!r} and {capability_id!r} both declare the {kind} "
                    f"{flag.flag}"
                )
    return dict(declared)


def collect_registrars(capabilities: Iterable[str] | None = None) -> dict[str, type]:
    """The YAML-configurable registrars by layer name, in the one shared order.

    The framework axes in the order listed below, then the axes of
    *capabilities* (every capability package when ``None``) in
    ``capability_id`` order and declared order; a run passes the capabilities
    its configuration names. The generator, the fingerprints and
    ``config explain`` all take this order: generated output does not depend
    on it, but a ``0`` / ``0.0`` bound prints with the build order and moves
    the fingerprints. Each call returns a fresh mapping; a caller may select a
    subset without affecting another.
    """
    registrars = _framework_axes()
    for capability in capability_declarations(capabilities):
        for layer, registrar in capability.strategy_axes.items():
            if layer in registrars:
                raise ValueError(
                    f"capability {capability.capability_id!r} declares the layer {layer!r}, "
                    f"which the framework or another capability already declares"
                )
            registrars[layer] = registrar
    return registrars


def discover_implementations(
    *packages: str,
    exclude: Iterable[str] = (),
    capabilities: Collection[str] | None = None,
    families: Collection[str] | None = None,
    benchmarks: Collection[str] | None = None,
) -> None:
    """Import every module under *packages* so that its classes register.

    conscribe's strict ``discover``, minus *exclude* and every module a
    capability lists in ``OPTIONAL_EXTRA_MODULES``: those import an optional
    extra at top level and register nothing, so a machine without the extra
    still composes, generates and reads traces. *capabilities*, *families* and
    *benchmarks* narrow the walk to those capability packages, agent families
    and benchmarks (``None``: every one), as a run narrows it to what its
    configuration names: the walk excludes the others and reads no excluded
    capability's declarations. It does not stop a walked module from
    importing an excluded one itself, so framework code imports a
    capability's modules only inside the functions that use them;
    ``tests/test_core/test_composer/test_configured_packages.py`` checks in
    fresh interpreters that a run imports no package it does not name.
    """
    walked = capability_ids() if capabilities is None else capabilities
    optional = [
        module
        for capability in capability_declarations(walked)
        for module in capability.optional_extra_modules
    ]
    unnamed = unnamed_packages(capabilities, families, benchmarks)
    discover(*packages, exclude=[*exclude, *unnamed, *optional])


def unnamed_packages(
    capabilities: Collection[str] | None = None,
    families: Collection[str] | None = None,
    benchmarks: Collection[str] | None = None,
) -> tuple[str, ...]:
    """The capability packages, agent families and benchmarks outside the ones named.

    ``None`` names every one. What a run does not walk, and what may not decide
    which layers its schema holds: an earlier import can have left one of
    them registered.
    """
    return (
        *(
            f"{CAPABILITIES_PACKAGE}.{name}"
            for name in capability_ids()
            if capabilities is not None and name not in capabilities
        ),
        *(
            f"{AGENTS_PACKAGE}.{name}"
            for name in agent_families()
            if families is not None and name not in families
        ),
        *(
            f"{BENCHMARKS_PACKAGE}.{name}"
            for name in benchmark_ids()
            if benchmarks is not None and name not in benchmarks
        ),
    )


def discover_every_implementation() -> None:
    """Import every package of ``spectragent`` except generated code and the generator itself.

    The whole catalog: what ``spectragent config generate`` generates, the
    fingerprints compare and ``components list`` lists.
    """
    discover_implementations(
        "spectragent", exclude=["spectragent.generated", "spectragent.scripts"]
    )


def setup_class_for_env(env_type: str) -> type[SetupBase]:
    """The registered task setup class whose ``__env_types__`` claims *env_type*.

    The setups already registered come first. Only on a miss is every
    capability package's declarations read, which registers each package's
    setup, and the lookup runs again: a caller that never imported the
    capability (a loader built directly, a test) still finds it, and a package
    whose declarations fail to import breaks only a lookup that needs them.
    That no two packages claim one env type is checked over every package by
    ``tests/test_core/test_capabilities/test_capability_declarations.py``.

    Raises:
        ValueError: no setup claims *env_type*, even after reading every package.
    """
    from spectragent.schemas.task import SetupBase, TaskSetupRegistrar

    def claimant() -> type[SetupBase] | None:
        return next(
            (
                setup
                for _, setup in sorted(TaskSetupRegistrar.get_all().items())
                if issubclass(setup, SetupBase) and env_type in setup.__env_types__
            ),
            None,
        )

    found = claimant()
    if found is None:
        capability_declarations()
        found = claimant()
    if found is None:
        known = sorted(
            {env for setup in TaskSetupRegistrar.get_all().values() for env in setup.__env_types__}
        )
        raise ValueError(
            f"No registered task setup claims env_type={env_type!r} (known: "
            f"{', '.join(known) or 'none'}). A capability package registers its task setup "
            f"and names it TASK_SETUP in its {DECLARATIONS_MODULE}.py."
        )
    return found


def _framework_axes() -> dict[str, type]:
    from spectragent.agents._registrar import AgentRegistrar
    from spectragent.agents.memory._registrar import MemoryRegistrar
    from spectragent.benchmarks._registrar import BenchmarkRegistrar
    from spectragent.benchmarks._setup_registrar import BenchmarkSetupRegistrar
    from spectragent.core.actions._registrar import ActionRegistrar
    from spectragent.core.archive._registrar import ArchiverRegistrar
    from spectragent.core.capabilities._registrar import CapabilityRegistrar
    from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
    from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
    from spectragent.core.data_loader.task.step_selector import StepSelectorRegistrar
    from spectragent.core.environments._registrar import EnvironmentRegistrar
    from spectragent.core.hooks._registrar import HookRegistrar
    from spectragent.core.llm._registrar import LLMRegistrar
    from spectragent.core.llm.decision_registrar import DecisionModelRegistrar
    from spectragent.core.loops._registrar import AgentLoopRegistrar
    from spectragent.core.loops.restore import RestorerRegistrar
    from spectragent.export._registrar import ExporterRegistrar
    from spectragent.export.translate._registrar import ActionTranslatorRegistrar
    from spectragent.replay_review._registrar import ReplayReviewerRegistrar
    from spectragent.verifiers._registrar import VerifierRegistrar

    return {
        "archiver": ArchiverRegistrar,
        "action": ActionRegistrar,
        "capability": CapabilityRegistrar,
        "restorer": RestorerRegistrar,
        "agent_loop": AgentLoopRegistrar,
        "llm": LLMRegistrar,
        "decision_model": DecisionModelRegistrar,
        "replay_reviewer": ReplayReviewerRegistrar,
        "memory": MemoryRegistrar,
        "environment": EnvironmentRegistrar,
        "hook": HookRegistrar,
        "step_selector": StepSelectorRegistrar,
        "task_loader": TaskLoaderRegistrar,
        "eval_loader": EvalLoaderRegistrar,
        "benchmark_setup": BenchmarkSetupRegistrar,
        "benchmark": BenchmarkRegistrar,
        "verifier": VerifierRegistrar,
        "action_translator": ActionTranslatorRegistrar,
        "exporter": ExporterRegistrar,
        "agent": AgentRegistrar,
    }


def _subpackages(package: str) -> tuple[str, ...]:
    """The names of *package*'s subpackages, sorted; ``pkgutil`` imports none of them."""
    path = importlib.import_module(package).__path__
    return tuple(sorted(module.name for module in pkgutil.iter_modules(path) if module.ispkg))


def _read_declarations(capability_id: str) -> CapabilityDeclarations:
    """Import and check one capability package's ``declarations`` module."""
    package = f"{CAPABILITIES_PACKAGE}.{capability_id}"
    name = f"{package}.{DECLARATIONS_MODULE}"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        raise ImportError(
            f"capability package {package} has no {DECLARATIONS_MODULE} module. Every "
            f"subpackage of {CAPABILITIES_PACKAGE} is a capability and declares its strategy "
            f"axes, task setup, optional-extra modules, start flags and template starting "
            f"block and flags there "
            f"(docs/architecture_design.md §4.2.1)."
        ) from exc
    from spectragent.schemas.task import SetupBase, StartFlag

    axes = getattr(module, "STRATEGY_AXES", None)
    if not isinstance(axes, Mapping):
        raise TypeError(f"{name}.STRATEGY_AXES must map layer names to registrars, not {axes!r}")
    for layer, registrar in axes.items():
        _check_layer(name, layer, registrar)
    setup = getattr(module, "TASK_SETUP", None)
    if not (isinstance(setup, type) and issubclass(setup, SetupBase)):
        raise TypeError(f"{name}.TASK_SETUP must be a SetupBase subclass, not {setup!r}")
    optional = getattr(module, "OPTIONAL_EXTRA_MODULES", None)
    if not (
        isinstance(optional, tuple)
        and all(isinstance(item, str) and item.startswith(f"{package}.") for item in optional)
    ):
        raise TypeError(
            f"{name}.OPTIONAL_EXTRA_MODULES must be a tuple of module names inside {package}, "
            f"not {optional!r}"
        )
    flags = getattr(module, "START_FLAGS", None)
    if not (isinstance(flags, tuple) and all(isinstance(flag, StartFlag) for flag in flags)):
        raise TypeError(f"{name}.START_FLAGS must be a tuple of StartFlag, not {flags!r}")
    for flag in flags:
        if not flag.flag.startswith("--") or not flag.key or not flag.help:
            raise ValueError(
                f"{name}.START_FLAGS: {flag!r} needs a flag starting with '--', a context key "
                f"and a help text"
            )
    for field in ("flag", "key"):
        values = [getattr(flag, field) for flag in flags]
        if len(set(values)) != len(values):
            raise ValueError(f"{name}.START_FLAGS declares one {field} twice: {values}")
    block = getattr(module, "TEMPLATE_BLOCK", None)
    if not (isinstance(block, Mapping) and all(isinstance(key, str) for key in block)):
        raise TypeError(
            f"{name}.TEMPLATE_BLOCK must map the fields of a capability block to their "
            f"starting values, not {block!r}"
        )
    if "capability_id" in block:
        raise ValueError(f"{name}.TEMPLATE_BLOCK names the capability_id: leave it out")
    template = getattr(module, "TEMPLATE_FLAGS", None)
    if not (
        isinstance(template, tuple) and all(isinstance(flag, TemplateFlag) for flag in template)
    ):
        raise TypeError(f"{name}.TEMPLATE_FLAGS must be a tuple of TemplateFlag, not {template!r}")
    for flag in template:
        if not flag.flag.startswith("--") or not flag.receptor or not flag.help:
            raise ValueError(
                f"{name}.TEMPLATE_FLAGS: {flag!r} needs a flag starting with '--', a receptor "
                f"and a help text"
            )
    for field in ("flag", "receptor"):
        values = [getattr(flag, field) for flag in template]
        if len(set(values)) != len(values):
            raise ValueError(f"{name}.TEMPLATE_FLAGS declares one {field} twice: {values}")
    return CapabilityDeclarations(
        capability_id=capability_id,
        strategy_axes=dict(axes),
        task_setup=setup,
        optional_extra_modules=optional,
        start_flags=flags,
        template_block=copy.deepcopy(dict(block)),
        template_flags=template,
    )


def _check_layer(where: str, layer: object, registrar: object) -> None:
    """*registrar* is a conscribe registrar whose ``create_registrar`` name is *layer*."""
    if not (isinstance(registrar, type) and issubclass(registrar, LayerRegistrar)):
        raise TypeError(f"{where}.STRATEGY_AXES[{layer!r}] is {registrar!r}, not a registrar")
    registry = get_registry(layer) if isinstance(layer, str) else None
    if registry is None or registry.protocol is not registrar.protocol:
        raise ValueError(
            f"{where}.STRATEGY_AXES[{layer!r}] is the registrar of "
            f"{registrar.protocol.__name__}, and {layer!r} is not its create_registrar name"
        )
