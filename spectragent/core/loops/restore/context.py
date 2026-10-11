"""What a restorer is handed, what it hands back, and how it says the world diverged.

A restorer (``RestorerRegistrar``, ``docs/architecture_design.md`` §4.1
"`BranchLoop`") puts the environment back where a recorded run stood before
its checkpoint step. It is handed a deliberately narrow
:class:`RestorationContext` — the woven environment, ``guarded_act``, the
observation negotiation and typed access to the source trace, and nothing
else: no model, no memory, no recorder. Restoring the *environment* is its
whole job; the conversation is rebuilt by the Loop, and the steps are
recorded by the Loop. A later ``capability_checkpoint`` restorer (a Docker
snapshot, an emulator snapshot) is the same interface with another strategy.

It hands back one :class:`RestoredStep` per step as it goes, so each restored
step is committed as soon as it is restored: a restoration that times out or
crashes half way still leaves the steps before in the trace.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.restoration import RestorationCheck

if TYPE_CHECKING:
    from spectragent.core.loops.restore.source import SourceRun


@dataclass(frozen=True)
class RestorationContext:
    """Everything a restorer may touch — and, by omission, everything it may not.

    Attributes:
        env: The woven environment: ``observe`` / ``retarget`` /
            ``verify_restoration`` (guards and recorders see these calls).
        act_fn: ``guarded_act`` — the only way a restored action executes.
        negotiated_obs: This run's observation negotiation result.
        source: Typed, read-only access to the source run's committed steps.
        checkpoint_step: *t*: steps ``0 .. t-1`` are restored, and step *t*'s
            observation is the one the branch decides on.
    """

    env: Any
    act_fn: Callable[[Action], Awaitable[ActionResult]]
    negotiated_obs: Any
    source: SourceRun
    checkpoint_step: int


@dataclass(frozen=True)
class RestoredStep:
    """One restored step, as the restorer hands it back.

    Attributes:
        step: The step number. ``step < t``: a restored step whose recorded
            actions were re-executed; ``step == t``: the checkpoint, whose
            observation the branch's first policy step decides on.
        check: How that step's restoration verified.
        observation: The live observation the step started from (env-source
            fragments only) — what the Loop surrenders with the step's record.
    """

    step: int
    check: RestorationCheck
    observation: Any


class RestorationDivergedError(Exception):
    """The restored environment left the recording, under ``verify: strict``.

    Raised by a restorer at the step that failed, before any further action
    runs. The Loop records a restored step carrying :attr:`check` with
    ``error_phase="restore"`` and ends the run (``agent_done=error``,
    ``env_status=failed``) — no policy step, no terminal observation.

    Attributes:
        check: The failing check (``verdict="diverged"``).
        observation: The live observation of the step that diverged, or
            ``None`` when there was none to take.
    """

    def __init__(self, check: RestorationCheck, observation: Any = None) -> None:
        self.check = check
        self.observation = observation
        reasons = ", ".join(check.reasons) or "unknown"
        super().__init__(f"restoration diverged at step {check.step}: {reasons}")
