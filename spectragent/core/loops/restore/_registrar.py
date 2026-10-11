"""The ``restorer`` axis: how a branch run puts the environment back at its checkpoint.

A restorer is a receptor of ``BranchLoop`` (``__wiring__["restorer"]``), so it
is configured nested inside the loop's block and arrives constructed, knobs
and all (``docs/architecture_design.md`` §4.1 "`BranchLoop`", §6). v1 has one,
``prefix_replay`` — re-execute the recorded prefix with retargeting and
per-step verification; the axis exists for the strategies that restore by
snapshot (``capability_checkpoint``: a Docker snapshot, an emulator
snapshot), which restore the same environment through the same interface.

A restorer restores the **environment** only, and is handed no model, no
memory and no recorder (:class:`~.context.RestorationContext`). It yields one
:class:`~.context.RestoredStep` per restored step and finally the checkpoint
step's, raising :class:`~.context.RestorationDivergedError` under ``strict``
when the restored world left the recording.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.loops.restore.context import RestorationContext, RestoredStep


@runtime_checkable
class Restorer(Protocol):
    """A restoration strategy."""

    @property
    def restorer_id(self) -> str: ...

    def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]:
        """Restore the environment to the checkpoint, one step at a time.

        An async generator, so the Loop can close it once it has the
        checkpoint step — or when recording a restored step fails.

        Yields a :class:`RestoredStep` for each step ``k < t`` as soon as it
        is restored and verified, then one for step *t* carrying the
        verified observation the branch decides on. A ``RunAbort`` from the
        environment or ``act_fn`` propagates. ``env.verify_restoration`` is
        called exactly once per restored observation, in order: a
        capability may report a count as the increase since its previous
        call (``RestorationSupport``).

        Raises:
            RestorationDivergedError: Under ``verify: strict``, at the first
                check that failed — the observation's, a retarget's or an
                action's outcome — before any further action runs.
        """
        ...


RestorerRegistrar = create_registrar(
    "restorer",
    Restorer,
    discriminator_field="restorer_id",
)


@dataclass
class RestorerBase(metaclass=RestorerRegistrar.Meta):
    """Base for restorers — subclass to auto-register.

    Each restorer sets ``__registry_key__`` (the YAML-facing ``restorer_id``)
    and declares its knobs as ``Annotated`` dataclass fields. Stateless: one
    instance may restore many runs, so nothing per run is kept on ``self``.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def restorer_id(self) -> str:
        return self.__registry_key__

    def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]:
        raise NotImplementedError(f"restorer {self.restorer_id!r} must implement restore()")


def default_restorer() -> Restorer:
    """The restorer used when no configuration named one: ``prefix_replay``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built ``BranchLoop``.
    """
    from spectragent.core.loops.restore.prefix_replay import PrefixReplayRestorer

    return PrefixReplayRestorer()
