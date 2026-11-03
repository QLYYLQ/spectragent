"""The benchmark setup component registry (``docs/architecture_design.md`` §4.10.1, §4.9 "基准组件由 capability 承载").

A benchmark's own setup of each task (AndroidWorld's initialize, a MiniWoB
page's goal; later KernelBench's container) is a registered component written
in the benchmark block's ``setup`` receptor, never as a top-level section. It
names the capability that hosts it (:attr:`BenchmarkSetup.capability_id`):
``AgentRuntime.prepare()`` routes it there, the capability's ``check_setup``
asks it, and it is bound as the last step of ``prepare()``, as archivers are.
The host gives it access to its device or environment at setup, lets it start
the task, and asks it first at teardown. What a component must offer its host
is the host's own protocol (the Android capability's is
``AndroidBenchmarkSetup``): only the hosting capability knows what access it
gives.

What the device or environment must offer beyond its plain form comes from the
component (Android: ``DeviceNeeds``), never from a provider setting. Judging is
a verifier's, never a setup component's, once the benchmark's live verifier
lands: until then AndroidWorld's component hands the Android capability's
``oracle_identity`` and ``read_score`` (``TaskOracle``) its identity and its
judgment, from the service it started, and the environment keeps them as the
run's live verdict.

This module stays light: ``core/registries.py`` lists the registrar with the
framework axes.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar


@runtime_checkable
class BenchmarkSetup(Protocol):
    """A benchmark's own setup of each of its tasks, hosted by one capability."""

    @property
    def setup_id(self) -> str: ...

    @property
    def capability_id(self) -> str:
        """The ``capability_id`` of the capability that hosts it."""
        ...


BenchmarkSetupRegistrar = create_registrar(
    "benchmark_setup",
    BenchmarkSetup,
    discriminator_field="setup_id",
    inline_only=True,
)


class BenchmarkSetupBase(metaclass=BenchmarkSetupRegistrar.Meta):
    """Base of every benchmark setup component; ``setup_id`` is the registry key.

    A concrete component sets ``__registry_key__`` and ``__hosted_by__``, the
    ``capability_id`` of the capability that hosts it.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    #: The ``setup_id`` this component registers under.
    __registry_key__: ClassVar[str]
    #: The ``capability_id`` of the capability that hosts it.
    __hosted_by__: ClassVar[str]

    @property
    def setup_id(self) -> str:
        return self.__registry_key__

    @property
    def capability_id(self) -> str:
        return self.__hosted_by__


__all__ = ["BenchmarkSetup", "BenchmarkSetupBase", "BenchmarkSetupRegistrar"]
