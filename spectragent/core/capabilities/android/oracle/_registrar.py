"""The Android task oracle axis (``docs/architecture_design.md`` §11.2 "策略轴", "任务裁判与 ``AndroidSetup``").

An oracle prepares each task of its benchmark on the booted device, reads the
task's score from the live device at the Runtime's scoring point (§4.2) and
ends the task at teardown. ``AndroidCapability`` holds one as its
``task_oracle`` receptor (``none`` selects no benchmark), implements ``TaskOracle`` by
delegating ``read_score`` to it, and passes it the task's data unread: an
Android task's ``oracle_task`` (``AndroidSetup``) is the oracle's own.

The oracle also says what it needs of the device beyond ADB
(:attr:`AndroidTaskOracle.device_needs`); the capability hands that to the
provider's ``open()``, so a strategy's needs never become a provider setting
the user must keep in step with it (the browser's isolation, derived from its
network source, is the precedent, §4.14).

This module stays light: the package's ``declarations.py`` imports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import JsonValue

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.backends.base import (
        AndroidProviderSession,
        DeviceNeeds,
    )
    from spectragent.schemas.oracle_score import OracleReading

#: An Android task's data for its oracle, opaque to the capability.
OracleTask = Mapping[str, JsonValue]


@runtime_checkable
class AndroidTaskOracle(Protocol):
    """Prepares, scores and ends one task of a benchmark on the device.

    The capability calls, in order: :meth:`check_setup` (in ``prepare()``,
    and again in its own ``setup()``), :meth:`start` once the device is up,
    :meth:`read_score` at the scoring point, :meth:`stop` at teardown.
    """

    @property
    def oracle_id(self) -> str: ...

    @property
    def device_needs(self) -> DeviceNeeds:
        """What the device must offer beyond ADB for this oracle to work."""
        ...

    def check_setup(self, task: OracleTask | None) -> None:
        """Raise, naming every problem, if :meth:`start` would refuse this task here.

        Reads configuration, files and *task* only: starts nothing.
        """
        ...

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        """Prepare *task* on *device*, which the provider booted meeting :attr:`device_needs`."""
        ...

    async def read_score(self) -> OracleReading | None:
        """This task's score, read from the live device; ``None`` when there is no oracle.

        Turns its own transport failures into ordinary exceptions and lets no
        ``CancelledError`` of its own escape (``TaskOracle``, §4.9).
        """
        ...

    async def stop(self) -> None:
        """End the task and release what :meth:`start` acquired; safe after a partial start, and again."""
        ...


AndroidTaskOracleRegistrar = create_registrar(
    "android_task_oracle",
    AndroidTaskOracle,
    discriminator_field="oracle_id",
)


class AndroidTaskOracleBase(metaclass=AndroidTaskOracleRegistrar.Meta):
    """Base of every Android task oracle; ``oracle_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def oracle_id(self) -> str:
        return self.__registry_key__


def default_task_oracle() -> AndroidTaskOracle:
    """The oracle of a capability nothing configured: ``none``."""
    from spectragent.core.capabilities.android.oracle.none import NoTaskOracle

    return NoTaskOracle()
