"""``wait_actions: drop_when_capped``: the waits a teacher took on a capped capture, removed.

``docs/architecture_design.md`` §4.15, "等待动作". On a slow live network the
settle wait before a capture is ended by its cap (``capture_state``'s
``settle_state.outcome == "capped"``), the teacher sees a page that has not
loaded, and it waits. A student served on a normal network should not learn
that, so the export may leave those waits out of what it trains on — never
out of the recorded trace, which it only reads.

Three pieces, in pipeline order:

- :func:`capped_waits` reads one run and says, per policy step, which waits
  go: on a step whose capture state says capped, every wait (an action whose
  class declares ``only_waits``, of the capability whose capture was capped)
  that ran and succeeded. A failed wait stays (it explains the skipped tail
  after it), and so does a wait in the skipped tail (it never ran). The
  :class:`StepWaits` it returns carries the step without them; a step with
  nothing left leaves the trajectory.
- :func:`without_waits` applies that to the step's canonical sample, before
  translation.
- :class:`WaitShaper`, per trajectory, before the student renders it: later
  samples' ``history_steps`` lose the removed steps, and the thoughts the
  removal affects — a step that lost waits and kept other actions, and the
  first step kept after removed ones — are rewritten by the exporter's
  helper chat model through :class:`ThoughtRewriter`, whose content-addressed
  cache keeps the export deterministic: a re-export reads every rewrite back
  and calls no model.

The rewriter sees the thought and the removed actions, nothing else: it
neither reads nor writes a trace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from spectragent.core.actions._registrar import is_wait_action
from spectragent.core.llm.base import BaseChatModel, require_inference_protocol
from spectragent.core.llm.lifecycle import close_models, report_cleanup
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.trace.reader import RunTrace
from spectragent.export.canonical import is_exportable, target_actions
from spectragent.export.errors import ThoughtRewriteCacheError
from spectragent.export.render import TrajectoryStep
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.capture_state import CaptureStateFragment
from spectragent.schemas.sample import (
    CanonicalStepSample,
    RemovedAction,
    RewriteModel,
    ThoughtRecord,
    ThoughtRewrite,
    WaitRemoval,
)
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

#: The rewrite prompt's version; :data:`REWRITE_PROMPT_DIGEST` covers its text.
REWRITE_PROMPT_VERSION = "wait-thought-rewrite-1"

REWRITE_SYSTEM_PROMPT = (
    "You edit one thought an agent wrote before it acted. The agent's run was recorded on "
    "a slow network, where the page or screen sometimes had not finished loading when the "
    "agent looked at it, and the agent waited. Those waits have been removed from the "
    "recording, and the thought must now read as if everything had loaded normally.\n"
    "\n"
    "Remove only the words that refer to waiting, or to the page or screen still loading "
    "or not having loaded yet. Keep every other word exactly as written: do not rephrase, "
    "summarise, translate, correct or add anything. If nothing in the thought refers to "
    "waiting or loading, return it unchanged.\n"
    "\n"
    "Reply with the edited thought only, without quotes, labels or explanation."
)

REWRITE_USER_TEMPLATE = (
    "Waits removed from this step's actions:\n{removed_from_step}\n"
    "\n"
    "Steps right before this one that only waited, now removed:\n{removed_steps_before}\n"
    "\n"
    "Thought:\n{thought}"
)

#: sha256 of the prompt's version and text, as canonical JSON.
REWRITE_PROMPT_DIGEST = hashlib.sha256(
    json.dumps(
        [REWRITE_PROMPT_VERSION, REWRITE_SYSTEM_PROMPT, REWRITE_USER_TEMPLATE],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
).hexdigest()

#: The shape of one cache entry (``<cache>/<key>.json``).
CACHE_FORMAT = "af-thought-rewrite-1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _capability(key: str) -> str:
    """The capability a fully-qualified key belongs to: its first segment."""
    return key.partition(".")[0]


# ── which waits go ───────────────────────────────────────────


def capped_capabilities(trace: RunTrace, step: int, index: Mapping[str, Any]) -> frozenset[str]:
    """The capabilities whose capture state at *step* says the settle wait was capped.

    *index* is the step's fragment index as the trace manifest lists it.
    A capture-state fragment is any whose registered class is a
    :class:`CaptureStateFragment`, whatever the platform; one that was not
    recorded, failed, or reports no settle wait is no evidence, and its
    capability is not in the answer.
    """
    capped: set[str] = set()
    for key in sorted(index):
        cls = ObservationRegistrar.get_or_none(key)
        if not (isinstance(cls, type) and issubclass(cls, CaptureStateFragment)):
            continue
        if index[key].failed:
            continue
        fragment = trace.observation_fragment(step, key)
        if not isinstance(fragment, CaptureStateFragment):
            continue
        settle = fragment.settle_state
        if settle is not None and settle.outcome == "capped":
            capped.add(_capability(key))
    return frozenset(capped)


@dataclass(frozen=True)
class StepWaits:
    """The waits removed from one recorded step.

    Attributes:
        record: A copy of the step without them: its ``actions`` and
            ``action_results`` lose the same entries; a skipped tail is never
            touched, so ``skipped_actions`` still counts it.
        removed: The removed waits, in recorded order.
    """

    record: StepRecord
    removed: tuple[RemovedAction, ...]

    @property
    def removes_step(self) -> bool:
        """Every action of the step was a removed wait: the step leaves its trajectory."""
        return not self.record.actions


def capped_waits(trace: RunTrace) -> dict[int, StepWaits]:
    """Per exportable step of *trace* with a capped capture, the waits that go.

    Only steps the trace is complete through, and only exportable ones
    (``is_exportable``): a step that is no sample changes nothing. On such a
    step every action that ran, succeeded, is a wait (``is_wait_action``, by
    the name the guard qualified it with) and belongs to a capability whose
    capture was capped is removed. A failed wait stays — its failure is why
    the actions after it were skipped — and a wait in the skipped tail stays,
    because it never ran. A step without such a wait is absent from the map.
    """
    through = trace.manifest.complete_through
    entries = {entry.step: entry for entry in trace.manifest.steps if entry.step <= through}
    found: dict[int, StepWaits] = {}
    for record in trace.step_records:
        if record.step not in entries or not is_exportable(record):
            continue
        capped = capped_capabilities(trace, record.step, entries[record.step].fragments)
        if not capped:
            continue
        drop = [
            index
            for index, (action, result) in enumerate(
                zip(record.actions, record.action_results, strict=False)
            )
            if result.success and is_wait_action(action.name) and _capability(action.name) in capped
        ]
        if not drop:
            continue
        dropped = frozenset(drop)
        reduced = record.model_copy(
            update={
                "actions": [a for i, a in enumerate(record.actions) if i not in dropped],
                "action_results": [
                    r for i, r in enumerate(record.action_results) if i not in dropped
                ],
            },
            deep=True,
        )
        found[record.step] = StepWaits(
            record=reduced,
            removed=tuple(
                RemovedAction(
                    run_id=trace.run_id,
                    step=record.step,
                    action_index=index,
                    action=record.actions[index],
                )
                for index in drop
            ),
        )
    return found


def without_waits(sample: CanonicalStepSample, waits: StepWaits) -> CanonicalStepSample:
    """*sample* (canonical, not yet translated) without the removed waits, and saying so.

    Its target is the reduced step's (``target_actions``); its labels do not
    change, because a removed wait succeeded and was never in the skipped
    tail.
    """
    return sample.model_copy(
        update={
            "teacher_actions": target_actions(waits.record),
            "wait_removal": WaitRemoval(removed_actions=list(waits.removed)),
        }
    )


# ── rewriting thoughts ───────────────────────────────────────


def _action_lines(actions: Sequence[Action]) -> str:
    if not actions:
        return "(none)"
    return "\n".join(_canonical(action.model_dump(mode="json")) for action in actions)


class ThoughtRewriter:
    """The helper chat model rewriting thoughts, behind a content-addressed cache.

    A rewrite's key is the sha256 of the canonical JSON of its inputs: the
    original thought, the waits removed from its step and from the steps
    removed right before it (names and params), the model's identity and
    :data:`REWRITE_PROMPT_DIGEST`. An entry ``<cache>/<key>.json`` records
    those inputs and the rewritten text; a cached rewrite is reused without a
    call, a missing one is asked for once and written at once (to a temporary
    file, then renamed), so a later failure of the export does not cost the
    call again. One export asks for one key once.

    Args:
        model: The exporter's ``thought_rewrite_llms`` model.
        cache_dir: ``thought_rewrite_cache``, resolved.

    Attributes:
        cache_hits: Distinct rewrites read from the cache in this export.
        model_calls: Rewrites the model was asked for in this export.
    """

    def __init__(self, model: BaseChatModel, cache_dir: Path) -> None:
        require_inference_protocol(model, "chat", consumer="exporter.thought_rewrite_llms")
        self.model = model
        self.cache_dir = cache_dir
        self.identity = RewriteModel(
            model_type=str(model.model_type), provider=str(model.provider or ""), model=model.model
        )
        self.cache_hits = 0
        self.model_calls = 0
        self._done: dict[str, str] = {}

    async def rewrite(
        self, thought: str, removed_from_step: Sequence[Action], removed_before: Sequence[Action]
    ) -> ThoughtRewrite:
        """*thought*, rewritten as if the removed waits had never been needed."""
        inputs = {
            "prompt_digest": REWRITE_PROMPT_DIGEST,
            "model": self.identity.model_dump(mode="json"),
            "thought": thought,
            "removed_from_step": [a.model_dump(mode="json") for a in removed_from_step],
            "removed_steps_before": [a.model_dump(mode="json") for a in removed_before],
        }
        key = hashlib.sha256(_canonical(inputs).encode("utf-8")).hexdigest()
        rewritten = self._done.get(key)
        if rewritten is None:
            rewritten = self._read(key, inputs)
            if rewritten is None:
                rewritten = await self._ask(thought, removed_from_step, removed_before)
                self.model_calls += 1
                self._write(key, inputs, rewritten)
            else:
                self.cache_hits += 1
            self._done[key] = rewritten
        unchanged = rewritten == thought.strip()
        return ThoughtRewrite(
            original=thought,
            rewritten=thought if unchanged else rewritten,
            outcome="reviewed_unchanged" if unchanged else "rewritten",
            synthetic=not unchanged,
            model=self.identity,
            prompt_version=REWRITE_PROMPT_VERSION,
            prompt_digest=REWRITE_PROMPT_DIGEST,
            cache_key=key,
        )

    async def _ask(
        self, thought: str, removed_from_step: Sequence[Action], removed_before: Sequence[Action]
    ) -> str:
        user = REWRITE_USER_TEMPLATE.format(
            removed_from_step=_action_lines(removed_from_step),
            removed_steps_before=_action_lines(removed_before),
            thought=thought,
        )
        completion = await self.model.ainvoke(
            [SystemMessage(content=REWRITE_SYSTEM_PROMPT), UserMessage(content=user)]
        )
        reply = completion.completion
        return (reply if isinstance(reply, str) else str(reply)).strip()

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read(self, key: str, inputs: dict[str, Any]) -> str | None:
        path = self._path(key)
        if not path.is_file():
            return None
        name = path.name
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ThoughtRewriteCacheError(
                name, f"does not read back ({type(exc).__name__})"
            ) from exc
        if not isinstance(entry, dict) or entry.get("format") != CACHE_FORMAT:
            raise ThoughtRewriteCacheError(name, f"is not a {CACHE_FORMAT} entry")
        if entry.get("key") != key or entry.get("inputs") != inputs:
            raise ThoughtRewriteCacheError(name, "records other inputs than its name stands for")
        rewritten = entry.get("rewritten")
        if not isinstance(rewritten, str):
            raise ThoughtRewriteCacheError(name, "holds no rewritten text")
        return rewritten

    def _write(self, key: str, inputs: dict[str, Any], rewritten: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        entry = {
            "format": CACHE_FORMAT,
            "key": key,
            "prompt_version": REWRITE_PROMPT_VERSION,
            "inputs": inputs,
            "rewritten": rewritten,
        }
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(entry, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    async def aclose(self) -> None:
        """End the model's client use; cleanup trouble is logged, never raised."""
        for diagnostic in await close_models([self.model]):
            report_cleanup(diagnostic)


