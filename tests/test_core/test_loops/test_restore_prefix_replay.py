"""``prefix_replay``: re-execute the recorded prefix, verifying each step (§4.1 "恢复器").

The restorer restores the environment only. Per step ``k < t``: one observe
(one cycle), verify against the recorded observation, then retarget and
execute each action the source executed and compare its outcome; finally one
more observe for the checkpoint step *t*, verified the same way. Each step is
handed back as it is restored. Under ``strict`` the first failed check raises
before any further action; under ``warn`` it is recorded and restoration goes
on. Each ``RestorationCheck.reason`` comes from its own case here, and the
precedence when several fire. ``pace: recorded_timeline`` is driven on a
fake clock: its observation and action waits from recorded timestamps, the
lead taken from the previous observe, no wait when behind, and its refusal of
a source whose action starts were not measured.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any, ClassVar, Literal

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops.errors import RunAbort
from spectragent.core.loops.restore import (
    PrefixReplayRestorer,
    RestorationContext,
    RestorationDivergedError,
    RestoredStep,
    prefix_replay,
)
from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.restoration import (
    REASON_ORDER,
    RestorationEvidence,
    Retarget,
)
from spectragent.schemas.step_record import ActionSpan, StepRecord
from tests._helpers.assembly import make_env


def _record(
    step: int,
    *,
    actions: int = 1,
    success: bool = True,
    latency: int | None = None,
    name: str = "cap.click",
) -> StepRecord:
    return StepRecord(
        step=step,
        actions=[Action(name=name, params={"index": step * 10 + i}) for i in range(actions)],
        action_results=[ActionResult(success=success) for _ in range(actions)],
        llm_latency_ms=latency,
    )


class _Source:
    """Recorded steps and observations, by step — what ``SourceRun`` serves."""

    name = "run 'src' (fake)"

    def __init__(self, records: list[StepRecord]) -> None:
        self.records = records

    def record(self, step: int) -> StepRecord:
        return self.records[step]

    def observation(self, step: int, negotiated: Any) -> Any:
        return f"recorded-{step}"


class _Env:
    """Logs every call; evidence and retargets are scripted per step."""

    def __init__(
        self,
        log: list[tuple[Any, ...]],
        *,
        evidence: dict[int, RestorationEvidence] | None = None,
        retargets: dict[int, Retarget] | None = None,
        observe_raises: BaseException | None = None,
    ) -> None:
        self.log = log
        self.evidence = evidence or {}
        self.retargets = retargets or {}
        self.observe_raises = observe_raises
        self.observed = 0

    async def observe(self, negotiated: Any) -> Any:
        self.log.append(("observe", self.observed))
        if self.observe_raises is not None:
            raise self.observe_raises
        self.observed += 1
        return f"live-{self.observed - 1}"

    async def align_next_observation(
        self, recorded: Any, *, media_seek_timeout_s: float, media_end_lead_s: float
    ) -> None:
        self.log.append(("align", recorded, media_seek_timeout_s, media_end_lead_s))

    async def verify_restoration(self, recorded: Any, restored: Any) -> list[RestorationEvidence]:
        self.log.append(("verify", recorded, restored))
        step = int(str(recorded).rsplit("-", 1)[1])
        item = self.evidence.get(step)
        return [item] if item is not None else []

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        self.log.append(("retarget", action.params["index"], recorded, restored))
        scripted = self.retargets.get(action.params["index"])
        if scripted is not None:
            return scripted
        return Retarget(
            action=Action(name=action.name, params={"index": action.params["index"] + 1000}),
            method="geometry_signature",
        )


def _act(log: list[tuple[Any, ...]], *, results: dict[int, Any] | None = None):
    scripted = results or {}

    async def act(action: Action) -> ActionResult:
        log.append(("act", action.params["index"]))
        outcome = scripted.get(action.params["index"])
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        return ActionResult(success=True)

    return act


def _ctx(env: Any, act_fn: Any, source: Any, t: int) -> RestorationContext:
    return RestorationContext(
        env=env, act_fn=act_fn, negotiated_obs=object(), source=source, checkpoint_step=t
    )


async def _collect(restorer: PrefixReplayRestorer, ctx: RestorationContext) -> list[RestoredStep]:
    return [item async for item in restorer.restore(ctx)]


class TestOrder:
    async def test_call_order(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log)
        source = _Source([_record(0, actions=2), _record(1)])
        steps = await _collect(PrefixReplayRestorer(), _ctx(env, _act(log), source, 2))

        assert log == [
            ("align", "recorded-0", 2.0, 0.5),
            ("observe", 0),
            ("verify", "recorded-0", "live-0"),
            ("retarget", 0, "recorded-0", "live-0"),
            ("act", 1000),
            ("retarget", 1, "recorded-0", "live-0"),
            ("act", 1001),
            ("align", "recorded-1", 2.0, 0.5),
            ("observe", 1),
            ("verify", "recorded-1", "live-1"),
            ("retarget", 10, "recorded-1", "live-1"),
            ("act", 1010),
            ("align", "recorded-2", 2.0, 0.5),
            ("observe", 2),
            ("verify", "recorded-2", "live-2"),
        ]
        assert [(s.step, s.observation, s.check.verdict) for s in steps] == [
            (0, "live-0", "ok"),
            (1, "live-1", "ok"),
            (2, "live-2", "ok"),
        ]
        executed = steps[0].check.actions
        assert [
            (a.recorded.params, a.executed.params if a.executed else None) for a in executed
        ] == [
            ({"index": 0}, {"index": 1000}),
            ({"index": 1}, {"index": 1001}),
        ]

    async def test_a_checkpoint_at_step_zero_only_observes(self) -> None:
        log: list[tuple[Any, ...]] = []
        steps = await _collect(PrefixReplayRestorer(), _ctx(_Env(log), _act(log), _Source([]), 0))
        assert log == [
            ("align", "recorded-0", 2.0, 0.5),
            ("observe", 0),
            ("verify", "recorded-0", "live-0"),
        ]
        assert [s.step for s in steps] == [0]
        assert steps[0].check.actions == []

    async def test_only_actions_the_source_executed_run(self) -> None:
        log: list[tuple[Any, ...]] = []
        record = _record(0, actions=2)
        record.action_results = record.action_results[:1]
        await _collect(PrefixReplayRestorer(), _ctx(_Env(log), _act(log), _Source([record]), 1))
        assert [entry for entry in log if entry[0] == "act"] == [("act", 1000)]

    async def test_obs_t_verified(self) -> None:
        log: list[tuple[Any, ...]] = []
        evidence = {1: RestorationEvidence(capability_id="cap", url_match=True)}
        steps = await _collect(
            PrefixReplayRestorer(),
            _ctx(_Env(log, evidence=evidence), _act(log), _Source([_record(0)]), 1),
        )
        last = steps[-1]
        assert last.step == 1
        assert last.check.evidence == [evidence[1]]
        assert last.check.actions == []


class TestSkippedActions:
    """A step its Loop stopped at a failed action (the user's ruling of 2026-10-24).

    The source never ran the actions after the failed one, so neither does
    the restoration. When it reproduces the stop, it hands them back
    retargeted onto the restored page, never run.
    """

    @staticmethod
    def _stopped(step: int = 0) -> StepRecord:
        """Three actions: the first ran, the second failed, the third was skipped."""
        return StepRecord(
            step=step,
            actions=[Action(name="cap.click", params={"index": step * 10 + i}) for i in range(3)],
            action_results=[ActionResult(success=True), ActionResult(success=False)],
            skipped_actions=1,
        )

    async def test_a_skipped_action_is_retargeted_and_not_run(self) -> None:
        log: list[tuple[Any, ...]] = []
        act = _act(log, results={1001: ActionResult(success=False, error="covered")})
        steps = await _collect(
            PrefixReplayRestorer(), _ctx(_Env(log), act, _Source([self._stopped()]), 1)
        )
        assert [entry for entry in log if entry[0] == "act"] == [("act", 1000), ("act", 1001)]
        assert [entry[1] for entry in log if entry[0] == "retarget"] == [0, 1, 2]
        restored = steps[0]
        assert restored.check.verdict == "ok"
        assert [a.recorded.params["index"] for a in restored.check.actions] == [0, 1]
        assert restored.skipped == (Action(name="cap.click", params={"index": 1002}),)

    async def test_no_skipped_tail_when_the_last_action_succeeded_here(self) -> None:
        """``warn``: the outcome differed, so the reason the source skipped is not this run's."""
        log: list[tuple[Any, ...]] = []
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(_Env(log), _act(log), _Source([self._stopped()]), 1),
        )
        assert [entry for entry in log if entry[0] == "act"] == [("act", 1000), ("act", 1001)]
        assert steps[0].check.reasons == ["action_failed"]
        assert steps[0].skipped == ()

    async def test_no_skipped_tail_when_a_skipped_action_has_no_target_here(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(
            log,
            retargets={2: Retarget(action=None, method="geometry_signature", failure="not_found")},
        )
        act = _act(log, results={1001: ActionResult(success=False, error="covered")})
        steps = await _collect(
            PrefixReplayRestorer(), _ctx(env, act, _Source([self._stopped()]), 1)
        )
        # Never run, so never a divergence.
        assert steps[0].check.verdict == "ok"
        assert steps[0].skipped == ()

    async def test_a_step_recorded_before_the_stop_reruns_every_action(self) -> None:
        """The old Loop ran the actions after a failed one; the restoration runs them too."""
        log: list[tuple[Any, ...]] = []
        old = StepRecord(
            step=0,
            actions=[Action(name="cap.click", params={"index": i}) for i in range(2)],
            action_results=[ActionResult(success=False), ActionResult(success=True)],
        )
        act = _act(log, results={1000: ActionResult(success=False, error="covered")})
        steps = await _collect(PrefixReplayRestorer(), _ctx(_Env(log), act, _Source([old]), 1))
        assert [entry for entry in log if entry[0] == "act"] == [("act", 1000), ("act", 1001)]
        assert steps[0].check.verdict == "ok"
        assert steps[0].skipped == ()


_FRAG = "test_restore_frame"


class _Frame(FragmentBase):
    __registry_key__ = _FRAG
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    cycle: int


class _CycleCap(CapabilityBase):
    __registry_key__ = "restore_cycle_cap"
    __wiring__ = {"observation": ("observation", [_FRAG])}

    cycles: int

    async def begin_observation(self, cycle: int) -> None:
        self.cycles = cycle

    async def resolve_fragment(self, frag_key: str, **kw: Any) -> Any:
        return _Frame(cycle=self.cycles)

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        return ActionResult(success=True)


async def test_one_cycle_per_observe() -> None:
    """``t`` restored steps and the checkpoint: ``t + 1`` cycles, actions open none."""
    cap = _CycleCap()
    cap.cycles = 0
    env = make_env(EnvironmentBase, {"restore_cycle_cap": cap})
    negotiated = ObservationNegotiationResult(
        active_fragments=frozenset({_FRAG}),
        required_fragments=frozenset({_FRAG}),
        env_sourced=frozenset({_FRAG}),
        loop_sourced=frozenset(),
    )

    class _CycleSource(_Source):
        def observation(self, step: int, negotiated: Any) -> Any:
            return None

    name = "restore_cycle_cap.click"
    source = _CycleSource([_record(0, actions=2, name=name), _record(1, actions=3, name=name)])
    ctx = RestorationContext(
        env=env,
        act_fn=env.act,
        negotiated_obs=negotiated,
        source=source,  # type: ignore[arg-type]
        checkpoint_step=2,
    )
    steps = await _collect(PrefixReplayRestorer(), ctx)
    assert cap.cycles == 3
    assert [s.observation.test_restore_frame.cycle for s in steps] == [1, 2, 3]
    # No capability implements RestorationSupport: identity, nothing measured.
    assert all(s.check.verdict == "ok" and s.check.evidence == [] for s in steps)
    assert {a.method for s in steps for a in s.check.actions} == {"identity"}


class TestReasons:
    """Each reason from its own case, under ``warn`` so the check is handed back."""

    @pytest.mark.parametrize(
        ("reason", "evidence", "retargets", "results"),
        [
            pytest.param(
                "document_miss",
                RestorationEvidence(capability_id="cap", document_misses=2),
                {},
                {},
                id="document_miss",
            ),
            pytest.param(
                "url_mismatch",
                RestorationEvidence(
                    capability_id="cap",
                    url_match=False,
                    recorded_url="https://a.test/",
                    restored_url="https://b.test/",
                ),
                {},
                {},
                id="url_mismatch",
            ),
            pytest.param(
                "retarget_not_found",
                None,
                {0: Retarget(action=None, method="geometry_signature", failure="not_found")},
                {},
                id="retarget_not_found",
            ),
            pytest.param(
                "retarget_ambiguous",
                None,
                {
                    0: Retarget(
                        action=None,
                        method="geometry_signature",
                        failure="ambiguous",
                        detail="2 candidates",
                    )
                },
                {},
                id="retarget_ambiguous",
            ),
            pytest.param(
                "action_failed",
                None,
                {},
                {1000: ActionResult(success=False, error="element detached")},
                id="action_failed",
            ),
            pytest.param(
                "signature_mismatch",
                RestorationEvidence(
                    capability_id="cap", signature_match=False, signature_missing=1
                ),
                {},
                {},
                id="signature_mismatch",
            ),
            pytest.param(
                "screenshot_distance",
                RestorationEvidence(capability_id="cap", screenshot_distance=9),
                {},
                {},
                id="screenshot_distance",
            ),
        ],
    )
    async def test_each_reason(
        self,
        reason: str,
        evidence: RestorationEvidence | None,
        retargets: dict[int, Retarget],
        results: dict[int, Any],
    ) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log, evidence={0: evidence} if evidence else None, retargets=retargets)
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(env, _act(log, results=results), _Source([_record(0)]), 1),
        )
        check = steps[0].check
        assert (check.verdict, check.reason, check.reasons) == ("warn", reason, [reason])
        # The checkpoint step itself passed.
        assert steps[1].check.verdict == "ok"

    async def test_an_act_exception_is_an_action_failure(self) -> None:
        log: list[tuple[Any, ...]] = []
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(
                _Env(log),
                _act(log, results={1000: RuntimeError("socket closed")}),
                _Source([_record(0)]),
                1,
            ),
        )
        (restored,) = steps[0].check.actions
        assert (restored.live_success, restored.live_error) == (
            False,
            "RuntimeError: socket closed",
        )
        assert steps[0].check.reasons == ["action_failed"]

    async def test_a_recorded_failure_reproduced_is_not_a_failure(self) -> None:
        log: list[tuple[Any, ...]] = []
        steps = await _collect(
            PrefixReplayRestorer(),
            _ctx(
                _Env(log),
                _act(log, results={1000: ActionResult(success=False, error="refused")}),
                _Source([_record(0, success=False)]),
                1,
            ),
        )
        assert steps[0].check.verdict == "ok"

    async def test_unmeasured_never_fails(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log, evidence={0: RestorationEvidence(capability_id="cap")})
        steps = await _collect(
            PrefixReplayRestorer(), _ctx(env, _act(log), _Source([_record(0)]), 1)
        )
        assert steps[0].check.verdict == "ok"

    async def test_the_screenshot_threshold_is_inclusive(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(
            log, evidence={0: RestorationEvidence(capability_id="cap", screenshot_distance=5)}
        )
        steps = await _collect(
            PrefixReplayRestorer(), _ctx(env, _act(log), _Source([_record(0)]), 1)
        )
        assert steps[0].check.verdict == "ok"

    async def test_precedence(self) -> None:
        log: list[tuple[Any, ...]] = []
        evidence = RestorationEvidence(
            capability_id="cap",
            document_misses=1,
            url_match=False,
            signature_match=False,
            screenshot_distance=30,
        )
        env = _Env(
            log,
            evidence={0: evidence},
            retargets={0: Retarget(action=None, method="geometry_signature", failure="ambiguous")},
        )
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(
                env,
                _act(log, results={0: ActionResult(success=False)}),
                _Source([_record(0)]),
                1,
            ),
        )
        check = steps[0].check
        assert check.reasons == [
            "document_miss",
            "url_mismatch",
            "retarget_ambiguous",
            "action_failed",
            "signature_mismatch",
            "screenshot_distance",
        ]
        assert check.reasons == [r for r in REASON_ORDER if r in check.reasons]
        assert check.reason == "document_miss"


