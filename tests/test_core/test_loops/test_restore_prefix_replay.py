"""``prefix_replay``: re-execute the recorded prefix, verifying each step (§4.1 "恢复器").

The restorer restores the environment only. Per step ``k < t``: one observe
(one cycle), verify against the recorded observation, then retarget and
execute each action the source executed and compare its outcome; finally one
more observe for the checkpoint step *t*, verified the same way. Each step is
handed back as it is restored. Under ``strict`` the first failed check raises
before any further action; under ``warn`` it is recorded and restoration goes
on. Each ``RestorationCheck.reason`` comes from its own case here, and the
precedence when several fire.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
)
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.restoration import (
    REASON_ORDER,
    RestorationEvidence,
    Retarget,
)
from spectragent.schemas.step_record import StepRecord
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
            ("observe", 0),
            ("verify", "recorded-0", "live-0"),
            ("retarget", 0, "recorded-0", "live-0"),
            ("act", 1000),
            ("retarget", 1, "recorded-0", "live-0"),
            ("act", 1001),
            ("observe", 1),
            ("verify", "recorded-1", "live-1"),
            ("retarget", 10, "recorded-1", "live-1"),
            ("act", 1010),
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
        assert log == [("observe", 0), ("verify", "recorded-0", "live-0")]
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
        }
