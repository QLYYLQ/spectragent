"""Resume a ReAct checkpoint with source or actually restored Agent history.

The Loop owns sequencing; Agents render history through public slots. Every
restored record describes this run's execution, and source replies retain an
explicit provenance instead of masquerading as new policy output.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from spectragent.core.action_guard import executed_name
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.loops._loop_fragments import (
    loop_fragment_builders,
    pinned_step_info,
    populate_loop_fragments,
)
from spectragent.core.loops._registrar import LoopInput
from spectragent.core.loops.coordinate_frames import actions_in_capability_units, step_frame
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.core.loops.react import ReactLoop, ReactRun, serialize_llm_output
from spectragent.core.loops.restore import (
    RestorationContext,
    RestorationDivergedError,
    RestoredStep,
    Restorer,
    SourceRun,
    default_restorer,
)
from spectragent.core.loops.restore.prefix_replay import (  # noqa: F401 — registration
    PrefixReplayRestorer,
)
from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.core.trace.models import RecordedChatCall, TraceNegotiation
from spectragent.core.trace.reconstruction import first_difference, recorded_form
from spectragent.core.trace.redaction import OMITTED
from spectragent.core.trace.sources import not_closed_reason, past_complete_reason
from spectragent.schemas.action import Action
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory
from spectragent.schemas.restoration import Checkpoint, RestorationCheck
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: The parts of ``TraceConfig.negotiation`` a branch must reproduce: what the
#: agent observed and could do. The archive parts are the branch's own choice
#: (the user's ruling of 2026-10-09, AGENTS.md §2.5) and are not compared.
NEGOTIATION_FIELDS = (
    "active_fragments",
    "required_fragments",
    "env_sourced_fragments",
    "loop_sourced_fragments",
    "fragment_bindings",
    "active_actions",
    "action_bindings",
)
#: The source loop's settings a branch must share, picked one by one. The two
#: loop blocks differ by design (``loop_id``, ``max_steps``,
#: ``terminal_observation``); how many actions one reply may carry does not —
#: it is in the seeded prompt and decides what the replay drops.
COMPARED_LOOP_SETTINGS = ("max_actions_per_step",)


@dataclass(kw_only=True)
class BranchRun(ReactRun):
    """A branch run's books: a :class:`LoopRun` plus its source.

    The replay learns the name the guard gave each action it was handed —
    the name the source's record carries — from this run's action space
    (:func:`~spectragent.core.action_guard.executed_name` over
    ``action_space``). This run's guard stands in for the source's:
    ``weave`` proved the negotiation equal, and the seed check the seeded
    prompt, which carries the action table the guard was built from (for an
    agent that renders one).

    Attributes:
        source: The checkpoint's source run.
        step_info: The ``loop.step_info`` the source recorded at the
            checkpoint step, which pins what the policy steps are told about
            the step budget and the date (``None`` when the source recorded
            none).
    """

    source: SourceRun
    is_branch: bool = True
    restoration_iterator: AsyncIterator[RestoredStep] | None = None
    checkpoint_observation: Any = None
    checkpoint_check: RestorationCheck | None = None
    step_info: StepInfoObs | None = None

    @property
    def checkpoint_step(self) -> int:
        return self.source.checkpoint.step


class _BranchLoopReceptors:
    """Annotation-only declaration of ``BranchLoop``'s wired receptor.

    On a plain mixin, as ``AgentReceptors`` does it for agents: a dataclass
    field would become a parameter of the synthesised ``__init__`` and
    conscribe's ``WiredField`` would take its default's place
    (``af-add-a-receptor`` §6). The Composer injects the configured
    restorer; ``BranchLoop.__post_init__`` supplies one for a loop built by
    hand.
    """

    restorer: Restorer


@dataclass
class BranchLoop(_BranchLoopReceptors, ReactLoop):
    """Resume a recorded run at a checkpoint: restore, decide step *t* again, continue.

    The agent slots are ReAct's (inherited). Configuration fields, beyond
    ``ReactLoop``'s:

    - ``continue_steps``: ReAct steps after the policy step (0: decide step
      *t* only — a configuration value, not a special case);
    - ``terminal_observation``: on by default — a branch's outcome is judged
      on the state its last action left;
    - ``history_source``: exact source conversation or actual restored history;
    - ``restorer``: how the environment is restored (``prefix_replay``).
    """

    __registry_key__ = "branch"
    __wiring__: ClassVar[dict[str, Any]] = {"restorer": ("restorer", ["prefix_replay"])}
    __loop_inputs__ = {
        "checkpoint": LoopInput(
            Checkpoint, description="The source run's directory and the step to branch at."
        )
    }

    continue_steps: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description=(
                "ReAct steps to run after the policy step at the checkpoint. 0 "
                "decides the checkpoint step only."
            ),
        ),
    ] = 0
    terminal_observation: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "After the branch ends (its step budget, done, or the last "
                "tolerated parse error), observe the environment once more and "
                "record it as an origin='terminal' step."
            ),
        ),
    ] = True
    history_source: Annotated[
        Literal["source", "restored"],
        Field(
            default="source",
            description=(
                "History before the checkpoint: exact source conversation, or actual "
                "restored observations, retargeted actions and results. Independent of "
                "the restorer's strict/warn environment verification. A branch under a "
                "task variant (trace_branches task_variants) needs 'restored'."
            ),
        ),
    ] = "source"

    def __post_init__(self) -> None:
        # The default for a hand-built loop; the Composer's injection, which
        # happens after construction, replaces it with the configured one.
        self.restorer = default_restorer()

    # ── assembly: is this checkpoint one this assembly can branch from? ──

    def weave(self, agent: Any, **deps: Any) -> Any:
        """Check the checkpoint against this assembly, then weave like ``ReactLoop``.

        Raises:
            IncompatibleError: The agent lacks a ReAct slot.
            IncompatibleLoopInputError: No ``checkpoint`` (or an unknown input).
            IncompatibleCheckpointError: The source cannot be branched by
                this assembly — every problem at once.
        """
        self.validate_slots(agent)
        history_builder = getattr(agent, "build_restored_history", None)
        if self.history_source == "restored" and (
            not callable(history_builder) or not inspect.iscoroutinefunction(history_builder)
        ):
            from spectragent.core.loops.validation import IncompatibleError

            raise IncompatibleError(
                getattr(agent, "agent_id", type(agent).__name__),
                self.loop_id,
                ["build_restored_history"],
                needed_for="restored branch history",
            )
        if self.history_source == "restored":
            assert callable(history_builder)
            try:
                signature = inspect.signature(history_builder)
                for name in ("history", "context"):
                    parameter = signature.parameters.get(name)
                    if parameter is None or parameter.kind != inspect.Parameter.KEYWORD_ONLY:
                        raise TypeError(f"{name} must be an explicit keyword-only parameter")
                signature.bind(None, history=None, context=None)
            except TypeError as exc:
                raise TypeError(
                    "build_restored_history must accept observation, history= and context="
                ) from exc
        inputs = deps.get("loop_inputs") or {}
        self.check_loop_inputs(inputs)
        source = SourceRun.open(inputs["checkpoint"])
        self._check_checkpoint(source, agent, deps)
        return super().weave(agent, **deps, branch_source=source)

    def _check_checkpoint(self, source: SourceRun, agent: Any, deps: dict[str, Any]) -> None:
        """Refuse a source this assembly cannot resume, naming every problem."""
        manifest = source.manifest
        t = source.checkpoint.step
        problems: list[str] = []
        # The trace's own ledger is a hard condition however the checkpoint
        # was chosen (``trace_branches`` never offers such a step): closed,
        # and complete through the checkpoint.
        unclosed = not_closed_reason(manifest)
        if unclosed is not None:
            problems.append(unclosed)
        committed = source.committed_steps()
        gaps = [k for k in range(t + 1) if k not in committed]
        if gaps:
            problems.append(f"steps {gaps} are not committed (the checkpoint is step {t})")
        else:
            incomplete = past_complete_reason(manifest, t)
            if incomplete is not None:
                problems.append(incomplete)
            else:
                record = source.record(t)
                if record.origin != "policy":
                    problems.append(f"step {t} is a {record.origin!r} step, not a policy step")
                if record.error_phase == "observe":
                    problems.append(f"step {t} recorded no observation (its observe failed)")
        config = source.config
        # The prefix's records read back only when every step up to t is
        # committed and complete; otherwise the problems above already say so.
        prefix_readable = not problems
        if self.history_source == "restored" and prefix_readable:
            for step in range(t):
                record = source.record(step)
                if len(record.action_results) > len(record.actions) or (
                    len(record.action_results) < len(record.actions)
                    and all(
                        result.success and not result.is_done for result in record.action_results
                    )
                ):
                    problems.append(f"step {step} lacks necessary executed action results")
        if prefix_readable:
            # What the configured restorer needs from the source (a pace that
            # reads recorded timings, say), refused here with the rest.
            problems.extend(self.restorer.source_problems(source))
        if config.loop_id != "react":
            problems.append(
                f"the source ran loop {config.loop_id!r}; a branch replays ReAct's memory protocol"
            )
        if config.branch is not None:
            problems.append(
                "the source is itself a branch run (its prefix made no model calls to replay); "
                f"branch from its source run {config.branch.source_run_id!r} instead"
            )
        variant = source.checkpoint.task_variant
        if variant is not None and self.history_source == "source":
            # Decided by configuration and task data, so refused here, before
            # setup: the seed check would only fail once the run had started.
            problems.append(
                f"the checkpoint decides under task variant {variant!r} (the trace_branches "
                "loader's task_variants), but history_source: source replays the source's "
                "exact prompts, which carry the source's task; set the branch loop's "
                "history_source: restored"
            )

        mismatches: dict[str, tuple[Any, Any]] = {}
        agent_id = getattr(agent, "agent_id", "")
        if config.agent_id != agent_id:
            mismatches["agent_id"] = (config.agent_id, agent_id)
        else:
            entries = [
                entry
                for entry in config.resolved.get("agent", [])
                if isinstance(entry, dict) and entry.get("agent_id") == agent_id
            ]
            if len(entries) != 1:
                problems.append(
                    f"the source configuration records {len(entries)} agent entries for "
                    f"{agent_id!r}; its settings cannot be compared with this agent's"
                )
            else:
                mismatches.update(self._setting_mismatches("agent", entries[0], agent))
                memory_entry = entries[0].get("memory")
                if isinstance(memory_entry, dict):
                    mismatches.update(
                        self._setting_mismatches("memory", memory_entry, deps.get("memory"))
                    )
                mismatches.update(self._loop_mismatches(entries[0].get("agent_loop")))
        mismatches.update(self._negotiation_mismatches(config.negotiation, deps))
        if config.action_space != deps.get("action_space"):
            mismatches["action_space"] = (config.action_space, deps.get("action_space"))
        if problems or mismatches:
            raise IncompatibleCheckpointError(source.name, problems, mismatches=mismatches)

    @staticmethod
    def _setting_mismatches(
        prefix: str, recorded: dict[str, Any], owner: Any
    ) -> dict[str, tuple[Any, Any]]:
        """Recorded scalar settings *owner* does not share.

        Closed-set values and numbers are recorded raw, so they are compared
        here — ``coordinate_space``, a memory's ``max_messages``. A value
        written as ``"<omitted>"`` (a free-form string nobody marked
        trace-safe) cannot be, and nested blocks (the loop, the model) are
        another component's: of the loop's settings, the curated ones are
        compared by :meth:`_loop_mismatches`. A name *owner* does not have is
        not compared. The ``<omitted>`` ones a prompt depends on are caught
        at run time instead: the seeded system prompt and every replayed
        context must match the source's.
        """
        found: dict[str, tuple[Any, Any]] = {}
        for key, value in recorded.items():
            if key == "id" or value == OMITTED or isinstance(value, dict | list):
                continue
            if not hasattr(owner, key):
                continue
            current = getattr(owner, key)
            if callable(current):
                continue
            if isinstance(current, Enum):
                current = current.value
            if current != value:
                found[f"{prefix}.{key}"] = (value, current)
        return found

    def _loop_mismatches(self, recorded: Any) -> dict[str, tuple[Any, Any]]:
        """The :data:`COMPARED_LOOP_SETTINGS` this loop does not share with the source's loop.

        *recorded* is the source agent entry's ``agent_loop`` block. A setting
        it does not record as a value (absent, ``null``, or ``"<omitted>"`` in
        a trace written before numbers were recorded raw) is left to the
        seed check at run time.
        """
        found: dict[str, tuple[Any, Any]] = {}
        if not isinstance(recorded, dict):
            return found
        for name in COMPARED_LOOP_SETTINGS:
            value = recorded.get(name)
            if value is None or value == OMITTED:
                continue
            current = getattr(self, name)
            if current != value:
                found[f"agent_loop.{name}"] = (value, current)
        return found

    @staticmethod
    def _negotiation_mismatches(
        recorded: TraceNegotiation | None, deps: dict[str, Any]
    ) -> dict[str, tuple[Any, Any]]:
        """What this run negotiated differently from the source (archive axis excluded)."""
        current = TraceNegotiation.from_results(
            observation=deps.get("negotiated_obs"), action=deps.get("negotiated_actions")
        )
        found: dict[str, tuple[Any, Any]] = {}
        for name in NEGOTIATION_FIELDS:
            mine = getattr(current, name) if current is not None else None
            theirs = getattr(recorded, name) if recorded is not None else None
            if mine != theirs:
                found[f"negotiation.{name}"] = (theirs, mine)
        return found

    # ── run time ─────────────────────────────────────────────

    async def _execute(
        self,
        agent: Any,
        task: str,
        *,
        ctx: dict[str, Any],
        **kwargs: Any,
    ) -> AgentResult:
        """Rebuild the context, restore the environment, decide step *t*, continue."""
        source: SourceRun = ctx["branch_source"]
        t = source.checkpoint.step
        run = BranchRun.from_ctx(
            agent,
            task,
            ctx,
            source=source,
            step_executor=ctx.get("step_executor", self),
            step_info=source.step_info(t),
        )
        if self.history_source == "source":
            self._check_seed(run)
            await self._replay_prefix(run)

        restored = await self._restore(run)
        if isinstance(restored, AgentResult):
            return restored
        observation, check = restored

        budget = min(1 + self.continue_steps, self.max_steps)
        for offset in range(budget):
            result = await run.step_executor.execute_step(
                run,
                t + offset,
                observation=observation if offset == 0 else None,
                restoration=check if offset == 0 else None,
            )
            if result is not None:
                return result
        return await self.with_terminal(
            run,
            self.build_result(
                run, agent_done="max_steps", env_status="success", answer=run.last_answer
            ),
        )

    # 1. the seed

    def _check_seed(self, run: BranchRun) -> None:
        """The seeded messages must open the source's first recorded prompt.

        The runtime has already written ``agent.prepare``'s seed — the
        system prompt carries the task and the action table, which the trace
        does not record as settings (``max_actions_per_step``, which it also
        carries, ``weave`` has compared already). A seed that differs means
        this run's agent would not have decided the source's steps the way
        the source did.
        """
        seed = recorded_form(run.memory.get_context_messages())
        calls = run.source.calls(0)
        if not calls:
            raise IncompatibleCheckpointError(
                run.source.name, ["step 0 made no model call to compare the seeded prompt with"]
            )
        recorded = recorded_form(calls[0].messages)[: len(seed)]
        if seed != recorded:
            raise IncompatibleCheckpointError(
                run.source.name,
                [
                    "the seeded prompt differs from the source's first recorded prompt: "
                    + first_difference(seed, recorded)
                ],
            )

    # 2. the conversation, from the source trace

    async def _replay_prefix(self, run: BranchRun) -> None:
        """Rebuild memory and agent state for steps ``0 .. t-1`` from the source trace."""
        for step in range(run.checkpoint_step):
            await self._replay_step(run, step)

    async def _replay_step(self, run: BranchRun, step: int) -> None:
        source = run.source
        agent = run.agent
        memory = run.memory
        observation = source.observation(step, run.negotiated_obs)
        try:
            record = source.record(step)
            # The frame the source step recorded: the prompt is rebuilt, and
            # the reply read back, on the grid the source model was shown.
            new_msgs = await agent.build_react_prompt(
                observation,
                context=ReactPromptContext(
                    step=step,
                    phase="history",
                    is_branch=True,
                    contributions=list(record.prompt_contributions),
                    source_run_id=source.manifest.run_id,
                    source_reply=record.llm_output,
                    frame=record.frame,
                ),
            )
        except RunAbort as veto:
            await self._note_replay_veto(run, step, veto)
            raise
        memory.add_messages(new_msgs)

        calls = source.calls(step)
        if not calls:
            raise IncompatibleCheckpointError(
                source.name, [f"step {step} made no model call to replay"]
            )
        output_model = (
            agent.get_react_output_model() if hasattr(agent, "get_react_output_model") else None
        )
        parse_failure: ParseError | None = None
        actions: list[Action] = []
        output_text = ""
        for index, call in enumerate(calls):
            if parse_failure is None and index > 0:
                raise IncompatibleCheckpointError(
                    source.name,
                    [
                        f"step {step}: this agent parses reply {index - 1}, but the source "
                        "asked again"
                    ],
                )
            context = recorded_form(memory.get_context_messages())
            prompt = recorded_form(call.messages)
            if context != prompt:
                raise IncompatibleCheckpointError(
                    source.name,
                    [
                        f"step {step} call {index}: the rebuilt context is not the recorded "
                        f"prompt — {first_difference(context, prompt)}"
                    ],
                )
            reply = self._recorded_reply(source, call, output_model, step, index)
            output_text = serialize_llm_output(reply)
            try:
                # Read back off the frame the source step recorded, as the
                # live step read it.
                actions = actions_in_capability_units(
                    agent.parse_react_output(reply),
                    record.frame,
                    run.action_space,
                    raw_output=output_text[:500],
                )
                # The same check the live step made before running anything:
                # a reply it sent back (an unknown name, params that do not
                # fit) is read back as the parse failure it was.
                self.check_action_params(
                    run, actions[: self.max_actions_per_step], raw_output=output_text
                )
            except RunAbort as veto:
                await self._note_replay_veto(run, step, veto, llm_output=output_text)
                raise
            except ParseError as exc:
                parse_failure = exc
                self.remember_rejected(memory, output_text, exc)
                continue
            parse_failure = None

        record = source.record(step)
        if parse_failure is not None:
            run.parse_error_streak += 1
            kept: list[Action] = []
            dropped: list[str] = []
        else:
            run.parse_error_streak = 0
            kept, dropped = self.cap_actions(actions, step)
            self.remember_reply(memory, output_text, dropped)
        self._check_replayed_step(run, step, record, kept, dropped, output_text)

    def _recorded_reply(
        self,
        source: SourceRun,
        call: RecordedChatCall,
        output_model: Any,
        step: int,
        index: int,
    ) -> Any:
        """The reply the live loop handed the parser for this recorded call.

        A text call's recorded text; a structured call's reply rebuilt into
        the agent's own reply model (the trace keeps its JSON form); a
        structured reply the provider refused, as the text the model wrote
        (``raw_output``) — which the live loop parsed too. Anything else a
        prefix step cannot contain: such a call ended the source run.
        """
        if call.status == "completed" and call.completion is not None:
            reply = call.completion.completion
            if output_model is None:
                return reply
            try:
                return output_model.model_validate(reply)
            except ValueError as exc:
                raise IncompatibleCheckpointError(
                    source.name,
                    [
                        f"step {step} call {index}: the recorded reply does not validate as "
                        f"{getattr(output_model, '__name__', output_model)!r}: {exc}"
                    ],
                ) from exc
        failure = call.failure
        if (
            call.status == "failed"
            and failure is not None
            and failure.error == StructuredOutputValidationError.__name__
            and failure.raw_output is not None
        ):
            return failure.raw_output
        what = failure.error if failure is not None else "no completion"
        raise IncompatibleCheckpointError(
            source.name,
            [f"step {step} call {index} ended {call.status} ({what}); it cannot be replayed"],
        )

    def _check_replayed_step(
        self,
        run: BranchRun,
        step: int,
        record: StepRecord,
        kept: list[Action],
        dropped: list[str],
        output_text: str,
    ) -> None:
        """The replayed step must be the recorded one: actions, drops, reply."""
        # The parser names actions as the agent does; the record names them
        # as ``guarded_act`` left them — qualified in place when it resolved
        # one, as the agent wrote it when it refused one. The guard's own
        # rule says which (``executed_name``).
        named = [(executed_name(run.action_space, a.name) or a.name, a.params) for a in kept]
        recorded = [(a.name, a.params) for a in record.actions]
        mismatches: dict[str, tuple[Any, Any]] = {}
        if named != recorded:
            mismatches[f"step {step} actions"] = (recorded, named)
        if len(dropped) != record.truncated_actions:
            mismatches[f"step {step} truncated_actions"] = (record.truncated_actions, len(dropped))
        if output_text != record.llm_output:
            mismatches[f"step {step} reply"] = (record.llm_output, output_text)
        if mismatches:
            raise IncompatibleCheckpointError(
                run.source.name,
                ["this agent does not reproduce the recorded step"],
                mismatches=mismatches,
            )

    async def _note_replay_veto(
        self, run: BranchRun, step: int, veto: RunAbort, *, llm_output: str = ""
    ) -> None:
        """A guard woven onto an agent slot vetoed the replay: record that, then let it fly."""
        logger.warning("BranchLoop: run aborted while replaying step %d: %s", step, veto)
        record = StepRecord(
            step=step,
            origin="restored",
            source_history=RestoredHistory(
                source_run_id=run.source.manifest.run_id,
                source_step=step,
                source_reply=llm_output or run.source.record(step).llm_output,
                source_actions=run.source.record(step).actions,
            ),
            timestamp=time.time(),
            error_phase="replay",
            error=str(veto),
        )
        run.step_records.append(record)
        await self.surrender_vetoed(run.recorder, record, None, veto)

    # 3. the environment

    async def execute_step(
        self,
        run: ReactRun,
        step_idx: int,
        *,
        observation: Any = None,
        restoration: RestorationCheck | None = None,
    ) -> AgentResult | None:
        """Execute one restoration or policy step through the same public boundary."""
        if isinstance(run, BranchRun) and run.restoration_iterator is not None:
            return await self._restore_step(run, step_idx)
        return await super().execute_step(
            run, step_idx, observation=observation, restoration=restoration
        )

    async def _restore(self, run: BranchRun) -> tuple[Any, RestorationCheck] | AgentResult:
        """Drive each restoration step through the run's woven execution surface.

        The checkpoint capture starts step t; the later policy invocation continues
        that same step and surrenders its only record. No separate checkpoint step
        or observation is introduced.
        """
        context = RestorationContext(
            env=run.env,
            act_fn=run.act_fn,
            negotiated_obs=run.negotiated_obs,
            source=run.source,
            checkpoint_step=run.checkpoint_step,
        )
        async with aclosing(self.restorer.restore(context)) as restoring:
            run.restoration_iterator = restoring
            try:
                for step in range(run.checkpoint_step + 1):
                    result = await run.step_executor.execute_step(run, step)
                    if result is not None:
                        return result
            finally:
                run.restoration_iterator = None
        if run.checkpoint_check is None:
            raise IncompatibleCheckpointError(
                run.source.name,
                [f"restorer {self.restorer.restorer_id!r} ended without the checkpoint step"],
            )
        return run.checkpoint_observation, run.checkpoint_check

    async def _restore_step(self, run: BranchRun, step: int) -> AgentResult | None:
        """One actual recovery and its Agent-owned history, ending at step handover."""
        assert run.restoration_iterator is not None
        try:
            restored = await anext(run.restoration_iterator)
        except RestorationDivergedError as exc:
            record = self._restore_failure(
                run, exc.check.step, str(exc), exc.check, exc.observation
            )
            return await self.end_step(
                run,
                record,
                exc.observation,
                error=f"RestorationDivergedError: {exc}",
                agent_done="error",
                env_status="failed",
                answer=run.last_answer,
            )
        except RunAbort as veto:
            logger.warning("BranchLoop: run aborted while restoring step %d: %s", step, veto)
            record = self._restore_failure(run, step, str(veto), None)
            await self.surrender_vetoed(run.recorder, record, None, veto)
            raise
        except asyncio.CancelledError:
            record = self._restore_failure(run, step, "Restoration cancelled", None)
            await self.surrender(run.recorder, record, None)
            raise
        except Exception as exc:
            logger.exception("BranchLoop: restoring step %d failed", step)
            record = self._restore_failure(run, step, f"{type(exc).__name__}: {exc}", None)
            return await self.end_step(
                run,
                record,
                None,
                error=f"restore failed at step {step}: {type(exc).__name__}: {exc}",
                agent_done="error",
                env_status="failed",
                answer=run.last_answer,
            )
        if restored.step != step or restored.check.step != restored.step:
            raise IncompatibleCheckpointError(
                run.source.name,
                [
                    f"restorer returned step {restored.step} with check {restored.check.step}; expected {step}"
                ],
            )
        if step == run.checkpoint_step:
            run.checkpoint_observation = restored.observation
            run.checkpoint_check = restored.check
            return None
        record = self._restored_record(
            run, step, restored.check, restored.observation, restored.skipped
        )
        if self.history_source == "restored":
            try:
                await self._append_restored_history(run, record, restored.observation)
            except RunAbort as veto:
                record.error_phase, record.error = "restored_history", str(veto)
                run.step_records.append(record)
                await self.surrender_vetoed(run.recorder, record, restored.observation, veto)
                raise
            except asyncio.CancelledError:
                record.error_phase, record.error = "restored_history", "History rendering cancelled"
                run.step_records.append(record)
                await self.surrender(run.recorder, record, restored.observation)
                raise
            except Exception as exc:
                record.error_phase = "restored_history"
                record.error = f"{type(exc).__name__}: {exc}"
                run.step_records.append(record)
                return await self.end_step(
                    run,
                    record,
                    restored.observation,
                    error=record.error,
                    agent_done="error",
                    env_status="failed",
                    answer=run.last_answer,
                )
        run.step_records.append(record)
        run.prev_step_record = record
        await self.surrender(run.recorder, record, restored.observation)
        return None

    def _restored_record(
        self,
        run: BranchRun,
        step: int,
        check: RestorationCheck,
        observation: Any,
        skipped: Sequence[Action] = (),
    ) -> StepRecord:
        """Actual restored execution; the original reply is only source material.

        *skipped* are the source step's actions its Loop skipped after a failed
        one, which this restoration did not execute either, as the restorer
        retargeted them onto this page (``RestoredStep.skipped``): they follow
        the executed actions as the record's skipped tail, so a later prompt
        says they were not executed just as the source's history says it.
        """
        source_record = run.source.record(step)
        # These parallel lists contain only returned results and their exact
        # action. Attempts that raised remain in check.actions; compacting
        # only the result side would pair a later result with the wrong target.
        returned = [
            item for item in check.actions if item.executed is not None and item.result is not None
        ]
        actions = [item.executed for item in returned if item.executed is not None]
        results = [item.result for item in returned if item.result is not None]
        history = RestoredHistory(
            source_run_id=run.source.manifest.run_id,
            source_step=step,
            source_reply=source_record.llm_output,
            source_actions=source_record.actions[: len(source_record.action_results)],
            actions=actions,
            results=results,
            executions=list(check.actions),
        )
        for result in results:
            if result.extracted_content:
                run.last_answer = result.extracted_content
        return StepRecord(
            step=step,
            origin="restored",
            restoration=check,
            source_history=history,
            actions=[*actions, *skipped],
            action_results=results,
            skipped_actions=len(skipped),
            observation_summary=summarize_observation(observation)
            if observation is not None
            else "",
            timestamp=time.time(),
        )

    async def _append_restored_history(
        self, run: BranchRun, record: StepRecord, observation: Any
    ) -> None:
        """Let the Agent express actual recovery and own its state changes.

        The restored actions ran after *observation* was captured, so the Agent
        is shown them on that observation's frame, which the step's record
        keeps: the next step feeds this one back on the same grid
        (``core/loops/coordinate_frames.py``).
        """
        history = record.source_history
        assert history is not None
        history.validate_execution()
        observation = populate_loop_fragments(
            observation,
            loop_sourced=frozenset(getattr(run.negotiated_obs, "loop_sourced", ())),
            builders=self.loop_fragment_builders(run, record.step),
        )
        record.frame = step_frame(run.action_space, observation)
        context = ReactPromptContext(
            step=record.step,
            phase="history",
            is_branch=True,
            source_run_id=history.source_run_id,
            source_reply=history.source_reply,
            frame=record.frame,
        )
        messages = await run.agent.build_restored_history(
            observation,
            history=history,
            context=context,
        )
        run.memory.add_messages(messages)

    def _restore_failure(
        self,
        run: BranchRun,
        step: int,
        error: str,
        check: RestorationCheck | None,
        observation: Any = None,
    ) -> StepRecord:
        """The record of the step a restoration ended on (``error_phase="restore"``).

        Not a copy of the source's step: what ran of it, and how, is in the
        failing check's ``actions``.
        """
        if check is not None:
            record = self._restored_record(run, step, check, observation)
            record.error_phase = "restore"
            record.error = error
        else:
            record = StepRecord(
                step=step,
                origin="restored",
                observation_summary=(
                    summarize_observation(observation) if observation is not None else ""
                ),
                timestamp=time.time(),
                error_phase="restore",
                error=error,
            )
        run.step_records.append(record)
        return record

    def prompt_context(self, run: ReactRun, step_idx: int) -> ReactPromptContext:
        """Expose branch provenance as facts; contribution functions choose their text."""
        context = super().prompt_context(run, step_idx)
        if isinstance(run, BranchRun):
            context.source_run_id = run.source.manifest.run_id
            complete = run.source.manifest.complete_through
            if (
                complete is not None
                and step_idx <= complete
                and step_idx in run.source.committed_steps()
            ):
                context.source_reply = run.source.record(step_idx).llm_output
        return context

    def loop_fragment_builders(self, run: ReactRun, step_idx: int) -> dict[str, Any]:
        """ReAct's builders, with ``loop.step_info`` pinned to the source's.

        The policy step is told the step budget and the date the source
        was told at the checkpoint (``today_override`` still wins,
        ``pinned_step_info``), so the decision is made on the prompt the
        source made it on.
        """
        history = run.step_records
        if isinstance(run, BranchRun) and self.history_source == "source":
            history = [
                run.source.record(record.step) if record.origin == "restored" else record
                for record in run.step_records
            ]
        pinned = run.step_info if isinstance(run, BranchRun) else None
        if pinned is None:
            max_steps, today = self.max_steps, self.today_iso()
        else:
            max_steps, today = pinned_step_info(pinned, today_override=self.today_override)
        return loop_fragment_builders(
            task=run.task,
            step=step_idx,
            max_steps=max_steps,
            today=today,
            history=history,
            prev_step=history[-1] if history else None,
        )