class TestPolicy:
    async def test_strict_raises_with_the_check_and_observation(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log, evidence={1: RestorationEvidence(capability_id="cap", url_match=False)})
        source = _Source([_record(0), _record(1), _record(2)])
        seen: list[RestoredStep] = []
        with pytest.raises(RestorationDivergedError) as caught:
            async for item in PrefixReplayRestorer().restore(_ctx(env, _act(log), source, 3)):
                seen.append(item)
        assert [s.step for s in seen] == [0]
        assert caught.value.check.step == 1
        assert caught.value.check.verdict == "diverged"
        assert caught.value.check.reasons == ["url_mismatch"]
        assert caught.value.observation == "live-1"
        # A world that already left the recording is not acted on.
        assert ("act", 1010) not in log

    async def test_strict_does_not_execute_an_action_it_could_not_retarget(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(
            log,
            retargets={0: Retarget(action=None, method="geometry_signature", failure="not_found")},
        )
        with pytest.raises(RestorationDivergedError) as caught:
            await _collect(PrefixReplayRestorer(), _ctx(env, _act(log), _Source([_record(0)]), 1))
        assert [entry for entry in log if entry[0] == "act"] == []
        (restored,) = caught.value.check.actions
        assert restored.executed is None
        assert restored.failure == "not_found"

    @pytest.mark.parametrize("failing", [0, 1])
    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(ActionResult(success=False, error="no such element"), id="mismatch"),
            pytest.param(RuntimeError("element detached"), id="raised"),
        ],
    )
    async def test_strict_stops_at_the_first_failed_action(
        self, outcome: Any, failing: int
    ) -> None:
        """The step's later actions never run on a world an action already left."""
        log: list[tuple[Any, ...]] = []
        with pytest.raises(RestorationDivergedError) as caught:
            await _collect(
                PrefixReplayRestorer(),
                _ctx(
                    _Env(log),
                    _act(log, results={1000 + failing: outcome}),
                    _Source([_record(0, actions=3)]),
                    1,
                ),
            )
        ran = list(range(failing + 1))
        assert [entry for entry in log if entry[0] == "act"] == [("act", 1000 + i) for i in ran]
        assert [entry[1] for entry in log if entry[0] == "retarget"] == ran
        check = caught.value.check
        assert (check.step, check.verdict, check.reasons) == (0, "diverged", ["action_failed"])
        assert [a.recorded.params["index"] for a in check.actions] == ran
        assert check.actions[-1].live_success is False
        assert caught.value.observation == "live-0"

    async def test_warn_runs_the_rest_of_the_step_after_a_failed_action(self) -> None:
        log: list[tuple[Any, ...]] = []
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(
                _Env(log),
                _act(log, results={1000: ActionResult(success=False, error="no such element")}),
                _Source([_record(0, actions=3)]),
                1,
            ),
        )
        assert [entry for entry in log if entry[0] == "act"] == [
            ("act", 1000),
            ("act", 1001),
            ("act", 1002),
        ]
        check = steps[0].check
        assert (check.verdict, check.reasons, len(check.actions)) == (
            "warn",
            ["action_failed"],
            3,
        )

    async def test_warn_executes_the_recorded_action_unchanged(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(
            log,
            retargets={0: Retarget(action=None, method="geometry_signature", failure="not_found")},
        )
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(env, _act(log), _Source([_record(0)]), 1),
        )
        assert ("act", 0) in log
        (restored,) = steps[0].check.actions
        assert restored.executed == Action(name="cap.click", params={"index": 0})
        assert steps[0].check.verdict == "warn"

    async def test_warn_continues_past_a_failed_checkpoint(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log, evidence={1: RestorationEvidence(capability_id="cap", url_match=False)})
        steps = await _collect(
            PrefixReplayRestorer(verify="warn"),
            _ctx(env, _act(log), _Source([_record(0)]), 1),
        )
        assert steps[-1].check.verdict == "warn"

    async def test_strict_diverges_on_the_checkpoint_observation(self) -> None:
        log: list[tuple[Any, ...]] = []
        env = _Env(log, evidence={1: RestorationEvidence(capability_id="cap", url_match=False)})
        with pytest.raises(RestorationDivergedError) as caught:
            await _collect(PrefixReplayRestorer(), _ctx(env, _act(log), _Source([_record(0)]), 1))
        assert caught.value.check.step == 1
        assert caught.value.observation == "live-1"


