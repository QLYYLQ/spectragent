"""The ``replayability`` axis: how far a recording is pinned for replay (§4.14).

A replayability *tier* is a ``BrowserCapability`` receptor
(``__wiring__["replayability"]``), with the shape of ``typing_rhythm``: the
strategy is the switch, ``off`` is the default and the no-op, and there is no
boolean beside it. A tier owns three things and nothing else:

- the Chrome **launch switches** only it may set (:meth:`launch_args`,
  delivered through a provider declaring ``LAUNCH_ARGS``);
- the **device scale factor** the control connection pins the viewport to
  (:attr:`device_scale_factor`; ``0.0`` keeps the platform's);
- a per-run :class:`ReplayabilityPlan` (:meth:`plan`): the commands the
  recorder connection sends each new target before it runs, and the
  fingerprint fields describing the tier.

The configured instance holds knobs only; everything run-specific (T0, the
browser's own user agent) arrives as :meth:`plan` arguments and lives on the
plan. No ``from __future__ import annotations`` here or in the tier modules:
conscribe reads the knobs' ``Annotated`` metadata at class creation.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.capabilities.browser.recorder import (
    FRAME_TARGET_TYPES,
    WORKER_TARGET_TYPES,
    CDPCommand,
    RecorderParticipantBase,
    RecorderTarget,
)


@dataclass(frozen=True)
class ReplayabilityPlan(RecorderParticipantBase):
    """One run's replayability plan — and one recorder participant.

    ``frame_commands`` go to every page and iframe target before it resumes,
    ``worker_commands`` to every worker target. The remaining fields are what
    the environment fingerprint records about the tier (``env/run.browser.json``).
    """

    tier: str
    #: The tier's knobs, as recorded in the fingerprint.
    params: Mapping[str, Any] = field(default_factory=dict)
    #: Epoch milliseconds the shim's clock starts from; ``None`` without a shim.
    t0_ms: int | None = None
    #: sha256 of the shim *template* (independent of T0 and seed).
    shim_sha256: str | None = None
    #: The launch switches the tier owns (its :meth:`launch_args`).
    replayability_args: tuple[str, ...] = ()
    frame_commands: tuple[CDPCommand, ...] = ()
    worker_commands: tuple[CDPCommand, ...] = ()

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        if target.type in FRAME_TARGET_TYPES:
            return self.frame_commands
        if target.type in WORKER_TARGET_TYPES:
            return self.worker_commands
        return ()

    def fingerprint(self) -> dict[str, Any]:
        """The tier as the environment fingerprint records it."""
        return {
            "tier": self.tier,
            "params": dict(self.params),
            "t0_ms": self.t0_ms,
            "shim_sha256": self.shim_sha256,
            "replayability_args": list(self.replayability_args),
        }


@runtime_checkable
class ReplayabilityProtocol(Protocol):
    """A replayability tier (``off`` / ``pinned``)."""

    @property
    def replayable(self) -> bool:
        """``False`` only for ``off``: the strategy is the switch."""
        ...

    @property
    def needs_archive_clock(self) -> bool:
        """Whether T0 must come from a recorded archive (``base_time: from_archive``).

        Valid only with an archive network source (M30); the cooperation
        check refuses it otherwise, naming both sides.
        """
        ...

    @property
    def device_scale_factor(self) -> float:
        """DPR for the control connection's viewport; ``0.0`` = the platform's.

        A property, not a method: on ``pinned`` it *is* the YAML knob of the
        same name, so the configured value is the attribute a reader sees.
        """
        ...

    def launch_args(self) -> tuple[str, ...]:
        """Chrome switches this tier owns; ``()`` for ``off``."""
        ...

    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan:
        """This run's plan. The configured instance keeps no run state."""
        ...


ReplayabilityRegistrar = create_registrar(
    "replayability",
    ReplayabilityProtocol,
    discriminator_field="replayability_id",
)


class ReplayabilityBase(metaclass=ReplayabilityRegistrar.Meta):
    """Base for replayability tiers — subclass to auto-register.

    Each tier sets ``__registry_key__`` explicitly (the YAML-facing
    ``replayability_id``) and takes its knobs as ``__init__`` keyword
    parameters. Only genuinely safe defaults live here: a tier is replayable
    unless it is ``off``, and its clock starts at the run unless it says
    otherwise. The launch switches, the DPR (``device_scale_factor``, a
    property or a knob attribute) and the plan have no safe default and must
    be written by every tier.
    """

    __abstract__ = True
    __registry_key__: str

    @property
    def replayability_id(self) -> str:
        return self.__registry_key__

    @property
    def replayable(self) -> bool:
        return True

    @property
    def needs_archive_clock(self) -> bool:
        return False

    def launch_args(self) -> tuple[str, ...]:
        raise NotImplementedError(
            f"replayability tier {self.replayability_id!r} must say which launch switches it owns"
        )

    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan:
        raise NotImplementedError(
            f"replayability tier {self.replayability_id!r} must build a ReplayabilityPlan"
        )


def default_replayability() -> ReplayabilityProtocol:
    """The tier used when no configuration named one: ``off``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built capability.
    """
    from spectragent.core.capabilities.browser.replay.off import OffReplayability

    return OffReplayability()
