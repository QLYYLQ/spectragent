"""The ``replayability`` axis: how far a recording is pinned for replay (§4.14).

A replayability *tier* is a ``BrowserCapability`` receptor
(``__wiring__["replayability"]``), with the shape of ``typing_rhythm``: the
strategy is the switch, ``none`` is the default and the no-op, and there is no
boolean beside it. A tier owns three things and nothing else:

- the Chrome **launch switches** and **launch environment** only it may set
  (:meth:`launch_args`, :meth:`launch_env`, delivered together through a
  provider declaring ``LAUNCH_ARGS``);
- the **device scale factor** the control connection pins the viewport to
  (:attr:`device_scale_factor`; ``0.0`` keeps the platform's);
- a per-run :class:`~.replayability_plan.ReplayabilityPlan` (:meth:`plan`): the
  commands the recorder connection sends each new target before it runs, and the
  fingerprint fields describing the tier.

The browser's ``declarations.py`` imports this module, so it imports nothing of
the recorder: the plan's types are for annotations only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from conscribe import create_registrar

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.client_hints import ClientHints
    from spectragent.core.capabilities.browser.color_scheme import ColorScheme
    from spectragent.core.capabilities.browser.replay.replayability_plan import (
        ReplayabilityPlan,
    )


@runtime_checkable
class ReplayabilityProtocol(Protocol):
    """A replayability tier (``none`` / ``pinned``)."""

    @property
    def replayable(self) -> bool:
        """``False`` only for ``none``: the strategy is the switch."""
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

    @property
    def needs_client_hints(self) -> bool:
        """Whether :meth:`plan` needs the browser's own client hints.

        ``True`` for a tier whose plan overrides the user agent: without them
        the override would empty ``navigator.userAgentData`` and send no
        ``Sec-CH-UA``. The capability reads them right after launch
        (``client_hints.measure_client_hints``) and passes them to :meth:`plan`.
        """
        ...

    @property
    def needs_color_scheme(self) -> bool:
        """Whether the plan pins the recording's measured media theme.

        Read after launch for a live run; read from the source fingerprint
        before launch for an archive replay. Never inferred from a default.
        """
        ...

    def launch_args(self) -> tuple[str, ...]:
        """Chrome switches this tier owns; ``()`` for ``none``."""
        ...

    def launch_env(self) -> Mapping[str, str]:
        """Environment variables this tier sets on the browser process; ``{}`` for ``none``.

        They reach the browser the way the switches do (``LAUNCH_ARGS``) and
        enter the launch flags the fingerprint digests, so a replay compares
        them before launch.
        """
        ...

    def plan(
        self,
        *,
        t0_ms: int,
        browser_user_agent: str,
        run_started_ms: int | None = None,
        client_hints: ClientHints | None = None,
        color_scheme: ColorScheme | None = None,
    ) -> ReplayabilityPlan:
        """This run's plan. The configured instance keeps no run state.

        *t0_ms* is what the tier's clock reads at the run's start;
        *run_started_ms* is that start on the host's wall clock, from which the
        clock advances in real time. ``None`` means the clock is not shifted:
        the run starts at T0 itself (``base_time: run_start``). A replay whose
        T0 is the recording's passes its own start.
        *client_hints* are the browser's own, read after launch when
        :attr:`needs_client_hints`; ``None`` when not read (the consistency
        check plans with none, for the parameters only).
        *color_scheme* is the recording's measured theme, inherited from the
        archive when replaying; ``None`` only for an unpinned tier or a plan
        built before launch to compare configuration parameters.
        """
        ...


ReplayabilityRegistrar = create_registrar(
    "replayability",
    ReplayabilityProtocol,
    discriminator_field="replayability_id",
    inline_only=True,
)


class ReplayabilityBase(metaclass=ReplayabilityRegistrar.Meta):
    """Base for replayability tiers — subclass to auto-register.

    Each tier sets ``__registry_key__`` explicitly (the YAML-facing
    ``replayability_id``) and takes its knobs as ``__init__`` keyword
    parameters. Only genuinely safe defaults live here: a tier is replayable
    unless it is ``none``, and its clock starts at the run unless it says
    otherwise, and it needs no client hints unless its plan overrides the
    user agent. The launch switches and environment, the DPR
    (``device_scale_factor``, a property or a knob attribute) and the plan
    have no safe default and must be written by every tier.
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

    @property
    def needs_client_hints(self) -> bool:
        return False

    @property
    def needs_color_scheme(self) -> bool:
        return False

    def launch_args(self) -> tuple[str, ...]:
        raise NotImplementedError(
            f"replayability tier {self.replayability_id!r} must say which launch switches it owns"
        )

    def launch_env(self) -> Mapping[str, str]:
        raise NotImplementedError(
            f"replayability tier {self.replayability_id!r} must say which launch environment "
            f"it owns"
        )

    def plan(
        self,
        *,
        t0_ms: int,
        browser_user_agent: str,
        run_started_ms: int | None = None,
        client_hints: ClientHints | None = None,
        color_scheme: ColorScheme | None = None,
    ) -> ReplayabilityPlan:
        raise NotImplementedError(
            f"replayability tier {self.replayability_id!r} must build a ReplayabilityPlan"
        )


def default_replayability() -> ReplayabilityProtocol:
    """The tier used when no configuration named one: ``none``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built capability.
    """
    from spectragent.core.capabilities.browser.replay.none import NoneReplayability

    return NoneReplayability()
