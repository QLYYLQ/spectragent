"""Student rendering: a recorded trajectory re-told in the student's own words (arch §4.15 "学生渲染").

The student is an ordinary af agent. Its run configuration is composed and
**assembled only** — ``AgentRuntime.prepare()``, the same negotiation, the
same ``fragments=``-narrowed descriptors, bindings and weave as a live run —
and every sample's context is rebuilt by the student's own ``prepare`` seed
(:meth:`AgentRuntime.seed_messages`), ``build_react_prompt`` and memory
strategy, with the target turn written by its ``format_react_output``. So the
training format is the serving format. The environment is never set up, no
LLM is called, nothing is executed.

**A trajectory, in segments.** A trajectory is one source run (a branch run:
its source run's steps ``0..t-1`` and then the branch's own policy steps).
A segment starts at a renderable step with the student's memory cleared and
seeded for the task; within it, step *k*'s observation is the student's
``NegotiatedObservation`` — env-source fragments read back from the trace
(``core/loops/restore/source.py::recorded_observation``), loop-source ones
built by ``loop_fragment_builders`` with the ``max_steps`` / ``today`` step
*k* recorded (``today_override`` first) and the segment's earlier steps as
history — then ``build_react_prompt`` writes into memory, the context at that
moment is the sample's, and the reply ``format_react_output`` writes for the
step's translated actions goes into memory through the Loop's own
``_remember_reply``. A step that is not exportable, or that the
``untranslatable`` policy drops, ends the segment.

**Round trip.** Every reply written is parsed back at once (which also
advances any agent state a served reply would) and must satisfy the four
clauses of arch §4.15 "round-trip"; a failure is a :class:`RoundTripError` —
a formatter defect, never an untranslatable step.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml

from spectragent.core.action_guard import resolve_action_name
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.params import param_fragments, point_params
from spectragent.core.capabilities.base import CaptureComparison
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.loops._loop_fragments import (
    loop_fragment_builders,
    pinned_step_info,
    populate_loop_fragments,
)
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.core.loops.react import ReactLoop
from spectragent.core.loops.restore.source import recorded_observation, recorded_step_info
from spectragent.core.negotiation.observation import IncompatibleFragmentError
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.export.canonical import ThoughtSource
from spectragent.export.errors import (
    ExportConfigError,
    RoundTripError,
    StudentCaptureMismatchError,
    UntranslatableActionError,
    missing_export_slots,
)
from spectragent.export.settings import ThoughtIndexRefs
from spectragent.export.translate._registrar import UntranslatablePolicy
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import FragmentBase, fragment_field_name
from spectragent.schemas.observation.geometry import CoordinateSpace, point_grid
from spectragent.schemas.sample import CanonicalStepSample, ThoughtRecord, UntranslatableReason
from spectragent.schemas.step_record import StepRecord

StepInfoSource = Literal["recorded", "fallback"]

#: An element-index token in a thought (``[123]``) — meaningless to a student
#: that does not see the selector map those indices address.
_INDEX_REF = re.compile(r"\[\d+\]")
#: The student's grid when its agent declares no ``coordinate_space``.
_DEFAULT_SPACE: CoordinateSpace = "screenshot_pixels"


@dataclass(frozen=True)
class RenderedSample:
    """One sample, as the student would have been asked for it, and its answer.

    Attributes:
        sample: The canonical sample, translated (``actions`` set).
        context: The student's context at the step — its seed messages, the
            segment's earlier turns and this step's observation turn —
            exactly what its memory would hand the model.
        target: The student's reply for the step's translated actions: the
            training target (weight 1).
        target_actions: What *target* executes when the student serves it —
            the parsed reply, named as the student's guard names it. Its
            points are the quantised ones: the training label.
        segment: Which context segment of its trajectory the sample is in.
        context_steps: The steps whose turns precede the target in
            ``context``, in order.
        quantization: Per point param of the target, the larger axis error
            between the point *target* executes and the translated point, in
            viewport px.
        thought: The thought the target turn carries, or ``None``.
        thought_dropped: A teacher thought was dropped because it referenced
            element indices the student does not see.
        step_info: Where the step's ``max_steps`` / ``today`` came from:
            the source run's recorded ``loop.step_info``, or the fallback.
        source: The source run the step was recorded in (path relative to
            the sources root).
    """

    sample: CanonicalStepSample
    context: tuple[BaseMessage, ...]
    target: str
    target_actions: tuple[Action, ...]
    segment: int
    context_steps: tuple[int, ...]
    quantization: tuple[int, ...]
    thought: ThoughtRecord | None
    thought_dropped: bool
    step_info: StepInfoSource
    source: str


@dataclass(frozen=True)
class TrajectoryStep:
    """One committed step of a trajectory to re-tell, in order.

    Attributes:
        trace: The run that recorded the step.
        source: How the manifest and errors name that run (its path
            relative to the sources root).
        record: The committed record.
        sample: The step's canonical sample, translated, or ``None`` when the
            step is not exportable — it then ends the current segment.
        emit: Whether a rendered step is a sample of this export; a branch's
            source prefix is context only.
    """

    trace: RunTrace
    source: str
    record: StepRecord
    sample: CanonicalStepSample | None
    emit: bool = True


@dataclass
class RenderStats:
    """What rendering did with the emitted steps, for the dataset manifest.

    Attributes:
        untranslatable: Reason → emitted steps the policy dropped, kept as
            text or truncated at (``fail`` raises instead).
        skipped: Why emitted steps were never rendered: ``not_exportable``
            (``is_exportable`` refused it), ``fragment_unavailable`` (a fragment
            the student needs is not recorded at the step), ``truncated``
            (after a ``truncate``).
        thought_fields: The teacher field each target thought came from.
        thoughts_dropped: Target thoughts dropped for index references.
        step_info: Targets per ``step_info`` source.
        quantization: Point error in px → points.
        capture_unchecked: Settings the student's capture check could not
            compare, as the capability named them.
    """

    untranslatable: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    thought_fields: Counter[str] = field(default_factory=Counter)
    thoughts_dropped: int = 0
    step_info: Counter[str] = field(default_factory=Counter)
    quantization: Counter[int] = field(default_factory=Counter)
    capture_unchecked: set[str] = field(default_factory=set)


@dataclass
class _Segment:
    """One context segment: its number and the steps rendered into it so far."""

    index: int
    steps: list[int] = field(default_factory=list)
    #: Copies of those steps' records with their actions as the student saw them.
    history: list[StepRecord] = field(default_factory=list)


class _Truncated(Exception):
    """The trajectory ends here (``untranslatable: truncate``)."""


class StudentRenderer:
    """One student, assembled once, re-telling recorded trajectories (module docstring).

    Build it with :meth:`from_config` (the export's ``student_run_config``) or
    from an already composed run. The student must have exactly one agent, a
    ``ReactLoop`` (or subclass) and no hooks; its agent must have
    ``format_react_output``.

    Raises:
        ExportConfigError: The run is not a student (see above).
        IncompatibleError: The agent lacks ``format_react_output``, worded as
            the export's need (arch §4.1 "可选 slot").
    """

    def __init__(
        self,
        run: ComposedRun,
        *,
        untranslatable: UntranslatablePolicy,
        thought_source: ThoughtSource,
        thought_index_refs: ThoughtIndexRefs,
    ) -> None:
        agent_ids = run.ids("agent")
        if len(agent_ids) != 1:
            raise ExportConfigError(
                f"the student's run configuration must hold exactly one agent, it holds "
                f"{len(agent_ids)}"
            )
        if run.hooks:
            raise ExportConfigError(
                "the student's run configuration declares hooks; a student is assembled, "
                "never run — remove its hook block"
            )
        self._run = run
        self.runtime = AgentRuntime.for_agent(run, agent_ids[0])
        self.agent: Any = self.runtime.prepare()
        loop = self.agent.agent_loop
        if not isinstance(loop, ReactLoop):
            raise ExportConfigError(
                f"the student's loop is {getattr(loop, 'loop_id', type(loop).__name__)!r}: "
                f"samples are ReAct turns, so it must be 'react' or a subclass"
            )
        self.loop: ReactLoop = loop
        if not callable(getattr(self.agent, "format_react_output", None)):
            raise missing_export_slots(
                getattr(self.agent, "agent_name", type(self.agent).__name__),
                loop.loop_id,
                ["format_react_output"],
            )
        self.memory: Any = self.agent.memory
        self.untranslatable = untranslatable
        self.thought_source = thought_source
        self.thought_index_refs = thought_index_refs
        negotiated_obs = self.runtime.negotiated_observation
        negotiated_actions = self.runtime.negotiated_actions
        assert negotiated_obs is not None and negotiated_actions is not None  # prepare() ran
        self.negotiated: Any = negotiated_obs
        self.guarded: frozenset[str] = frozenset(d.name for d in self.runtime.active_descriptors)
        self.bindings: Mapping[str, str] = dict(negotiated_actions.action_bindings)
        space = getattr(self.runtime.agent, "coordinate_space", None)
        self.coordinate_space: CoordinateSpace = (
            cast(CoordinateSpace, space) if space in get_args(CoordinateSpace) else _DEFAULT_SPACE
        )
        self.stats = RenderStats()
        self._index_fragments: dict[str, frozenset[str]] = {}

    @classmethod
    def from_config(
        cls,
        path: Path,
        *,
        composer: ConfigComposer,
        untranslatable: UntranslatablePolicy,
        thought_source: ThoughtSource,
        thought_index_refs: ThoughtIndexRefs,
    ) -> StudentRenderer:
        """Compose the student's run YAML at *path* and assemble it."""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ExportConfigError(f"student_run_config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ExportConfigError(f"student_run_config {path} is not a YAML mapping")
        return cls(
            composer.compose(raw),
            untranslatable=untranslatable,
            thought_source=thought_source,
            thought_index_refs=thought_index_refs,
        )

    # ── identity, for the dataset manifest ───────────────────

    @property
    def agent_id(self) -> str:
        return str(getattr(self.runtime.agent, "agent_name", type(self.runtime.agent).__name__))

    @property
    def loop_id(self) -> str:
        return self.loop.loop_id

    @property
    def config_sha256(self) -> str:
        """sha256 of the student's validated configuration, as canonical JSON."""
        cfg = self._run.top_level_cfg
        payload = cfg.model_dump(mode="json") if cfg is not None else {}
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def env_fragments(self) -> frozenset[str]:
        """The env-source fragments the student negotiated."""
        return frozenset(self.negotiated.env_sourced)

    # ── per source ───────────────────────────────────────────

    def check_source(self, trace: RunTrace, source: str) -> None:
        """The student can be shown this source: every env fragment it negotiates was recorded.

        Raises:
            IncompatibleFragmentError: Naming the fragments the source run did
                not record and the run (``provider=``) — a DOM student cannot
                learn from a vision-only recording (re-rendering from raw
                archives is a later extension).
        """
        negotiation = trace.config.negotiation
        recorded = set(negotiation.env_sourced_fragments) if negotiation is not None else set()
        missing = set(self.env_fragments) - recorded
        if missing:
            raise IncompatibleFragmentError(
                missing,
                recorded,
                {key: [f"{self.agent_id} (the student)"] for key in sorted(missing)},
                provider=f"source run {source!r} (as recorded)",
            )

    # ── rendering ────────────────────────────────────────────

    def render(self, steps: Sequence[TrajectoryStep]) -> list[RenderedSample]:
        """Every emitted, renderable step of one trajectory, rendered in order.

        Raises:
            UntranslatableActionError: An untranslatable step under
                ``untranslatable: fail``.
            RoundTripError: A reply the student wrote does not read back.
            StudentCaptureMismatchError: The student's configuration would not
                capture what a sampled step recorded.
        """
        rendered: list[RenderedSample] = []
        segment: _Segment | None = None
        segments = 0
        for position, item in enumerate(steps):
            sample = item.sample
            if sample is None:
                if item.emit:
                    self.stats.skipped["not_exportable"] += 1
                segment = None
                continue
            try:
                outcome = self._render_step(item, sample, segment, segments)
            except _Truncated:
                self.stats.skipped["truncated"] += sum(
                    1 for later in steps[position + 1 :] if later.emit and later.sample is not None
                )
                break
            if outcome is None:
                segment = None
                continue
            segment, result = outcome
            segments = max(segments, segment.index + 1)
            if result is not None:
                rendered.append(result)
        return rendered

    def _render_step(
        self,
        item: TrajectoryStep,
        sample: CanonicalStepSample,
        segment: _Segment | None,
        segments: int,
    ) -> tuple[_Segment, RenderedSample | None] | None:
        """Render one exportable step; ``None`` when it ends the current segment."""
        refusal = self._refusal(sample)
        if refusal is not None and self.untranslatable != "keep_as_text":
            self._refuse(item, sample, *refusal)
            return None
        observation, _unavailable = recorded_observation(
            item.trace, item.record.step, self.negotiated, keys=self.negotiated.env_sourced
        )
        if observation is None:
            if item.emit:
                self.stats.skipped["fragment_unavailable"] += 1
            return None
        if item.emit and refusal is None:
            self._check_capture(item, observation)
        if segment is None:
            segment = self._open(item, segments)
        observation, step_info = self._with_loop_fragments(item, observation, segment)
        self.memory.add_messages(self.agent.build_react_prompt(observation))
        actions = sample.actions or []
        if refusal is not None:
            self._refuse(item, sample, *refusal)  # keep_as_text: counted, then the text turn
            self._keep_as_text(segment, item, sample, refusal[0])
            return segment, None

        context = tuple(m.model_copy(deep=True) for m in self.memory.get_context_messages())
        thought, dropped = self._thought(item, sample)
        try:
            text = self.agent.format_react_output(
                actions, thought=thought.text if thought is not None else None
            )
        except UnrepresentableActionError as exc:
            self._refuse(item, sample, "unrepresentable", str(exc))
            if self.untranslatable != "keep_as_text":
                return None
            self._keep_as_text(segment, item, sample, "unrepresentable")
            return segment, None
        executed, errors = self._round_trip(sample, actions, text, thought)
        self.loop._remember_reply(self.memory, text, [])
        context_steps = tuple(segment.steps)
        segment.steps.append(item.record.step)
        segment.history.append(_as_student_saw(item.record, actions))
        if not item.emit:
            return segment, None
        self.stats.step_info[step_info] += 1
        self.stats.quantization.update(errors)
        if thought is not None:
            self.stats.thought_fields[thought.source_field] += 1
        if dropped:
            self.stats.thoughts_dropped += 1
        return segment, RenderedSample(
            sample=sample,
            context=context,
            target=text,
            target_actions=tuple(executed),
            segment=segment.index,
            context_steps=context_steps,
            quantization=tuple(errors),
            thought=thought,
            thought_dropped=dropped,
            step_info=step_info,
            source=item.source,
        )

    # ── the pieces ───────────────────────────────────────────

    def _refusal(self, sample: CanonicalStepSample) -> tuple[UntranslatableReason, str] | None:
        """Why the student cannot take this step before it is shown anything, or ``None``.

        The translator's reason first; then a name the student's guard does
        not execute (``no_counterpart``) and more actions than its Loop runs
        per step (``exceeds_action_budget``). Whether the student can *write*
        the actions is learned when it tries (``unrepresentable``).
        """
        if sample.untranslatable is not None:
            return sample.untranslatable.reason, sample.untranslatable.detail
        actions = sample.actions or []
        for action in actions:
            if resolve_action_name(action.name, self.guarded, self.bindings) is None:
                return "no_counterpart", f"the student does not offer {action.name}"
        budget = self.loop.max_actions_per_step
        if len(actions) > budget:
            return (
                "exceeds_action_budget",
                f"{len(actions)} actions, the student's loop runs {budget} per step",
            )
        return None

    def _refuse(
        self,
        item: TrajectoryStep,
        sample: CanonicalStepSample,
        reason: UntranslatableReason,
        detail: str,
    ) -> None:
        """Apply the ``untranslatable`` policy's decision for one step (counted when emitted).

        Raises:
            UntranslatableActionError: Under ``fail``.
            _Truncated: Under ``truncate``.
        """
        if self.untranslatable == "fail":
            raise UntranslatableActionError(sample.sample_id, reason, detail)
        if item.emit:
            self.stats.untranslatable[reason] += 1
        if self.untranslatable == "truncate":
            raise _Truncated

    def _keep_as_text(
        self,
        segment: _Segment,
        item: TrajectoryStep,
        sample: CanonicalStepSample,
        reason: UntranslatableReason,
    ) -> None:
        """``keep_as_text``: the step stays in the context, its reply written as text."""
        actions = sample.actions if sample.actions is not None else sample.teacher_actions
        written = json.dumps(
            [action.model_dump(mode="json") for action in actions],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.loop._remember_reply(self.memory, f"[not expressible: {reason}] {written}", [])
        segment.steps.append(item.record.step)
        segment.history.append(_as_student_saw(item.record, list(actions)))

    def _open(self, item: TrajectoryStep, index: int) -> _Segment:
        """A new segment: the student's memory cleared and seeded for the task."""
        self.memory.clear()
        self.memory.add_messages(self.runtime.seed_messages(item.trace.config.task))
        return _Segment(index=index)

    def _with_loop_fragments(
        self, item: TrajectoryStep, observation: Any, segment: _Segment
    ) -> tuple[Any, StepInfoSource]:
        """*observation* with the loop-source fragments step *k* was shown, and their source."""
        step = item.record.step
        recorded = recorded_step_info(item.trace, step)
        if recorded is not None:
            max_steps, today = pinned_step_info(recorded, today_override=self.loop.today_override)
            source: StepInfoSource = "recorded"
        else:
            created = item.trace.manifest.created_at.astimezone(UTC).date().isoformat()
            max_steps, today = self.loop.max_steps, self.loop.today_override or created
            source = "fallback"
        observation = populate_loop_fragments(
            observation,
            loop_sourced=frozenset(self.negotiated.loop_sourced),
            builders=loop_fragment_builders(
                task=item.trace.config.task,
                step=step,
                max_steps=max_steps,
                today=today,
                history=list(segment.history),
                prev_step=segment.history[-1] if segment.history else None,
            ),
        )
        return observation, source

    def _check_capture(self, item: TrajectoryStep, observation: Any) -> None:
        """The student environment's ``compare_capture`` on the step's recorded fragments."""
        env = self.runtime.env
        compare = getattr(env, "compare_capture", None)
        if not callable(compare):
            return
        recorded = {
            key: value
            for key in sorted(self.negotiated.env_sourced)
            if isinstance(
                value := getattr(observation, fragment_field_name(key), None), FragmentBase
            )
        }
        comparison = cast(CaptureComparison, compare(recorded))
        if comparison.mismatches:
            raise StudentCaptureMismatchError(
                item.source, item.record.step, tuple(comparison.mismatches)
            )
        self.stats.capture_unchecked.update(comparison.unchecked)

    def _thought(
        self, item: TrajectoryStep, sample: CanonicalStepSample
    ) -> tuple[ThoughtRecord | None, bool]:
        """The thought the step's reply carries, and whether one was dropped for index refs."""
        if self.thought_source == "none" or sample.thought is None:
            return None, False
        if (
            self.thought_index_refs == "drop"
            and self._indices_unseen(item.trace)
            and _INDEX_REF.search(sample.thought.text)
        ):
            return None, True
        return sample.thought, False

    def _indices_unseen(self, trace: RunTrace) -> bool:
        """Whether the source's index params address fragments the student does not see."""
        run_id = trace.run_id
        if run_id not in self._index_fragments:
            negotiation = trace.config.negotiation
            keys: set[str] = set()
            for name in negotiation.active_actions if negotiation is not None else ():
                params = getattr(ActionRegistrar.get_or_none(name), "Params", None)
                if params is not None:
                    for dependencies in param_fragments(params).values():
                        keys |= dependencies
            self._index_fragments[run_id] = frozenset(keys)
        return bool(self._index_fragments[run_id] - frozenset(self.negotiated.active_fragments))

    def _round_trip(
        self,
        sample: CanonicalStepSample,
        actions: Sequence[Action],
        text: str,
        thought: ThoughtRecord | None,
    ) -> tuple[list[Action], list[int]]:
        """The four round-trip clauses (arch §4.15); the executed actions and point errors.

        Raises:
            RoundTripError: Naming the clause that failed.
        """
        sample_id = sample.sample_id
        try:
            parsed = list(self.agent.parse_react_output(text))
        except ParseError as exc:
            raise RoundTripError(sample_id, f"the reply does not parse: {exc}") from exc
        if len(parsed) != len(actions):
            raise RoundTripError(
                sample_id, f"{len(actions)} action(s) written, {len(parsed)} read back"
            )
        again = self.agent.format_react_output(
            parsed, thought=thought.text if thought is not None else None
        )
        if again != text:
            raise RoundTripError(sample_id, "format(parse(text)) is not text (no fixed point)")
        grid = point_grid(self.coordinate_space, sample.image) if sample.image else None
        quantum = grid.quantum if grid is not None else 0
        executed: list[Action] = []
        errors: list[int] = []
        for action, back in zip(actions, parsed, strict=True):
            name = resolve_action_name(back.name, self.guarded, self.bindings)
            if name != action.name:
                raise RoundTripError(
                    sample_id, f"{action.name} reads back as {back.name} ({name or 'unguarded'})"
                )
            params_model = getattr(ActionRegistrar.get_or_none(action.name), "Params", None)
            pairs = point_params(params_model) if params_model is not None else []
            point_keys = {key for pair in pairs for key in pair}
            for x_key, y_key in pairs:
                if action.params.get(x_key) is None:
                    continue
                x, y = back.params.get(x_key), back.params.get(y_key)
                if not isinstance(x, int | float) or not isinstance(y, int | float):
                    raise RoundTripError(
                        sample_id,
                        f"{action.name} point ({x_key}, {y_key}) reads back as ({x}, {y})",
                    )
                error = max(abs(x - action.params[x_key]), abs(y - action.params[y_key]))
                if error > quantum:
                    raise RoundTripError(
                        sample_id,
                        f"{action.name} point ({x_key}, {y_key}) moved {error} px, more than "
                        f"the grid's quantum {quantum}",
                    )
                errors.append(int(error))
            for key, value in action.params.items():
                if value is not None and key not in point_keys and back.params.get(key) != value:
                    raise RoundTripError(
                        sample_id,
                        f"{action.name} param {key!r} reads back as {back.params.get(key)!r}, "
                        f"not {value!r}",
                    )
            executed.append(Action(name=action.name, params=dict(back.params)))
        return executed, errors


def _as_student_saw(record: StepRecord, actions: Sequence[Action]) -> StepRecord:
    """A copy of *record* with its actions as the student's history shows them."""
    return record.model_copy(update={"actions": list(actions)}, deep=True)


__all__ = [
    "RenderStats",
    "RenderedSample",
    "StepInfoSource",
    "StudentRenderer",
    "TrajectoryStep",
]
