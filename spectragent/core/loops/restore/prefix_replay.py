"""``prefix_replay`` — the v1 restorer: re-execute the recorded prefix, verifying each step.

For each step ``k`` before the checkpoint *t* (``docs/architecture_design.md``
§4.1 "恢复器"): one ``env.observe`` (one cycle, as the source run took) →
``env.verify_restoration(recorded obs_k, live obs_k)`` → for each action the
source executed at ``k``: ``env.retarget`` → ``act_fn`` → compare the outcome
with the recorded one. Then one more observe, and the checkpoint step's
observation is verified the same way. Each step is handed back as it is
restored.

Knobs:

- ``verify`` — ``strict`` (default): the first failed check raises
  :class:`~.context.RestorationDivergedError`, before any further action runs
  (a retarget that found no single target is a failed check, and its action
  is not executed; an action whose outcome differs from the recorded one, or
  that raised, is one too, and the step's later actions are not executed);
  ``warn``: failed checks are recorded on the step and the restoration goes
  on (an action whose retarget failed runs as recorded);
- ``screenshot_max_distance`` — the largest Hamming distance between the
  recorded and restored screenshots' 64-bit perceptual hashes that still
  passes (default 5);
- ``pace`` — ``none`` (default) or ``recorded_llm_latency``: wait, before
  executing step ``k``'s actions, as long as the source run waited for its
  model at step ``k``, so asynchronous loading settles as it did then.
"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from spectragent.core.loops.errors import RunAbort
from spectragent.core.loops.restore._registrar import RestorerBase
from spectragent.core.loops.restore.checks import judge
from spectragent.core.loops.restore.context import (
    RestorationContext,
    RestorationDivergedError,
    RestoredStep,
)
from spectragent.schemas.action import ActionResult
from spectragent.schemas.restoration import (
    RestorationCheck,
    RestorationEvidence,
    RestoredAction,
)


@dataclass
class PrefixReplayRestorer(RestorerBase):
    """Re-execute the recorded prefix with retargeting and per-step verification."""

    __registry_key__ = "prefix_replay"

    verify: Annotated[
        Literal["strict", "warn"],
        Field(
            default="strict",
            description=(
                "'strict': the first failed restoration check ends the run "
                "(RestorationDivergedError) before any further action runs. "
                "'warn': failed checks are recorded on the restored step and "
                "the restoration continues."
            ),
        ),
    ] = "strict"
    screenshot_max_distance: Annotated[
        int,
        Field(
            default=5,
            ge=0,
            le=64,
            description=(
                "Largest Hamming distance between the recorded and restored "
                "screenshots' 64-bit perceptual hashes (dHash) that still passes."
            ),
        ),
    ] = 5
    pace: Annotated[
        Literal["none", "recorded_llm_latency"],
        Field(
            default="none",
            description=(
                "'recorded_llm_latency': before executing a restored step's "
                "actions, wait as long as the source run waited for its model at "
                "that step, so asynchronous loading settles as it did then. "
                "'none': no wait."
            ),
        ),
    ] = "none"

    async def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]:
        """Restore steps ``0 .. t-1``, then hand back step *t*'s verified observation."""
        for step in range(ctx.checkpoint_step):
            recorded_obs = ctx.source.observation(step, ctx.negotiated_obs)
            record = ctx.source.record(step)
            live_obs = await ctx.env.observe(ctx.negotiated_obs)
            evidence = await ctx.env.verify_restoration(recorded_obs, live_obs)
            # A world that already left the recording is not acted on.
            self._raise_if_diverged(step, evidence, [], live_obs)

            if self.pace == "recorded_llm_latency" and record.llm_latency_ms:
                await asyncio.sleep(record.llm_latency_ms / 1000)

            actions: list[RestoredAction] = []
            # Only what the source executed: an action after a failed or a
            # final one never ran there, so it does not run here either.
            for recorded, recorded_result in zip(
                record.actions, record.action_results, strict=False
            ):
                retarget = await ctx.env.retarget(recorded, recorded_obs, live_obs)
                if retarget.failure is not None and self.verify == "strict":
                    actions.append(
                        RestoredAction(
                            recorded=recorded,
                            executed=None,
                            method=retarget.method,
                            failure=retarget.failure,
                            detail=retarget.detail,
                            recorded_success=recorded_result.success,
                        )
                    )
                    self._raise_if_diverged(step, evidence, actions, live_obs)
                # Under ``warn`` an action no target was found for runs as
                # recorded, and the outcome comparison still applies.
                executed = (
                    retarget.action
                    if retarget.action is not None
                    else recorded.model_copy(deep=True)
                )
                act_error: Exception | None = None
                result: ActionResult | None = None
                try:
                    result = await ctx.act_fn(executed)
                except Exception as exc:  # noqa: PERF203 — per-action attribution
                    act_error = exc
                if isinstance(act_error, RunAbort):
                    raise act_error
                actions.append(
                    RestoredAction(
                        recorded=recorded,
                        executed=executed,
                        method=retarget.method,
                        failure=retarget.failure,
                        detail=retarget.detail,
                        recorded_success=recorded_result.success,
                        live_success=result.success if result is not None else False,
                        live_error=(
                            f"{type(act_error).__name__}: {act_error}"
                            if act_error is not None
                            else None
                        ),
                    )
                )
                # An outcome that differs from the recording is a world that
                # already left it: the step's next action does not run on it.
                self._raise_if_diverged(step, evidence, actions, live_obs)

            check = self._judge(step, evidence, actions)
            if check.verdict == "diverged":
                raise RestorationDivergedError(check, live_obs)
            yield RestoredStep(step=step, check=check, observation=live_obs)

        # The checkpoint: the observation the branch decides on.
        step = ctx.checkpoint_step
        recorded_obs = ctx.source.observation(step, ctx.negotiated_obs)
        live_obs = await ctx.env.observe(ctx.negotiated_obs)
        evidence = await ctx.env.verify_restoration(recorded_obs, live_obs)
        check = self._judge(step, evidence, [])
        if check.verdict == "diverged":
            raise RestorationDivergedError(check, live_obs)
        yield RestoredStep(step=step, check=check, observation=live_obs)

    def _judge(
        self,
        step: int,
        evidence: Sequence[RestorationEvidence],
        actions: Sequence[RestoredAction],
    ) -> RestorationCheck:
        return judge(
            step,
            evidence,
            actions,
            verify=self.verify,
            screenshot_max_distance=self.screenshot_max_distance,
        )

    def _raise_if_diverged(
        self,
        step: int,
        evidence: Sequence[RestorationEvidence],
        actions: Sequence[RestoredAction],
        observation: Any,
    ) -> None:
        """Under ``strict``, stop at a check that already failed — before acting on it."""
        if self.verify != "strict":
            return
        check = self._judge(step, evidence, actions)
        if check.verdict == "diverged":
            raise RestorationDivergedError(check, observation)
