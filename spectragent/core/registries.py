"""The registrar catalog shared by composition, inspection and generation.

The framework lists its own axes here. Every subpackage of
``spectragent/core/capabilities/`` is a capability, named by its
``capability_id``, and declares in its own ``declarations`` module what the
framework needs before importing it (``docs/architecture_design.md``
§4.2.1):

``STRATEGY_AXES``           its internal strategy axes, layer name → registrar;
``TASK_SETUP``              its task setup class (§4.10);
``OPTIONAL_EXTRA_MODULES``  modules whose top-level imports need an optional
                            extra, which discovery never imports.

So adding a capability edits no file here. Every subpackage of
``spectragent/agents/`` is an agent family, named by the part of an
``agent_id`` before its first ``.``, except the framework's own packages
listed below. A run imports the framework's own packages and only the
capability packages and agent families its configuration names
(:func:`discover_run_implementations`); generation and the other
whole-catalog tools import everything (:func:`discover_every_implementation`).
Nothing is cached: every call reads the declarations afresh and returns new
objects.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from conscribe import LayerRegistrar, discover, get_registry

if TYPE_CHECKING:
    from spectragent.schemas.task import SetupBase

#: The package whose every subpackage is one capability.
CAPABILITIES_PACKAGE = "spectragent.core.capabilities"
#: The package whose every subpackage is one agent family, except the framework's own.
AGENTS_PACKAGE = "spectragent.agents"
#: The subpackages of :data:`AGENTS_PACKAGE` that are the framework's, not families:
#: the memory axis and the prompt rendering the families share.
AGENT_FRAMEWORK_PACKAGES = frozenset({"memory", "rendering"})
#: Where a run finds the framework's own implementations. The capability packages
#: and agent families below them are walked only when a configuration names them.
FRAMEWORK_PACKAGES = (AGENTS_PACKAGE, "spectragent.core", "spectragent.schemas")
#: The module each capability package declares itself in.
DECLARATIONS_MODULE = "declarations"


@dataclass(frozen=True)
class CapabilityDeclarations:
    """What one capability package declares in its ``declarations`` module."""

    capability_id: str
    #: Layer name → registrar, in declared order.
    strategy_axes: Mapping[str, type]
    task_setup: type[SetupBase]
    #: Fully qualified names, all inside the package.
    optional_extra_modules: tuple[str, ...]


def capability_ids() -> tuple[str, ...]:
    """Every capability package's ``capability_id``, sorted. Imports no capability package."""
    return _subpackages(CAPABILITIES_PACKAGE)


def agent_families() -> tuple[str, ...]:
    """Every agent family's name, sorted. Imports no family package."""
    return tuple(
        name for name in _subpackages(AGENTS_PACKAGE) if name not in AGENT_FRAMEWORK_PACKAGES
    )


def capability_declarations(
    capabilities: Iterable[str] | None = None,
) -> tuple[CapabilityDeclarations, ...]:
    """The declarations of *capabilities* (every capability package when ``None``), in ``capability_id`` order.

    Only each package's ``declarations`` module is imported, and it imports no
    implementation.
    """
    selected = capability_ids() if capabilities is None else sorted(set(capabilities))
    return tuple(_read_declarations(capability_id) for capability_id in selected)


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


def discover_implementations(*packages: str, exclude: Iterable[str] = ()) -> None:
    """Import every module under *packages* so that its classes register.

    conscribe's strict ``discover``, minus *exclude* and every module a
    capability lists in ``OPTIONAL_EXTRA_MODULES``: those import an optional
    extra at top level and register nothing, so a machine without the extra
    still composes, generates and reads traces.
    """
    optional = [
        module
        for capability in capability_declarations()
        for module in capability.optional_extra_modules
    ]
    discover(*packages, exclude=[*exclude, *optional])


def discover_run_implementations(
    *, capabilities: Collection[str], families: Collection[str]
) -> None:
    """Import the framework's own packages, the named capability packages and agent families.

    What one run's configuration names, and nothing else: every other
    capability package and family is excluded from the walk, so neither it nor
    its ``declarations`` module is imported. The caller checks that every name
    has a package.
    """
    optional = [
        module
        for capability in capability_declarations(capabilities)
        for module in capability.optional_extra_modules
    ]
    discover(
        *FRAMEWORK_PACKAGES,
        exclude=[
            *(
                f"{CAPABILITIES_PACKAGE}.{name}"
                for name in capability_ids()
                if name not in capabilities
            ),
            *(f"{AGENTS_PACKAGE}.{name}" for name in agent_families() if name not in families),
            *optional,
        ],
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

    It first reads every capability package's declarations, which registers
    each package's setup: a caller (a loader built directly, a test) need not
    have imported the capability, and two packages claiming one env type are
    always caught.

    Raises:
        ValueError: no registered setup claims *env_type*, or more than one does.
    """
    from spectragent.schemas.task import SetupBase, TaskSetupRegistrar

    capability_declarations()
    found = [
        setup
        for _, setup in sorted(TaskSetupRegistrar.get_all().items())
        if issubclass(setup, SetupBase) and env_type in setup.__env_types__
    ]
    if len(found) > 1:
        names = ", ".join(setup.__name__ for setup in found)
        raise ValueError(f"task setups {names} all claim env_type={env_type!r}; one may")
    if not found:
        known = sorted(
            {env for setup in TaskSetupRegistrar.get_all().values() for env in setup.__env_types__}
        )
        raise ValueError(
            f"No registered task setup claims env_type={env_type!r} (known: "
            f"{', '.join(known) or 'none'}). A capability package registers its task setup "
            f"and names it TASK_SETUP in its {DECLARATIONS_MODULE}.py."
        )
    return found[0]


def _framework_axes() -> dict[str, type]:
    from spectragent.agents._registrar import AgentRegistrar
    from spectragent.agents.memory._registrar import MemoryRegistrar
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
    from spectragent.evaluator._registrar import EvaluatorRegistrar
    from spectragent.export._registrar import ExporterRegistrar
    from spectragent.export.translate._registrar import ActionTranslatorRegistrar
    from spectragent.replay_review._registrar import ReplayReviewerRegistrar

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
        "evaluator": EvaluatorRegistrar,
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
            f"axes, task setup and optional-extra modules there "
            f"(docs/architecture_design.md §4.2.1)."
        ) from exc
    from spectragent.schemas.task import SetupBase

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
    return CapabilityDeclarations(
        capability_id=capability_id,
        strategy_axes=dict(axes),
        task_setup=setup,
        optional_extra_modules=optional,
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
