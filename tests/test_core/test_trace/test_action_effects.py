"""A recorded action effect reads back as its own type, through the action registry.

``ActionResult.effect`` holds the concrete ``Effect`` model an action
declares. It is written whole wherever a result is written — a step's
``action_results``, a restored step's attempts and history, the
``loop.history`` fragment — and read back by looking ``effect_id`` up in the
existing action registry and validating with that action's ``Effect``
(``docs/architecture_design.md`` §4.5, "动作效果"). Nothing reads back as a
dict: a key no action answers to, an action without an ``Effect`` and a
payload the ``Effect`` refuses are each a ``TraceSchemaError``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.trace.effects import registered_effect, restore_effects
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import (
    Action,
    ActionDescriptor,
    ActionEffect,
    ActionResult,
    PointWriter,
    write_viewport_point,
)
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.prompt import RestoredHistory
from spectragent.schemas.restoration import RestorationCheck, RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import fragment, new_wal


class ProbeEffect(ActionEffect):
    """A test effect with the three kinds of fact: a value, a zero, and the two gaps."""

    moved: int | Literal["unknown", "unavailable"]
    target: str | Literal["unknown", "unavailable"] = "unknown"

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return f"Moved {self.moved}; target {self.target}."


def _register_probe_actions() -> None:
    """Two actions sharing one effect, and one that declares none (registered per test)."""

    @dataclass
    class ProbeAction(ActionBase):
        __registry_key__ = "probe.move"
        Effect = ProbeEffect

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Move.", **context)

    @dataclass
    class ProbeSibling(ActionBase):
        __registry_key__ = "probe.nudge"
        Effect = ProbeEffect

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Nudge.", **context)

    @dataclass
    class ProbeSilent(ActionBase):
        __registry_key__ = "probe.silent"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Do something unmeasured.", **context)


def _result(effect_id: str = "probe.move", **facts: Any) -> ActionResult:
    return ActionResult(success=True, effect=ProbeEffect(effect_id=effect_id, **facts))


def _record(step: int, results: list[ActionResult]) -> StepRecord:
    return StepRecord(
        step=step,
        actions=[Action(name=r.effect.effect_id if r.effect else "probe.silent") for r in results],
        action_results=results,
    )


def _write(tmp_path: Path, records: list[StepRecord], fragments: Any = ()) -> Path:
    wal = new_wal(tmp_path)
    for record in records:
        wal.commit_step(
            record.step,
            record,
            fragments=[fragment(record.step, value) for value in fragments],
        )
    return wal.run_dir


def test_effects_round_trip_through_the_trace_as_their_types(tmp_path: Path) -> None:
    _register_probe_actions()
    results = [
        _result(moved=300, target="page"),
        _result("probe.nudge", moved=0, target="page"),
        _result(moved="unknown"),
        _result(moved="unavailable", target="unavailable"),
        ActionResult(success=True),
    ]
    run_dir = _write(tmp_path, [_record(0, results)])

    read = RunTrace(run_dir).step(0)
    assert read is not None
    assert read.action_results == results
    effects = [r.effect for r in read.action_results]
    assert all(isinstance(e, ProbeEffect) for e in effects[:4])
    assert effects[4] is None
    assert [e.describe() for e in effects[:4] if e is not None] == [
        "Moved 300; target page.",
        "Moved 0; target page.",
        "Moved unknown; target unknown.",
        "Moved unavailable; target unavailable.",
    ]


def test_a_result_without_an_effect_writes_no_effect_key(tmp_path: Path) -> None:
    run_dir = _write(tmp_path, [_record(0, [ActionResult(success=True)])])
    written = json.loads((run_dir / "steps" / "000.json").read_text())
    assert "effect" not in written["action_results"][0]


def test_effects_inside_history_restoration_and_fragments_read_back(tmp_path: Path) -> None:
    _register_probe_actions()
    first = _result(moved=120)
    restored = _result(moved=0, target="element")
    move = Action(name="probe.move")
    history = RestoredHistory(
        source_run_id="source",
        source_step=1,
        source_actions=[move],
        actions=[move],
        results=[restored],
        executions=[
            RestoredAction(
                recorded=move,
                executed=move,
                method="identity",
                recorded_success=True,
                result=restored,
                live_success=True,
            )
        ],
    )
    record = StepRecord(
        step=1,
        origin="restored",
        actions=[move],
        action_results=[restored],
        source_history=history,
        restoration=RestorationCheck(step=1, verdict="ok", actions=list(history.executions)),
    )
    run_dir = _write(
        tmp_path,
        [_record(0, [first]), record],
        fragments=[HistoryObs(items=[_record(0, [first])])],
    )
    trace = RunTrace(run_dir)
    step = trace.step(1)
    assert step is not None and step == record
    assert step.source_history is not None
    assert isinstance(step.source_history.results[0].effect, ProbeEffect)
    assert step.restoration is not None
    assert isinstance(step.restoration.actions[0].result.effect, ProbeEffect)  # type: ignore[union-attr]
    fragment_back = trace.observation_fragment(1, "loop.history")
    assert isinstance(fragment_back, HistoryObs)
    assert fragment_back.items[0].action_results[0] == first
    assert isinstance(fragment_back.items[0].action_results[0].effect, ProbeEffect)


def _corrupt(run_dir: Path, mutate: Any) -> None:
    path = run_dir / "steps" / "000.json"
    document = json.loads(path.read_text())
    mutate(document["action_results"][0]["effect"])
    path.write_text(json.dumps(document))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda e: e.update(effect_id="probe.unregistered"), "names no registered action"),
        (lambda e: e.update(effect_id="probe.silent"), "declares no Effect"),
        (lambda e: e.update(moved="a lot"), "ProbeEffect"),
        (lambda e: e.update(surprise=1), "surprise"),
        (lambda e: e.pop("effect_id"), "no effect_id"),
    ],
)
def test_an_effect_that_does_not_read_back_is_a_trace_schema_error(
    tmp_path: Path, mutate: Any, match: str
) -> None:
    _register_probe_actions()
    run_dir = _write(tmp_path, [_record(0, [_result(moved=1)])])
    _corrupt(run_dir, mutate)
    with pytest.raises(TraceSchemaError, match=match) as caught:
        RunTrace(run_dir).step(0)
    assert "000.json" in str(caught.value)


def test_a_mapping_is_never_accepted_as_an_effect() -> None:
    with pytest.raises(ValidationError, match="ActionEffect instance"):
        ActionResult.model_validate(
            {"success": True, "effect": {"effect_id": "probe.move", "moved": 1}}
        )


def test_the_resolver_finds_a_shared_effect_by_either_action() -> None:
    _register_probe_actions()
    assert registered_effect("probe.move") is ProbeEffect
    assert registered_effect("probe.nudge") is ProbeEffect
    with pytest.raises(ValueError, match="declares no Effect"):
        registered_effect("probe.silent")


def test_restoring_walks_the_schema_not_the_key_names() -> None:
    """A mapping under a field that cannot hold an effect is left for the model to judge."""

    class Elsewhere(BaseModel):
        effect: dict[str, Any]
        result: ActionResult | None = None

    data = {"effect": {"effect_id": "probe.move", "moved": 1}, "result": None}
    assert restore_effects(Elsewhere, data, registered_effect) == data
