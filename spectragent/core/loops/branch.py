"""BranchLoop — resume a recorded run at a checkpoint and decide that step again.

A branch (``docs/architecture_design.md`` §4.1 "`BranchLoop`") is a recorded
run's environment put back where it stood before step *t*, one policy step on
it, and ``continue_steps`` ordinary ReAct steps after. Its Loop input is a
:class:`~spectragent.schemas.restoration.Checkpoint` (the source run's
directory and *t*); its ``restorer`` receptor decides how the environment is
restored (``prefix_replay`` in v1); its teacher is whatever model this run
wires, sampled however that model is configured.

**At assembly** (``weave``, inside ``AgentRuntime.prepare()``, before
``on_run_start`` and any environment setup) the source trace is opened and
checked against this assembly: a source the branch cannot resume, or one an
agent configured differently recorded, raises
:class:`~spectragent.core.loops.validation.IncompatibleCheckpointError` naming
the source and both values.

**At run time** (``_execute``):

1. the seeded memory (the runtime wrote ``agent.prepare``'s seed messages)
   must equal the start of the source's first recorded prompt;
2. the conversation is rebuilt **from the source trace** (PRD revision F1):
   for each ``k < t`` the recorded observation is rendered by the agent's own
   ``build_react_prompt`` and every recorded model call of the step is
   replayed through ``parse_react_output`` and ReAct's memory protocol
   (``ReactLoop._remember_rejected`` / ``_cap_actions`` / ``_remember_reply``
   — the same methods the live step uses). The replay checks itself: before
   each call the context must equal the recorded prompt, and after a step's
   last call the actions, the dropped count and the reply must equal the
   source's record. All of this happens before any action runs;
3. the restorer restores the environment, handing back one step at a time;
   each restored step is recorded (``origin="restored"``) as it arrives;
4. the policy step decides on the restored, verified observation of step
   *t* (``ReactLoop._run_step``); ``continue_steps`` more steps may follow;
5. at the step budget's end the run ends with ``agent_done=max_steps``, and
   the terminal observation is recorded (``terminal_observation`` is on by
   default here).

The Loop stores nothing per run on itself: the source travels in the weave
deps (``ctx["branch_source"]``), the run's state in a :class:`BranchRun`.

``from __future__ import annotations``: conscribe writes a ``.pyi`` stub next
to a class with an injected receptor (``restorer``) and renders evaluated
``Annotated[..., Field(...)]`` parameters as ``FieldInfo(...)`` reprs — an
invalid stub. As strings they are copied verbatim, like ``ReactLoop``'s. The
strings — this module's and the ones ``BranchLoop`` inherits from
``react.py`` — resolve in this module's globals (the dataclass-synthesised
``__init__`` carries them), which is why ``Annotated`` and ``Field`` are
imported here under exactly those names.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.action_guard import resolve_action_name
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.loops._loop_fragments import loop_fragment_builders, pinned_step_info
from spectragent.core.loops._registrar import LoopInput
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.core.loops.react import (
    ReactLoop,
    _model_name,
    _serialize_llm_output,
    _UsageTally,
)
from spectragent.core.loops.restore import (
    RestorationContext,
    RestorationDivergedError,
    Restorer,
    SourceRun,
    default_restorer,
)
from spectragent.core.loops.restore.prefix_replay import (  # noqa: F401 — registration
    PrefixReplayRestorer,
)
from spectragent.core.loops.restore.source import first_difference, recorded_form
from spectragent.core.loops.run import LoopRun
from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.core.trace.models import RecordedLLMCall, TraceNegotiation
from spectragent.core.trace.redaction import OMITTED
from spectragent.schemas.action import Action
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation.dynamic_model import summarize_observation
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.restoration import Checkpoint, RestorationCheck
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: The parts of ``TraceConfig.negotiation`` a branch must reproduce: what the
#: agent observed and could do. The archive parts are the branch's own choice
#: (ruling D3) and are not compared.
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
class BranchRun(LoopRun):
    """A branch run's books: a :class:`LoopRun` plus its source.

    Attributes:
        source: The checkpoint's source run.
        step_info: The ``loop.step_info`` the source recorded at the
            checkpoint step, which pins what the policy steps are told about
            the step budget and the date (``None`` when the source recorded
            none).
        action_names: What this run's ``guarded_act`` executes (its
            descriptors' names, after any ``action_filter``).
        action_bindings: The short-name bindings it resolves with. With
            ``action_names``, what the replay hands
            :func:`~spectragent.core.action_guard.resolve_action_name` to
            learn the name the guard gave each action it was handed — the
            name the source's record carries. This run's guard stands in
            for the source's: ``weave`` proved the negotiation equal, and
            the seed check the seeded prompt, which carries the action table
            the guard was built from (for an agent that renders one).
    """

    source: SourceRun
    step_info: StepInfoObs | None = None
    action_names: frozenset[str] = frozenset()
    action_bindings: Mapping[str, str] = field(default_factory=dict)

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
        self._validate_slots(agent)
        inputs = deps.get("loop_inputs") or {}
        self.check_loop_inputs(inputs)
        source = SourceRun.open(inputs["checkpoint"])
        self._check_checkpoint(source, agent, deps)
        return super().weave(agent, **deps, branch_source=source)

    def _check_checkpoint(self, source: SourceRun, agent: Any, deps: dict[str, Any]) -> None:
        """Refuse a source this assembly cannot resume, naming every problem."""
        manifest = source.manifest
        if manifest.format_version < 2:
            raise IncompatibleCheckpointError(
                source.name,
                [
                    f"trace format {manifest.format_version}; branching reads the per-step "
                    "fragment and call records of format 2"
                ],
            )
        t = source.checkpoint.step
        problems: list[str] = []
        committed = source.committed_steps()
        gaps = [k for k in range(t + 1) if k not in committed]
        if gaps:
            problems.append(f"steps {gaps} are not committed (the checkpoint is step {t})")
        else:
            record = source.record(t)
            if record.origin != "policy":
                problems.append(f"step {t} is a {record.origin!r} step, not a policy step")
            if record.error_phase == "observe":
                problems.append(f"step {t} recorded no observation (its observe failed)")
        config = source.config
        if config.loop_id != "react":
            problems.append(
                f"the source ran loop {config.loop_id!r}; a branch replays ReAct's memory protocol"
            )
        if config.branch is not None:
            problems.append(
                "the source is itself a branch run (its prefix made no model calls to replay); "
                f"branch from its source run {config.branch.source_run_id!r} instead"
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
        negotiated_actions = ctx.get("negotiated_actions")
        run = BranchRun.from_ctx(
            agent,
            task,
            ctx,
            model_id=_model_name(ctx["llm"]),
            usage=_UsageTally(),
            source=source,
            step_info=source.step_info(t),
            action_names=frozenset(d.name for d in ctx.get("active_descriptors") or ()),
            action_bindings=dict(getattr(negotiated_actions, "action_bindings", None) or {}),
        )
        self._check_seed(run)
        await self._replay_prefix(run)

        restored = await self._restore(run)
        if isinstance(restored, AgentResult):
            return restored
        observation, check = restored

        budget = min(1 + self.continue_steps, self.max_steps)
        for offset in range(budget):
            result = await self._run_step(
                run,
                t + offset,
                observation=observation if offset == 0 else None,
                restoration=check if offset == 0 else None,
            )
            if result is not None:
                return result
        return await self._with_terminal(
            run,
            self._build_result(
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
            new_msgs = agent.build_react_prompt(observation)
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
            output_text = _serialize_llm_output(reply)
            try:
                actions = agent.parse_react_output(reply)
            except RunAbort as veto:
                await self._note_replay_veto(run, step, veto, llm_output=output_text)
                raise
            except ParseError as exc:
                parse_failure = exc
                self._remember_rejected(memory, output_text, exc)
                continue
            parse_failure = None

        record = source.record(step)
        if parse_failure is not None:
            run.parse_error_streak += 1
            kept: list[Action] = []
            dropped: list[str] = []
        else:
            run.parse_error_streak = 0
            kept, dropped = self._cap_actions(actions, step)
            self._remember_reply(memory, output_text, dropped)
        self._check_replayed_step(run, step, record, kept, dropped, output_text)

    def _recorded_reply(
        self,
        source: SourceRun,
        call: RecordedLLMCall,
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
        # rule says which (``resolve_action_name``).
        named = [
            (
                resolve_action_name(a.name, run.action_names, run.action_bindings) or a.name,
                a.params,
            )
            for a in kept
        ]
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
            llm_output=llm_output,
            timestamp=time.time(),
            error_phase="replay",
            error=str(veto),
        )
        run.step_records.append(record)
        await self._surrender_vetoed(run.recorder, record, None, veto)

    # 3. the environment

    async def _restore(self, run: BranchRun) -> tuple[Any, RestorationCheck] | AgentResult:
        """Run the restorer, recording each restored step as it arrives.

        Returns the checkpoint step's verified observation and its check, or
        the terminal result of a restoration that diverged (``strict``) or
        failed. A veto propagates, after the step it interrupted is recorded.
        """
        context = RestorationContext(
            env=run.env,
            act_fn=run.act_fn,
            negotiated_obs=run.negotiated_obs,
            source=run.source,
            checkpoint_step=run.checkpoint_step,
        )
        step = 0
        async with aclosing(self.restorer.restore(context)) as restoring:
            while True:
                try:
                    restored = await anext(restoring)
                except StopAsyncIteration:
                    break
                except RestorationDivergedError as exc:
                    record = self._restore_failure(
                        run, exc.check.step, str(exc), exc.check, exc.observation
                    )
                    return await self._end_failing(
                        run,
                        record,
                        exc.observation,
                        error=f"RestorationDivergedError: {exc}",
                        agent_done="error",
                        env_status="failed",
                        answer=run.last_answer,
                    )
                except RunAbort as veto:
                    logger.warning(
                        "BranchLoop: run aborted while restoring step %d: %s", step, veto
                    )
                    record = self._restore_failure(run, step, str(veto), None)
                    await self._surrender_vetoed(run.recorder, record, None, veto)
                    raise
                except Exception as exc:
                    logger.exception("BranchLoop: restoring step %d failed", step)
                    record = self._restore_failure(run, step, f"{type(exc).__name__}: {exc}", None)
                    return await self._end_failing(
                        run,
                        record,
                        None,
                        error=f"restore failed at step {step}: {type(exc).__name__}: {exc}",
                        agent_done="error",
                        env_status="failed",
                        answer=run.last_answer,
                    )
                if restored.step >= run.checkpoint_step:
                    return restored.observation, restored.check
                # Outside the handlers on purpose: a recorder that cannot
                # write a restored step ends the run like any other step's
                # (fail closed) — it is not a restoration failure.
                record = self._restored_record(run, restored.step, restored.check)
                run.step_records.append(record)
                run.prev_step_record = record
                await self._surrender(run.recorder, record, restored.observation)
                step = restored.step + 1
        raise IncompatibleCheckpointError(
            run.source.name,
            [f"restorer {self.restorer.restorer_id!r} ended without the checkpoint step"],
        )

    def _restored_record(self, run: BranchRun, step: int, check: RestorationCheck) -> StepRecord:
        """A restored step: the source's record of it, marked and re-stamped.

        Its actions, results and reply are the source's, so the policy step
        sees the same ``loop.history`` / ``loop.action_result`` the source
        saw at *t*. The source's tokens are not this run's, and the timings
        are measured anew by the recorder; the timestamp is now.
        """
        source_record = run.source.record(step)
        for result in source_record.action_results:
            if result.extracted_content:
                run.last_answer = result.extracted_content
        return source_record.model_copy(
            update={
                "origin": "restored",
                "restoration": check,
                "token_usage": None,
                "duration_ms": None,
                "llm_latency_ms": None,
                "timestamp": time.time(),
            },
            deep=True,
        )

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
        record = StepRecord(
            step=step,
            origin="restored",
            observation_summary=(
                summarize_observation(observation) if observation is not None else ""
            ),
            timestamp=time.time(),
            error_phase="restore",
            error=error,
            restoration=check,
        )
        run.step_records.append(record)
        return record

    # 4. the policy step's view of the budget

    def _loop_fragment_builders(self, run: LoopRun, step_idx: int) -> dict[str, Any]:
        """ReAct's builders, with ``loop.step_info`` pinned to the source's.

        The policy step is told the step budget and the date the source
        was told at the checkpoint (``today_override`` still wins,
        ``pinned_step_info``), so the decision is made on the prompt the
        source made it on.
        """
        pinned = run.step_info if isinstance(run, BranchRun) else None
        if pinned is None:
            return super()._loop_fragment_builders(run, step_idx)
        max_steps, today = pinned_step_info(pinned, today_override=self.today_override)
        return loop_fragment_builders(
            task=run.task,
            step=step_idx,
            max_steps=max_steps,
            today=today,
            history=run.step_records,
            prev_step=run.prev_step_record,
        )