class TestPace:
    async def test_sleeps_the_recorded_model_latency(self, monkeypatch) -> None:  # noqa: ANN001
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("spectragent.core.loops.restore.prefix_replay.asyncio.sleep", _sleep)
        log: list[tuple[Any, ...]] = []
        source = _Source([_record(0, latency=1500), _record(1, latency=None)])
        await _collect(
            PrefixReplayRestorer(pace="recorded_llm_latency"),
            _ctx(_Env(log), _act(log), source, 2),
        )
        assert slept == [1.5]

    async def test_no_pace_no_sleep(self, monkeypatch) -> None:  # noqa: ANN001
        slept: list[float] = []

        async def _sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("spectragent.core.loops.restore.prefix_replay.asyncio.sleep", _sleep)
        log: list[tuple[Any, ...]] = []
        await _collect(
            PrefixReplayRestorer(),
            _ctx(_Env(log), _act(log), _Source([_record(0, latency=1500)]), 1),
        )
        assert slept == []


class TestVetoes:
    async def test_a_veto_on_observe_propagates(self) -> None:
        veto = RunAbort("banned page", source="guard")
        log: list[tuple[Any, ...]] = []
        with pytest.raises(RunAbort) as caught:
            await _collect(
                PrefixReplayRestorer(),
                _ctx(_Env(log, observe_raises=veto), _act(log), _Source([_record(0)]), 1),
            )
        assert caught.value is veto

    async def test_a_veto_on_act_propagates(self) -> None:
        veto = RunAbort("risk-control interstitial", source="guard")
        log: list[tuple[Any, ...]] = []
        with pytest.raises(RunAbort) as caught:
            await _collect(
                PrefixReplayRestorer(verify="warn"),
                _ctx(_Env(log), _act(log, results={1000: veto}), _Source([_record(0)]), 1),
            )
        assert caught.value is veto

    async def test_a_cancellation_is_not_an_action_failure(self) -> None:
        log: list[tuple[Any, ...]] = []
        with pytest.raises(asyncio.CancelledError):
            await _collect(
                PrefixReplayRestorer(verify="warn"),
                _ctx(
                    _Env(log),
                    _act(log, results={1000: asyncio.CancelledError()}),
                    _Source([_record(0)]),
                    1,
                ),
            )


