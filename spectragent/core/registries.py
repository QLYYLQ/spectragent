"""The registrar catalog shared by composition, inspection and generation.

The framework lists its own axes here. Every subpackage of
``spectragent/core/capabilities/`` is a capability, named by its
``capability_id``, and declares its internal strategy axes in its own
``declarations`` module (``STRATEGY_AXES``, layer name → registrar), so
adding a capability edits no file here (``docs/architecture_design.md``
§4.2.1). Nothing is cached: every call reads the declarations afresh and
returns new objects.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass

from conscribe import LayerRegistrar, get_registry

#: The package whose every subpackage is one capability.
CAPABILITIES_PACKAGE = "spectragent.core.capabilities"
#: The module each capability package declares itself in.
DECLARATIONS_MODULE = "declarations"


@dataclass(frozen=True)
class CapabilityDeclarations:
    """What one capability package declares in its ``declarations`` module."""

    capability_id: str
    #: Layer name → registrar, in declared order.
    strategy_axes: Mapping[str, type]


def capability_declarations() -> tuple[CapabilityDeclarations, ...]:
    """Every capability package's declarations, in ``capability_id`` order.

    ``pkgutil`` lists the packages without importing them; only each
    package's ``declarations`` module is imported, and it imports no
    implementation.
    """
    package = importlib.import_module(CAPABILITIES_PACKAGE)
    capability_ids = sorted(
        module.name for module in pkgutil.iter_modules(package.__path__) if module.ispkg
    )
    return tuple(_read_declarations(capability_id) for capability_id in capability_ids)


def collect_registrars() -> dict[str, type]:
    """Every YAML-configurable registrar by layer name, in the one shared order.

    The framework axes in the order listed below, then each capability
    package's axes in ``capability_id`` order and declared order. The
    generator, the fingerprints and ``config explain`` all take this order:
    generated output does not depend on it, but a ``0`` / ``0.0`` bound
    prints with the build order and moves the fingerprints. Each call returns
    a fresh mapping; a caller may select a subset without affecting another.
    """
    registrars = _framework_axes()
    for capability in capability_declarations():
        for layer, registrar in capability.strategy_axes.items():
            if layer in registrars:
                raise ValueError(
                    f"capability {capability.capability_id!r} declares the layer {layer!r}, "
                    f"which the framework or another capability already declares"
                )
            registrars[layer] = registrar
    return registrars


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
            f"axes there (docs/architecture_design.md §4.2.1)."
        ) from exc
    axes = getattr(module, "STRATEGY_AXES", None)
    if not isinstance(axes, Mapping):
        raise TypeError(f"{name}.STRATEGY_AXES must map layer names to registrars, not {axes!r}")
    for layer, registrar in axes.items():
        _check_layer(name, layer, registrar)
    return CapabilityDeclarations(capability_id=capability_id, strategy_axes=dict(axes))


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
