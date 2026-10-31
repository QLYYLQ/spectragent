"""One run's replayability plan: what a tier sends each target and records (§4.14).

A tier (``replayability.py``) builds it per run with :meth:`ReplayabilityProtocol.plan`;
the plan is a recorder participant, so it lives apart from the registrar module, which
must stay importable without the recorder and agent-cdp (``declarations.py``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.capabilities.browser.client_hints import ClientHints
from spectragent.core.capabilities.browser.color_scheme import ColorScheme
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
    #: Epoch milliseconds the shim's clock reads at the run's start (it then
    #: advances in real time); ``None`` without a shim.
    t0_ms: int | None = None
    #: sha256 of the shim *template* (independent of T0 and seed).
    shim_sha256: str | None = None
    #: The launch switches the tier owns (its :meth:`launch_args`).
    replayability_args: tuple[str, ...] = ()
    #: The launch environment the tier owns (its :meth:`launch_env`).
    replayability_env: Mapping[str, str] = field(default_factory=dict)
    #: The client hints the tier's user-agent override carries (the browser's
    #: own, ``client_hints.py``); ``None`` when it carries none.
    client_hints: ClientHints | None = None
    #: The measured recording theme, or the theme inherited from its archive.
    color_scheme: ColorScheme | None = None
    #: Why this plan cannot be honoured in this browser (a Chrome identifier
    #: its own client hints contradict), one line each naming both sides. Not
    #: fingerprinted: setup refuses the run before the recorder prepares any
    #: target.
    problems: tuple[str, ...] = ()
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
        record = {
            "tier": self.tier,
            "params": dict(self.params),
            "t0_ms": self.t0_ms,
            "shim_sha256": self.shim_sha256,
            "replayability_args": list(self.replayability_args),
            "replayability_env": dict(self.replayability_env),
            "client_hints": self.client_hints.as_metadata() if self.client_hints else None,
        }
        if self.color_scheme is not None:
            record["color_scheme"] = self.color_scheme
        return record