class TestTheContext:
    def test_context_has_no_llm_or_memory(self) -> None:
        names = {f.name for f in dataclasses.fields(RestorationContext)}
        assert names == {"env", "act_fn", "negotiated_obs", "source", "checkpoint_step"}

    def test_the_context_is_frozen(self) -> None:
        ctx = _ctx(object(), object(), object(), 0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.checkpoint_step = 1  # type: ignore[misc]

    def test_the_restorer_is_stateless(self) -> None:
        """Knobs only: nothing per run lives on the restorer."""
        restorer = PrefixReplayRestorer()
        assert {f.name for f in dataclasses.fields(restorer)} == {
            "verify",
            "screenshot_max_distance",
            "pace",
            "media",
            "media_time_tolerance_s",
            "media_seek_timeout_s",
            "media_end_lead_s",
        }


class TestMediaAlignment:
    """``media: align`` requests an alignment before every restored observation, and nothing more."""

    async def test_media_align_is_the_default_and_covers_the_checkpoint(self) -> None:
        log: list[tuple[Any, ...]] = []
        restorer = PrefixReplayRestorer(media_seek_timeout_s=0.5, media_end_lead_s=0.25)
        assert restorer.media == "align" and restorer.media_time_tolerance_s == 1.0
        await _collect(restorer, _ctx(_Env(log), _act(log), _Source([_record(0)]), 1))
        aligned = [entry for entry in log if entry[0] == "align"]
        assert aligned == [
            ("align", "recorded-0", 0.5, 0.25),
            ("align", "recorded-1", 0.5, 0.25),
        ]
        # Each request is followed by the observation it belongs to, and nothing after t asks.
        assert [entry[0] for entry in log][-3:] == ["align", "observe", "verify"]

    async def test_media_none_requests_nothing(self) -> None:
        log: list[tuple[Any, ...]] = []
        await _collect(
            PrefixReplayRestorer(media="none"),
            _ctx(_Env(log), _act(log), _Source([_record(0)]), 1),
        )
        assert [entry for entry in log if entry[0] == "align"] == []

    async def test_the_media_tolerance_decides_a_position_difference(self) -> None:
        from spectragent.schemas.restoration import MediaComparison, MediaEvidence

        media = MediaEvidence(
            comparisons=[
                MediaComparison(
                    element="main#0 video",
                    correspondence="matched",
                    reading="before",
                    recorded_time=41.0,
                    restored_time=10.0,
                    recorded_paused=True,
                    restored_paused=True,
                )
            ]
        )
        evidence = {0: RestorationEvidence(capability_id="cap", media=media)}

        async def verdict(tolerance: float) -> list[Any]:
            log: list[tuple[Any, ...]] = []
            steps = await _collect(
                PrefixReplayRestorer(verify="warn", media_time_tolerance_s=tolerance),
                _ctx(_Env(log, evidence=evidence), _act(log), _Source([_record(0)]), 1),
            )
            return steps[0].check.reasons

        assert await verdict(1.0) == ["media_mismatch"]
        assert await verdict(60.0) == []


# ── pace: recorded_timeline ──────────────────────────────────────────


class _Clock:
    """One fake timebase for the wall clock, the monotonic clock and sleep: sleeping moves it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(prefix_replay, "time", clock)
    monkeypatch.setattr("spectragent.core.loops.restore.prefix_replay.asyncio.sleep", clock.sleep)
    return clock


#: One env-source fragment, so an observation's capture time is its ``captured_at``.
_TIMED = ObservationNegotiationResult(
    active_fragments=frozenset({_FRAG}),
    required_fragments=frozenset({_FRAG}),
    env_sourced=frozenset({_FRAG}),
    loop_sourced=frozenset(),
)


def _observation(fragment: Any) -> SimpleNamespace:
    return SimpleNamespace(**{_FRAG: fragment})


def _captured(cycle: int, at: float) -> SimpleNamespace:
    return _observation(_Frame(cycle=cycle, captured_at=at))


class _TimedSource(_Source):
    """Recorded observations captured at the given wall-clock times, one per step."""

    def __init__(self, records: list[StepRecord], captures: list[float]) -> None:
        super().__init__(records)
        self.captures = captures

    def observation(self, step: int, negotiated: Any) -> Any:
        return _captured(step, self.captures[step])


class _TimedEnv(_Env):
    """Each observe takes its scripted time on the clock and captures *capture_after_s* in."""

    def __init__(
        self,
        log: list[tuple[Any, ...]],
        clock: _Clock,
        *,
        observe_s: list[float],
        capture_after_s: float,
    ) -> None:
        super().__init__(log)
        self.clock = clock
        self.observe_s = observe_s
        self.capture_after_s = capture_after_s

    async def observe(self, negotiated: Any) -> Any:
        cycle = self.observed
        self.observed += 1
        self.log.append(("observe", cycle, self.clock.now))
        captured = self.clock.now + self.capture_after_s
        self.clock.now += self.observe_s[cycle]
        return _captured(cycle, captured)

    async def verify_restoration(self, recorded: Any, restored: Any) -> list[RestorationEvidence]:
        return []


def _timed_act(log: list[tuple[Any, ...]], clock: _Clock, *, act_s: float = 1.0):
    async def act(action: Action) -> ActionResult:
        log.append(("act", action.params["index"], clock.now))
        clock.now += act_s
        return ActionResult(success=True)

    return act


def _spanned(step: int, starts: list[float] | None, *, actions: int | None = None) -> StepRecord:
    """A recorded step whose ``env.act`` calls started at *starts* (``None``: not measured)."""
    record = _record(step, actions=len(starts or []) if actions is None else actions)
    record.action_spans = (
        None
        if starts is None
        else [
            ActionSpan(action="cap.click", started_at=at, ended_at=at + 0.5, outcome="returned")
            for at in starts
        ]
    )
    return record


def _timed_ctx(env: Any, act_fn: Any, source: Any, t: int) -> RestorationContext:
    return RestorationContext(
        env=env, act_fn=act_fn, negotiated_obs=_TIMED, source=source, checkpoint_step=t
    )


def _timeline() -> PrefixReplayRestorer:
    return PrefixReplayRestorer(pace="recorded_timeline", media="none")


def _starts(log: list[tuple[Any, ...]], kind: str) -> list[float]:
    return [entry[2] for entry in log if entry[0] == kind]


class TestRecordedTimeline:
    """Anchored on the two step-0 captures; observations lead by the last observe; never hurried."""

    async def test_observations_and_first_actions_land_on_the_source_timeline(
        self, clock: _Clock
    ) -> None:
        # Source: captures at 100 / 120 / 150 (the checkpoint), first actions at 105 / 130.
        source = _TimedSource([_spanned(0, [105.0]), _spanned(1, [130.0])], [100.0, 120.0, 150.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[2.0, 3.0, 1.0], capture_after_s=1.5)
        steps = await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 2))

        # Live anchor 1001.5 (observe 0 started at 1000, captured 1.5 s in).
        assert _starts(log, "act") == pytest.approx([1001.5 + 5, 1001.5 + 30])
        # Observation k starts at its target less the previous observe's duration.
        assert _starts(log, "observe") == pytest.approx(
            [1000.0, 1001.5 + 20 - 2.0, 1001.5 + 50 - 3.0]
        )
        assert clock.slept == pytest.approx([4.5, 12.0, 9.0, 16.0])
        assert [(s.step, s.check.verdict) for s in steps] == [(0, "ok"), (1, "ok"), (2, "ok")]

    async def test_each_observation_leads_by_how_long_the_previous_observe_took(
        self, clock: _Clock
    ) -> None:
        source = _TimedSource([_spanned(0, []), _spanned(1, [])], [100.0, 110.0, 130.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[4.0, 7.0, 1.0], capture_after_s=1.0)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 2))

        # Anchor 1001; targets 1011 and 1031; leads 4 s (observe 0) and 7 s (observe 1).
        assert _starts(log, "observe") == pytest.approx([1000.0, 1011 - 4.0, 1031 - 7.0])
        assert clock.slept == pytest.approx([3.0, 10.0])

    async def test_a_restoration_behind_the_source_never_sleeps(self, clock: _Clock) -> None:
        source = _TimedSource([_spanned(0, [100.2]), _spanned(1, [100.7])], [100.0, 100.5, 101.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[2.0, 2.0, 2.0], capture_after_s=0.5)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 2))

        assert clock.slept == []
        # Back to back: observe 2 s, act 1 s, with no catching up and no pause.
        assert _starts(log, "observe") == [1000.0, 1003.0, 1006.0]
        assert _starts(log, "act") == [1002.0, 1005.0]

    async def test_a_late_restoration_waits_only_for_what_is_left_of_the_absolute_target(
        self, clock: _Clock
    ) -> None:
        """Targets are anchored, not step to step: being late shortens a later wait, never lengthens it."""
        source = _TimedSource([_spanned(0, [100.2]), _spanned(1, [100.7])], [100.0, 100.5, 108.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[2.0, 2.0, 2.0], capture_after_s=0.5)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 2))

        # Observe 2: target 1000.5 + 8 = 1008.5, less the 2 s lead, while the clock reads 1006.
        assert clock.slept == pytest.approx([0.5])
        assert _starts(log, "observe")[-1] == pytest.approx(1006.5)

    async def test_only_a_steps_first_action_is_aligned(self, clock: _Clock) -> None:
        source = _TimedSource([_spanned(0, [105.0, 109.0])], [100.0, 120.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[1.0, 1.0], capture_after_s=0.0)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock, act_s=0.1), source, 1))

        # The first at anchor + 5; the second right after it, not at anchor + 9.
        assert _starts(log, "act") == pytest.approx([1005.0, 1005.1])

    async def test_a_step_that_made_no_env_call_has_no_action_to_align(self, clock: _Clock) -> None:
        """``action_spans == []``: every action was refused before ``env.act``; nothing is missing."""
        source = _TimedSource([_spanned(0, [], actions=1)], [100.0, 120.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[1.0, 1.0], capture_after_s=0.0)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 1))

        assert _starts(log, "act") == [1001.0]
        # The checkpoint's observation still waits for its place: 1000 + 20 - 1.
        assert clock.slept == pytest.approx([17.0])

    async def test_a_checkpoint_at_step_zero_waits_for_nothing(self, clock: _Clock) -> None:
        source = _TimedSource([], [100.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[1.0], capture_after_s=0.5)
        await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 0))
        assert clock.slept == []

    async def test_the_capture_time_is_the_earliest_captured_environment_fragment(self) -> None:
        negotiated = ObservationNegotiationResult(
            active_fragments=frozenset({"a.one", "b.two", "c.three", "loop.step_info"}),
            env_sourced=frozenset({"a.one", "b.two", "c.three"}),
            loop_sourced=frozenset({"loop.step_info"}),
        )
        observation = SimpleNamespace(
            one=_Frame(cycle=0, captured_at=12.0),
            two=_Frame(cycle=0, captured_at=11.0),
            # A failed capture is no capture, however early it was recorded.
            three=FragmentFailure(fragment_id="c.three", error="TimeoutError", captured_at=5.0),
            step_info=_Frame(cycle=0, captured_at=1.0),
        )
        assert prefix_replay._capture_time(observation, negotiated) == 11.0
        assert prefix_replay._capture_time(SimpleNamespace(), negotiated) is None


class TestRecordedTimelineRefusals:
    """What the mode needs and the source lacks is refused, never guessed or swapped for another pace."""

    @pytest.mark.parametrize(
        ("records", "named"),
        [
            pytest.param([_spanned(0, [105.0]), _spanned(1, None, actions=1)], "step 1 ", id="one"),
            pytest.param(
                [_spanned(0, None, actions=2), _spanned(1, None, actions=1)],
                "steps 0, 1 ",
                id="several",
            ),
        ],
    )
    async def test_unmeasured_action_starts_are_refused_before_anything_runs(
        self, clock: _Clock, records: list[StepRecord], named: str
    ) -> None:
        source = _TimedSource(records, [100.0, 110.0, 120.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[1.0, 1.0, 1.0], capture_after_s=0.0)
        with pytest.raises(IncompatibleCheckpointError) as caught:
            await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 2))
        assert caught.value.problems == [
            f"{named}recorded no action start times (StepRecord.action_spans was not "
            "measured), which pace: recorded_timeline needs to start each restored step's "
            "first action when the source's started"
        ]
        assert caught.value.source == _Source.name
        assert log == [] and clock.slept == []

    def test_source_problems_names_the_same_steps_before_the_run(self) -> None:
        source = _Source([_spanned(0, [105.0]), _spanned(1, None, actions=1), _spanned(2, None)])
        source.checkpoint = SimpleNamespace(step=3)  # type: ignore[attr-defined]
        (problem,) = _timeline().source_problems(source)  # type: ignore[arg-type]
        # Step 2 executes nothing, so its unmeasured spans are not missing anything.
        assert problem.startswith("step 1 recorded no action start times")

    @pytest.mark.parametrize("pace", ["none", "recorded_llm_latency"])
    async def test_the_other_paces_need_no_action_spans(self, pace: str) -> None:
        source = _Source([_spanned(0, None, actions=1)])
        source.checkpoint = SimpleNamespace(step=1)  # type: ignore[attr-defined]
        restorer = PrefixReplayRestorer(pace=pace)  # type: ignore[arg-type]
        assert restorer.source_problems(source) == []  # type: ignore[arg-type]
        log: list[tuple[Any, ...]] = []
        steps = await _collect(restorer, _ctx(_Env(log), _act(log), source, 1))
        assert [s.step for s in steps] == [0, 1]

    async def test_a_source_observation_with_no_captured_fragment_is_refused(
        self, clock: _Clock
    ) -> None:
        class _FailedAtOne(_TimedSource):
            def observation(self, step: int, negotiated: Any) -> Any:
                if step == 1:
                    return _observation(FragmentFailure(fragment_id=_FRAG, error="TimeoutError"))
                return super().observation(step, negotiated)

        source = _FailedAtOne([_spanned(0, [105.0])], [100.0, 120.0])
        log: list[tuple[Any, ...]] = []
        env = _TimedEnv(log, clock, observe_s=[1.0, 1.0], capture_after_s=0.0)
        with pytest.raises(IncompatibleCheckpointError, match="step 1 captured no environment"):
            await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 1))
        # Refused before observing step 1.
        assert [entry[1] for entry in log if entry[0] == "observe"] == [0]

    async def test_a_live_step_zero_with_no_captured_fragment_has_no_anchor(
        self, clock: _Clock
    ) -> None:
        class _FailingEnv(_TimedEnv):
            async def observe(self, negotiated: Any) -> Any:
                await super().observe(negotiated)
                return _observation(FragmentFailure(fragment_id=_FRAG, error="TimeoutError"))

        source = _TimedSource([_spanned(0, [105.0])], [100.0, 120.0])
        log: list[tuple[Any, ...]] = []
        env = _FailingEnv(log, clock, observe_s=[1.0, 1.0], capture_after_s=0.0)
        with pytest.raises(RuntimeError, match="no live anchor"):
            await _collect(_timeline(), _timed_ctx(env, _timed_act(log, clock), source, 1))
        assert _starts(log, "act") == []
