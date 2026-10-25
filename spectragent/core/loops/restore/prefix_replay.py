"""``prefix_replay`` — the v1 restorer: re-execute the recorded prefix, verifying each step.

For each step ``k`` before the checkpoint *t* (``docs/architecture_design.md``
§4.1 "恢复器"): one ``env.observe`` (one cycle, as the source run took) →
``env.verify_restoration(recorded obs_k, live obs_k)`` → for each action the
source executed at ``k``: ``env.retarget`` → ``act_fn`` → compare the outcome
with the recorded one. Then one more observe, and the checkpoint step's
observation is verified the same way. Each step is handed back as it is
restored.

Only what the source executed runs: an action its Loop skipped after a failed
one (``StepRecord.skipped_actions``), or one after a ``done`` or a raise, never
ran there and does not run here. A step recorded before the Loop stopped at a
failed action ran every action after one, and they all run again here. When a
restoration reproduces a skipped tail (it ran all the source ran and the last
one failed here too), the skipped actions are retargeted onto the restored page
without running and handed back on the step (``RestoredStep.skipped``), so the
restored record says they were not executed, as the source's does.

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
- ``pace`` — how long to wait between restored steps (``docs/architecture_design.md``
  §4.1 "恢复节奏"). ``none`` (default): no wait. ``recorded_llm_latency``:
  wait, before executing step ``k``'s actions, as long as the source run
  waited for its model at step ``k``, so asynchronous loading settles as it
  did then. ``recorded_timeline``: keep to the source run's timeline from its
  step-0 observation on (see :class:`_RecordedTimeline`): every later
  observation, the checkpoint's included, is started so that it captures
  when the source's did, and every restored step's first action starts when
  the source step's first action started; a restoration already behind
  goes on at once, never hurrying. A source step that executes actions but
  measured no action spans is refused (:meth:`PrefixReplayRestorer.source_problems`).
  Either wait only approximates media playback position; neither restores
  it;
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
import time
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
from spectragent.core.loops.restore.source import SourceRun
from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase, fragment_field_name
from spectragent.schemas.restoration import (
    RestorationCheck,
    RestorationEvidence,
    RestoredAction,
)
from spectragent.schemas.step_record import StepRecord


def _unmeasured_action_starts(source: SourceRun, checkpoint_step: int) -> list[str]:
    """The problem ``pace: recorded_timeline`` has with *source*: restored steps with unknown action starts.

    A restored step that executes actions (``zip(actions, action_results)``
    is not empty) needs the time its first ``env.act`` call started in the
    source run. ``StepRecord.action_spans`` is ``None`` when that was not
    measured — such a step is named; ``[]`` says the source step made no
    ``env.act`` call (every action refused by ``guarded_act``), so there is
    nothing to align and nothing missing.
    """
    unmeasured = [
        step
        for step in range(checkpoint_step)
        if (record := source.record(step)).action_spans is None
        and min(len(record.actions), len(record.action_results)) > 0
    ]
    if not unmeasured:
        return []
    named = (
        f"step {unmeasured[0]}"
        if len(unmeasured) == 1
        else "steps " + ", ".join(str(step) for step in unmeasured)
    )
    return [
        f"{named} recorded no action start times (StepRecord.action_spans was not "
        "measured), which pace: recorded_timeline needs to start each restored step's "
        "first action when the source's started"
    ]


def _capture_time(observation: Any, negotiated: Any) -> float | None:
    """When *observation* was captured: the earliest ``captured_at`` of its env-source fragments.

    The same rule for a recorded and a live observation. A slot whose capture
    failed (``FragmentFailure``) or is empty was not captured, so it has no
    say; ``None`` when no env-source fragment was captured at all.
    """
    times = [
        fragment.captured_at
        for key in negotiated.env_sourced
        if isinstance(
            fragment := getattr(observation, fragment_field_name(key), None), FragmentBase
        )
    ]
    return min(times, default=None)


async def _wait_until(moment: float) -> None:
    """Sleep until the wall-clock *moment*; return at once when it has passed — never hurry."""
    delay = moment - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


@dataclass
class _RecordedTimeline:
    """``pace: recorded_timeline``'s state for one restoration — never kept on the restorer.

    Reproduces the source run's timeline from its step-0 observation on, from
    facts its trace committed (``docs/architecture_design.md`` §4.1 "恢复节奏"):

    - the anchors are the capture times (:func:`_capture_time`) of the
      source's and of this restoration's step-0 observations — how long the
      start page took to load is network speed, not reproduced;
    - observation *k* >= 1 (the checkpoint's included) is aimed at
      ``live_anchor + (source capture k - source_anchor)`` and started
      earlier by *lead*, how long this restoration's previous ``env.observe``
      call took (a monotonic clock around the call);
    - step *k*'s first action is aimed at
      ``live_anchor + (source step k's first ActionSpan.started_at - source_anchor)``.

    Every wait only waits: a moment already past is not caught up on. The
    waits write nothing anywhere.
    """

    source_name: str
    source_anchor: float = 0.0
    live_anchor: float = 0.0
    #: How long this restoration's previous ``env.observe`` call took.
    lead: float = 0.0

    async def observe(self, ctx: RestorationContext, step: int, recorded_obs: Any) -> Any:
        """Wait for step *step*'s place on the timeline, then take its one live observation.

        Raises:
            IncompatibleCheckpointError: The source step's observation has no
                capture time (every env-source fragment failed).
            RuntimeError: This restoration's step-0 observation has none, so
                there is no live anchor.
        """
        source_capture = _capture_time(recorded_obs, ctx.negotiated_obs)
        if source_capture is None:
            raise IncompatibleCheckpointError(
                self.source_name,
                [
                    f"step {step} captured no environment fragment (every one failed), so "
                    "pace: recorded_timeline has no capture time to place its observation on"
                ],
            )
        if step == 0:
            self.source_anchor = source_capture
        else:
            target = self.live_anchor + (source_capture - self.source_anchor)
            await _wait_until(target - self.lead)
        started = time.monotonic()
        live_obs = await ctx.env.observe(ctx.negotiated_obs)
        self.lead = time.monotonic() - started
        if step == 0:
            live_capture = _capture_time(live_obs, ctx.negotiated_obs)
            if live_capture is None:
                raise RuntimeError(
                    "the restored step 0 observation captured no environment fragment (every "
                    "one failed), so pace: recorded_timeline has no live anchor"
                )
            self.live_anchor = live_capture
        return live_obs

    async def before_first_action(self, record: StepRecord) -> None:
        """Wait until the moment the source step's first action started, on this timeline."""
        spans = record.action_spans
        # ``None`` (not measured) was refused before the restoration began;
        # ``[]``: the source step never called ``env.act`` — nothing to align.
        if spans:
            await _wait_until(self.live_anchor + (spans[0].started_at - self.source_anchor))


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
        Literal["none", "recorded_llm_latency", "recorded_timeline"],
        Field(
            default="none",
            description=(
                "'recorded_llm_latency': before executing a restored step's "
                "actions, wait as long as the source run waited for its model at "
                "that step, so asynchronous loading settles as it did then. "
                "'recorded_timeline': keep to the source run's timeline from its "
                "step-0 observation on. An observation's capture time is the "
                "earliest captured_at of its environment fragments, and the two "
                "step-0 captures are the anchors. Every later restored observation, "
                "the checkpoint's included, starts early by as long as this "
                "restoration's previous observation took, so that it captures when "
                "the source's did; every restored step's first action starts when "
                "the source step's first action started (StepRecord.action_spans). "
                "It only waits: a restoration already behind goes on at once. A "
                "source step that executes actions but measured no action spans is "
                "refused before the run. 'none': no wait."
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

    def source_problems(self, source: SourceRun) -> list[str]:
        """Under ``pace: recorded_timeline``, the restored steps whose action start times are unknown."""
        if self.pace != "recorded_timeline":
            return []
        return _unmeasured_action_starts(source, source.checkpoint.step)

    async def restore(self, ctx: RestorationContext) -> AsyncGenerator[RestoredStep, None]:
        """Restore steps ``0 .. t-1``, then hand back step *t*'s verified observation."""
        timeline: _RecordedTimeline | None = None
        if self.pace == "recorded_timeline":
            # ``BranchLoop`` refused such a source at assembly already; the
            # same check here keeps a restorer driven by hand from finding out
            # half way, after it has acted.
            problems = _unmeasured_action_starts(ctx.source, ctx.checkpoint_step)
            if problems:
                raise IncompatibleCheckpointError(ctx.source.name, problems)
            timeline = _RecordedTimeline(source_name=ctx.source.name)
        for step in range(ctx.checkpoint_step):
            recorded_obs = ctx.source.observation(step, ctx.negotiated_obs)
            record = ctx.source.record(step)
            await self._request_alignment(ctx, recorded_obs)
            live_obs = await self._observe(ctx, step, recorded_obs, timeline)
            evidence = await ctx.env.verify_restoration(recorded_obs, live_obs)
            # A world that already left the recording is not acted on.
            self._raise_if_diverged(step, evidence, [], live_obs)

            if self.pace == "recorded_llm_latency" and record.llm_latency_ms:
                await asyncio.sleep(record.llm_latency_ms / 1000)

            actions: list[RestoredAction] = []
            # Only what the source executed: an action after a failed or a
            # final one never ran there, so it does not run here either.
            for index, (recorded, recorded_result) in enumerate(
                zip(record.actions, record.action_results, strict=False)
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
                if timeline is not None and index == 0:
                    # The step's first action only: the rest follow it at once.
                    await timeline.before_first_action(record)
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
            skipped = await self._skipped(ctx, record, actions, recorded_obs, live_obs)
            yield RestoredStep(step=step, check=check, observation=live_obs, skipped=skipped)

        # The checkpoint: the observation the branch decides on.
        step = ctx.checkpoint_step
        recorded_obs = ctx.source.observation(step, ctx.negotiated_obs)
        await self._request_alignment(ctx, recorded_obs)
        live_obs = await self._observe(ctx, step, recorded_obs, timeline)
        evidence = await ctx.env.verify_restoration(recorded_obs, live_obs)
        check = self._judge(step, evidence, [])
        if check.verdict == "diverged":
            raise RestorationDivergedError(check, live_obs)
        yield RestoredStep(step=step, check=check, observation=live_obs)

    async def _skipped(
        self,
        ctx: RestorationContext,
        record: StepRecord,
        actions: Sequence[RestoredAction],
        recorded_obs: Any,
        live_obs: Any,
    ) -> tuple[Action, ...]:
        """The source step's skipped actions on this page, when this restoration skipped them too.

        The source's Loop stopped the step at a failed action and never ran
        the rest (``StepRecord.skipped_actions``); a restoration runs only what
        the source ran, so it never runs them either. When it ran every one of
        those and the last failed here as well, the rest went unexecuted for
        the same reason, and each is retargeted — never run — so the restored
        record names it on this page as it names the executed ones. Otherwise
        nothing: an outcome or an attempt that differed (``verify: warn``), or
        a skipped action with no single target here.
        """
        skipped = record.skipped()
        if not skipped or len(actions) != len(record.action_results):
            return ()
        if any(item.result is None for item in actions):
            return ()
        last = actions[-1].result
        if last is None or last.success:
            return ()
        targets: list[Action] = []
        for recorded in skipped:
            retarget = await ctx.env.retarget(recorded, recorded_obs, live_obs)
            if retarget.action is None:
                return ()
            targets.append(retarget.action)
        return tuple(targets)

    @staticmethod
    async def _observe(
        ctx: RestorationContext,
        step: int,
        recorded_obs: Any,
        timeline: _RecordedTimeline | None,
    ) -> Any:
        """Step *step*'s one live observation — on the source's timeline when there is one."""
        if timeline is None:
            return await ctx.env.observe(ctx.negotiated_obs)
        return await timeline.observe(ctx, step, recorded_obs)

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
