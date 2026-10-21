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
  model at step ``k``, so asynchronous loading settles as it did then. It
  only approximates media playback position; it does not restore it;
- ``media`` — ``align`` (default): before every restored observation,
  the checkpoint's included, ``env.align_next_observation`` asks the
  environment to move matched media elements to the recorded position and
  play state inside that observation's capture (after its settle wait,
  before its screenshot), and the next ``verify_restoration`` reports what
  was done; ``none``: nothing is aligned (the control arm). Nothing is
  requested after the checkpoint's observation;
- ``media_time_tolerance_s`` — how far, in seconds, a media element's
  restored position may lie from the recorded one and still pass (default 1);
- ``media_seek_timeout_s`` — how long each alignment seek, and the play
  after it, may take to complete (default 2); one that does not is undone
  and the step fails ``media_mismatch``;
- ``media_end_lead_s`` — a recorded element that had ended is sought this
  long before its end and played through to the natural end (default 0.5;
  shorter than ``media_seek_timeout_s``).
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
    media: Annotated[
        Literal["none", "align"],
        Field(
            default="align",
            description=(
                "'align': before every restored observation (the checkpoint's "
                "included), the environment moves each media element it can match "
                "to the recorded position and play state, inside that "
                "observation's capture after the settle wait and before the "
                "screenshot; every action and its outcome is recorded with the "
                "step's restoration check, and a seek that does not complete is "
                "undone and fails the step. Nothing is aligned after the "
                "checkpoint. 'none': nothing is aligned; the media state is still "
                "compared."
            ),
        ),
    ] = "align"
    media_time_tolerance_s: Annotated[
        float,
        Field(
            default=1.0,
            ge=0,
            le=60,
            description=(
                "How far, in seconds, a media element's restored playback position "
                "may lie from the recorded one and still pass the restoration check. "
                "A different paused state, a missing or extra element, an ambiguous "
                "match or a live stream fails regardless; an unknown value never "
                "decides."
            ),
        ),
    ] = 1.0
    media_seek_timeout_s: Annotated[
        float,
        Field(
            default=2.0,
            gt=0,
            le=30,
            description=(
                "With media: align, how long each seek, and the play after it for a "
                "media element the recording showed playing, may take to complete "
                "(seeked fired, no longer seeking, a frame to show). One that does not "
                "complete is put back where it was and fails the step."
            ),
        ),
    ] = 2.0
    media_end_lead_s: Annotated[
        float,
        Field(
            default=0.5,
            gt=0,
            le=10,
            description=(
                "With media: align, a media element the recording showed at its end "
                "(ended) is sought this many seconds before the recorded end and played "
                "through to its natural end, so the player shows what it shows at an end; "
                "a seek straight to the end leaves a stale frame and no end screen. The "
                "play to the end must finish within media_seek_timeout_s, so this must be "
                "shorter than that."
            ),
        ),
    ] = 0.5

    def __post_init__(self) -> None:
        if self.media_end_lead_s >= self.media_seek_timeout_s:
            raise ValueError(
                f"media_end_lead_s ({self.media_end_lead_s:g} s) is not shorter than "
                f"media_seek_timeout_s ({self.media_seek_timeout_s:g} s): playing the last "
                "media_end_lead_s seconds to a recorded end could never finish in time. "
                "Shorten the lead or raise the timeout."
            )

    async def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]:
        """Restore steps ``0 .. t-1``, then hand back step *t*'s verified observation."""
        for step in range(ctx.checkpoint_step):
            recorded_obs = ctx.source.observation(step, ctx.negotiated_obs)
            record = ctx.source.record(step)
            await self._request_alignment(ctx, recorded_obs)
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
                        result=result,
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
        await self._request_alignment(ctx, recorded_obs)
        live_obs = await ctx.env.observe(ctx.negotiated_obs)
        evidence = await ctx.env.verify_restoration(recorded_obs, live_obs)
        check = self._judge(step, evidence, [])
        if check.verdict == "diverged":
            raise RestorationDivergedError(check, live_obs)
        yield RestoredStep(step=step, check=check, observation=live_obs)

    async def _request_alignment(self, ctx: RestorationContext, recorded_obs: Any) -> None:
        """Under ``media: align``, have the next observation aligned to *recorded_obs*."""
        if self.media == "align":
            await ctx.env.align_next_observation(
                recorded_obs,
                media_seek_timeout_s=self.media_seek_timeout_s,
                media_end_lead_s=self.media_end_lead_s,
            )

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
            media_time_tolerance_s=self.media_time_tolerance_s,
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