# ── per trajectory ───────────────────────────────────────────


class WaitShaper:
    """What ``drop_when_capped`` does to one trajectory before the student renders it.

    Args:
        rewriter: The thought rewriter, or ``None`` when there are no teacher
            thoughts to rewrite (``thought_source: none``).
    """

    def __init__(self, rewriter: ThoughtRewriter | None) -> None:
        self.rewriter = rewriter

    async def shape(self, steps: Sequence[TrajectoryStep]) -> list[TrajectoryStep]:
        """*steps* with history and thoughts as the removed waits leave them.

        A removed step (``TrajectoryStep.removed``) stays in the list, for the
        renderer to skip without ending its segment. Every sample's
        ``history_steps`` loses the removed steps. A sample whose step lost
        waits, or that is the first step kept after removed ones (a step
        that is no sample in between breaks the run), gets its
        ``wait_removal`` and, with a teacher thought, its thought rewritten.
        """
        removed_steps = frozenset(item.record.step for item in steps if item.removed)
        before: list[RemovedAction] = []
        shaped: list[TrajectoryStep] = []
        for item in steps:
            if item.removed:
                before.extend(item.removed)
                shaped.append(item)
                continue
            sample = item.sample
            follows, before = before, []
            if sample is None:
                shaped.append(item)
                continue
            if removed_steps & set(sample.history_steps):
                sample = sample.model_copy(
                    update={
                        "history_steps": [
                            step for step in sample.history_steps if step not in removed_steps
                        ]
                    }
                )
            own = sample.wait_removal.removed_actions if sample.wait_removal is not None else []
            if own or follows:
                sample = await self._rewritten(sample, own, follows)
            shaped.append(replace(item, sample=sample))
        return shaped

    async def _rewritten(
        self,
        sample: CanonicalStepSample,
        own: Sequence[RemovedAction],
        follows: Sequence[RemovedAction],
    ) -> CanonicalStepSample:
        rewrite: ThoughtRewrite | None = None
        thought = sample.thought
        if self.rewriter is not None and thought is not None:
            rewrite = await self.rewriter.rewrite(
                thought.text, [r.action for r in own], [r.action for r in follows]
            )
            thought = (
                ThoughtRecord(text=rewrite.rewritten, source_field=thought.source_field)
                if rewrite.rewritten
                else None
            )
        return sample.model_copy(
            update={
                "thought": thought,
                "wait_removal": WaitRemoval(
                    removed_actions=list(own),
                    removed_steps_before=list(follows),
                    thought=rewrite,
                ),
            }
        )


__all__ = [
    "CACHE_FORMAT",
    "REWRITE_PROMPT_DIGEST",
    "REWRITE_PROMPT_VERSION",
    "REWRITE_SYSTEM_PROMPT",
    "REWRITE_USER_TEMPLATE",
    "StepWaits",
    "ThoughtRewriter",
    "WaitShaper",
    "capped_capabilities",
    "capped_waits",
    "without_waits",
]
