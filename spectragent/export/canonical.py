"""Recorded steps → :class:`CanonicalStepSample` (arch §4.15 "可导出的步与 CanonicalStepSample").

Reads one run through ``RunTrace`` only — its committed steps, the fragment
index its manifest lists, its configuration and its verdicts — so a step
file the WAL left uncommitted is as invisible here as it is to every other
reader. Translation into the student's action space and rendering are later
stages; a sample leaves here with ``actions`` unset.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Literal

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.trace.reader import RunTrace
from spectragent.export.errors import missing_export_slots
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import FragmentBase, ObservationRegistrar
from spectragent.schemas.observation.geometry import ScreenshotGeometry
from spectragent.schemas.observation.image import image_media_type
from spectragent.schemas.sample import (
    BranchSampleMeta,
    CanonicalStepSample,
    DroppedParams,
    FragmentRef,
    ImageRef,
    SampleLabels,
    SampleRedaction,
    ThoughtRecord,
    VerdictLabel,
)
from spectragent.schemas.step_record import StepRecord

ThoughtSource = Literal["teacher", "none"]


def executed_actions(record: StepRecord) -> list[Action]:
    """The actions of *record* that ran: those with a result, in order."""
    return [action for action, _result in zip(record.actions, record.action_results, strict=False)]


def is_exportable(record: StepRecord) -> bool:
    """A step a student can learn from: the run's own decision, healthy, that acted.

    ``origin == "policy"`` (a branch's restored prefix and every terminal
    observation are not decisions), no ``error_phase``, and at least one
    executed action. A reply that failed to parse and one that parsed to no
    action look the same on a ``StepRecord`` (M31's open question), and
    neither is a sample.
    """
    return record.origin == "policy" and not record.error_phase and bool(executed_actions(record))


def canonicalise(
    trace: RunTrace,
    *,
    rel_path: str,
    thought_source: ThoughtSource = "none",
    teacher_cls: type | None = None,
    verdicts_from: RunTrace | None = None,
) -> list[CanonicalStepSample]:
    """One :class:`CanonicalStepSample` per exportable committed step of *trace*, in step order.

    Only steps the trace is complete through (``complete_through``): past a
    hole, a step's context would rest on a step nobody recorded (the hard
    condition of arch §4.15).

    Args:
        trace: The source run.
        rel_path: The run directory relative to the export's sources root
            (POSIX) — recorded as ``source_trace``.
        thought_source: ``teacher`` reads each step's thought with the
            teacher's ``parse_react_thought`` (arch §4.1); ``none`` records
            none.
        teacher_cls: The teacher agent's class (``AgentRegistrar`` under
            the trace's ``agent_id``); needed for ``thought_source: teacher``.
        verdicts_from: Whose ``evals/`` label the samples: *trace* itself by
            default; a branch run's source run (the caller resolves it —
            branch runs carry no verdicts of their own in v1).

    Raises:
        IncompatibleError: ``thought_source`` is ``teacher`` and the teacher
            class has no ``parse_react_thought`` (worded as the export's
            need, not a Loop's).
        TraceSchemaError: The trace cannot be read as committed (a file
            its manifest lists is damaged or missing).
    """
    trace.require_recording("trajectory", "observations", consumer="training export")
    config = trace.config
    read_thought = _thought_reader(config.agent_id, config.loop_id, thought_source, teacher_cls)
    through = trace.manifest.complete_through
    entries = {entry.step: entry for entry in trace.manifest.steps if entry.step <= through}
    records = [record for step in sorted(entries) if (record := trace.step(step)) is not None]
    exportable = [record.step for record in records if is_exportable(record)]
    exportable_set = frozenset(exportable)

    labels_verdicts = _verdicts(verdicts_from if verdicts_from is not None else trace)
    redactions = _redactions(config)
    branch = _branch_meta(trace, records)
    checkpoint = branch.checkpoint_step if branch is not None else None

    samples: list[CanonicalStepSample] = []
    for record in records:
        if record.step not in exportable_set:
            continue
        executed = executed_actions(record)
        teacher_actions, dropped = _project(executed)
        observations, image = _observations(trace, record.step, entries[record.step].fragments)
        history = [step for step in exportable if step < record.step]
        if checkpoint is not None:
            history = list(range(checkpoint)) + [step for step in history if step >= checkpoint]
        results = record.action_results[: len(executed)]
        samples.append(
            CanonicalStepSample(
                sample_id=f"{trace.run_id}/s{record.step}",
                run_id=trace.run_id,
                source_trace=rel_path,
                step=record.step,
                benchmark=config.benchmark,
                subset=config.subset,
                task_id=config.task_id,
                agent_id=config.agent_id,
                loop_id=config.loop_id,
                llm_model=config.llm_model,
                config_hash=trace.manifest.config_hash,
                task=config.task,
                observations=observations,
                image=image,
                history_steps=history,
                teacher_actions=teacher_actions,
                dropped_params=dropped,
                thought=read_thought(record.llm_output),
                labels=SampleLabels(
                    step_status="ok" if all(r.success for r in results) else "action_failed",
                    verdicts=labels_verdicts,
                ),
                redactions=redactions,
                branch=branch,
            )
        )
    return samples


def _thought_reader(
    agent_id: str, loop_id: str, thought_source: ThoughtSource, teacher_cls: type | None
) -> Callable[[str], ThoughtRecord | None]:
    if thought_source == "none":
        return lambda _reply: None
    parse: Callable[[str], tuple[str, str] | None] | None = getattr(
        teacher_cls, "parse_react_thought", None
    )
    if parse is None or not callable(parse):
        raise missing_export_slots(agent_id, loop_id or "react", ["parse_react_thought"])

    def read(reply: str) -> ThoughtRecord | None:
        found = parse(reply)
        if found is None:
            return None
        text, source_field = found
        return ThoughtRecord(text=text, source_field=source_field)

    return read


def _project(actions: list[Action]) -> tuple[list[Action], list[DroppedParams]]:
    """Each action with only the params its ``Params`` declares; the rest named.

    An agent may write more into an action's params than the action reads —
    Skyvern puts its per-action ``reasoning`` there. The executed action is
    what the declared params say, so that is the training target. An action
    no registered class claims is kept as recorded: its translator says
    what it can do with it.
    """
    projected: list[Action] = []
    dropped: list[DroppedParams] = []
    for index, action in enumerate(actions):
        cls = ActionRegistrar.get_or_none(action.name)
        params_model = getattr(cls, "Params", None) if cls is not None else None
        if params_model is None:
            projected.append(Action(name=action.name, params=dict(action.params)))
            continue
        declared = params_model.model_fields
        projected.append(
            Action(
                name=action.name,
                params={key: value for key, value in action.params.items() if key in declared},
            )
        )
        extra = sorted(key for key in action.params if key not in declared)
        if extra:
            dropped.append(DroppedParams(action_index=index, params=extra))
    return projected, dropped


def _env_sourced(key: str) -> bool:
    cls = ObservationRegistrar.get_or_none(key)
    return getattr(cls, "__source__", "env") == "env"


def _observations(
    trace: RunTrace, step: int, index: dict[str, Any]
) -> tuple[list[FragmentRef], ImageRef | None]:
    """References to the step's environment-sourced fragments, and its image."""
    refs: list[FragmentRef] = []
    image: ImageRef | None = None
    for key in sorted(index):
        if not _env_sourced(key):
            continue
        files = index[key]
        if files.failed:
            refs.append(FragmentRef(key=key, file=files.file, failed=True))
            continue
        digest: str | None = None
        if files.artifact is not None:
            fragment = trace.observation_fragment(step, key)
            binary = fragment.trace_binary() if isinstance(fragment, FragmentBase) else None
            data = binary[1] if binary is not None else None
            if data:
                digest = hashlib.sha256(data).hexdigest()
                if image is None and isinstance(fragment, ScreenshotGeometry):
                    image = ImageRef(
                        fragment=key,
                        sha256=digest,
                        media_type=image_media_type(data) or "application/octet-stream",
                        image_width=fragment.image_width,
                        image_height=fragment.image_height,
                        viewport_width=fragment.viewport_width,
                        viewport_height=fragment.viewport_height,
                    )
        refs.append(
            FragmentRef(key=key, file=files.file, artifact=files.artifact, artifact_sha256=digest)
        )
    return refs, image


def _verdicts(trace: RunTrace) -> list[VerdictLabel]:
    return [
        VerdictLabel(
            verdict_slug=slug,
            evaluator_id=verdict.evaluator_id,
            predicted_label=verdict.predicted_label,
        )
        for slug, verdict in trace.evals().items()
        if verdict.verdict_source == "graded"
    ]


def _redactions(config: Any) -> list[SampleRedaction]:
    setup = config.task_setup
    if setup is None:
        return []
    ledger = setup.redactions
    return [
        SampleRedaction(
            policy=ledger.policy, location=entry.location, field=entry.field, rule=entry.rule
        )
        for entry in ledger.entries
    ]


def _branch_meta(trace: RunTrace, records: list[StepRecord]) -> BranchSampleMeta | None:
    provenance = trace.config.branch
    if provenance is None:
        return None
    terminal = next((record for record in records if record.origin == "terminal"), None)
    image = trace.screenshot(terminal.step) if terminal is not None else None
    return BranchSampleMeta(
        branch_group_id=provenance.branch_group_id,
        source_run_id=provenance.source_run_id,
        checkpoint_step=provenance.checkpoint_step,
        branch_index=provenance.branch_index,
        terminal_step=terminal.step if terminal is not None else None,
        terminal_image_sha256=hashlib.sha256(image).hexdigest() if image else None,
    )


__all__ = ["ThoughtSource", "canonicalise", "executed_actions", "is_exportable"]
