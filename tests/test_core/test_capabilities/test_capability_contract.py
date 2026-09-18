"""Every capability, run through the shared observation-cycle contract.

The assertions live in ``tests/_helpers/capability_contract.py`` so a new
capability inherits them by adding one harness factory; this module is
what runs them, plus the multi-capability behaviour that only an
Environment can exercise (concurrency, per-capability failure isolation,
the foreign-action policy).

Read ``tests/_helpers/capability_contract.py`` first — it says what the
contract is and why each clause exists.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar, Literal

import pytest
from conscribe import discover

from spectragent.core.capabilities._registrar import CapabilityBase, CapabilityRegistrar
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.environments.errors import (
    ObservationFailedError,
    UnroutableFragmentError,
)
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.failure import FragmentFailure
from tests._helpers.assembly import make_env
from tests._helpers.capability_contract import (
    HARNESS_FACTORIES,
    CaptureHarness,
    assert_a_failed_cycle_costs_one_capture,
    assert_begin_observation_clears_the_cache,
    assert_foreign_action_policy_is_declared,
    assert_fragments_carry_capture_time,
    assert_next_cycle_resamples,
    assert_one_cycle_is_one_capture,
)

CAPABILITY_IDS = sorted(HARNESS_FACTORIES)


@pytest.fixture(params=CAPABILITY_IDS)
def harness(request: pytest.FixtureRequest) -> CaptureHarness:
    """A fresh harness per test, for each registered capability."""
    return HARNESS_FACTORIES[request.param]()


# ── The shared contract, run against every capability ─────────────────


class TestObservationCycleContract:
    """What ``begin_observation`` buys, pinned for every capability."""

    async def test_one_cycle_is_one_capture(self, harness: CaptureHarness) -> None:
        await assert_one_cycle_is_one_capture(harness)

    async def test_next_cycle_resamples(self, harness: CaptureHarness) -> None:
        await assert_next_cycle_resamples(harness)

    async def test_begin_observation_clears_the_cache(
        self, harness: CaptureHarness
    ) -> None:
        await assert_begin_observation_clears_the_cache(harness)

    async def test_fragments_carry_capture_time(
        self, harness: CaptureHarness
    ) -> None:
        await assert_fragments_carry_capture_time(harness, now=time.time)

    async def test_a_failed_cycle_costs_one_capture(
        self, harness: CaptureHarness
    ) -> None:
        await assert_a_failed_cycle_costs_one_capture(harness)

    def test_foreign_action_policy_is_declared(
        self, harness: CaptureHarness
    ) -> None:
        assert_foreign_action_policy_is_declared(harness)


def test_every_registered_capability_has_a_contract_harness() -> None:
    """A capability with no harness is a capability nobody checked.

    This is the mechanism that makes the suite *shared* rather than
    browser-specific: the moment ``DockerCapability`` is registered, this
    fails and names the file to edit.
    """
    discover("spectragent.core.capabilities")
    shipped = {
        key
        for key, cls in CapabilityRegistrar.get_all().items()
        if getattr(cls, "__module__", "").startswith("spectragent.")
    }
    missing = sorted(shipped - set(HARNESS_FACTORIES))
    assert not missing, (
        f"Capabilities with no observation-cycle contract harness: {missing}. "
        f"Add a CaptureHarness factory to HARNESS_FACTORIES in "
        f"tests/_helpers/capability_contract.py — the cycle rule is a "
        f"framework contract, not a browser convention, and a capability that "
        f"gets it wrong produces a trace that lies about when it looked."
    )


# ── Stub capabilities for the multi-capability half ───────────────────


class _AlphaObs(FragmentBase):
    __registry_key__ = "contract_alpha.alpha"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str = ""


class _BetaObs(FragmentBase):
    __registry_key__ = "contract_beta.beta"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str = ""


class _SurfaceObs(FragmentBase):
    __registry_key__ = "contract_shared_surface.surface"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str = ""


class _ScriptedCapability(CapabilityBase):
    """Base for the stub peers. No ``__init__`` — attributes are set after.

    A custom ``__init__`` would hide ``CapabilityBase``'s inherited
    ``Annotated`` ``id`` knob from conscribe's config extractor, which
    ``tests/test_generated/test_no_dropped_annotated_fields.py`` fails on.
    Same reason ``_RecordingCap`` in ``test_environments/test_observe.py``
    is shaped this way.
    """

    __abstract__ = True

    log: list[str]
    #: Raise this from ``resolve_fragment`` instead of answering.
    boom: Exception | None
    #: Raise this from ``begin_observation``.
    begin_boom: Exception | None
    #: Set on arrival, awaited to prove two capabilities really overlap.
    arrived: asyncio.Event | None
    peer_arrived: asyncio.Event | None
    cycles: list[int]

    def prime(self) -> Any:
        self.log = []
        self.boom = None
        self.begin_boom = None
        self.arrived = None
        self.peer_arrived = None
        self.cycles = []
        return self

    async def begin_observation(self, cycle: int) -> None:
        self.log.append(f"begin:{cycle}")
        self.cycles.append(cycle)
        if self.begin_boom is not None:
            raise self.begin_boom

    async def _rendezvous(self) -> None:
        if self.arrived is None or self.peer_arrived is None:
            return
        self.arrived.set()
        # Sequential resolution deadlocks here; concurrent resolution
        # passes immediately. One second is long enough to be certain and
        # short enough that a regression is a fast red, not a hang.
        await asyncio.wait_for(self.peer_arrived.wait(), 1.0)


class _AlphaCapability(_ScriptedCapability):
    __registry_key__ = "contract_alpha"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["contract_alpha.alpha"]),
    }

    async def resolve_fragment(self, fragment_key: str) -> Any:
        self.log.append(f"resolve:{fragment_key}")
        await self._rendezvous()
        if self.boom is not None:
            raise self.boom
        return _AlphaObs(value="alpha")


class _BetaCapability(_ScriptedCapability):
    __registry_key__ = "contract_beta"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["contract_beta.beta"]),
    }

    async def resolve_fragment(self, fragment_key: str) -> Any:
        self.log.append(f"resolve:{fragment_key}")
        await self._rendezvous()
        if self.boom is not None:
            raise self.boom
        return _BetaObs(value="beta")


class _SharedSurfaceCapability(_ScriptedCapability):
    """A capability that shares a surface with its peers, and says so.

    The one place in the tree where ``__invalidated_by_foreign_actions__``
    is ``True`` — the default is ``False`` and no shipped capability
    overrides it yet, so without this stub the policy would be a branch
    nothing ever took.
    """

    __registry_key__ = "contract_shared_surface"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["contract_shared_surface.surface"]),
    }
    __invalidated_by_foreign_actions__: ClassVar[bool] = True

    async def resolve_fragment(self, fragment_key: str) -> Any:
        self.log.append(f"resolve:{fragment_key}")
        return _SurfaceObs(value="surface")


class _ActingCapability(CapabilityBase):
    """Executes an action. Provides no fragments — it is the actor only."""

    __registry_key__ = "contract_actor"

    async def execute_action(self, action: Action) -> ActionResult:
        return ActionResult(success=True)


def _negotiated(
    keys: set[str], *, required: set[str] | None = None
) -> ObservationNegotiationResult:
    frozen = frozenset(keys)
    return ObservationNegotiationResult(
        active_fragments=frozen,
        env_sourced=frozen,
        loop_sourced=frozenset(),
        required_fragments=frozenset(required or set()),
    )


# ── Multi-capability behaviour ────────────────────────────────────────


class TestCapabilitiesObserveConcurrently:
    """Independent worlds are sampled at the same time, not one after another."""

    async def test_two_capabilities_overlap(self) -> None:
        alpha = _AlphaCapability().prime()
        beta = _BetaCapability().prime()
        alpha.arrived, beta.arrived = asyncio.Event(), asyncio.Event()
        alpha.peer_arrived, beta.peer_arrived = beta.arrived, alpha.arrived

        env = make_env(
            EnvironmentBase, {"contract_alpha#0": alpha, "contract_beta#0": beta}
        )
        obs = await env.observe(
            _negotiated({"contract_alpha.alpha", "contract_beta.beta"})
        )

        # Each capability only got past its rendezvous because the other
        # was already inside its own resolve. Serial resolution would have
        # timed out, and the timeout would have landed in the observation
        # as a FragmentFailure — which is what this asserts is absent.
        for key in ("contract_alpha.alpha", "contract_beta.beta"):
            assert not isinstance(_fragment(obs, key), FragmentFailure), (
                f"{key} timed out at the rendezvous: the environment resolved "
                f"its capabilities one after another. A 0.5s browser scrape "
                f"and a 3s device capture then cost 3.5s instead of 3s, and "
                f"the two samples are half a second apart for no reason."
            )

    async def test_every_capability_is_told_the_cycle_before_anyone_resolves(
        self,
    ) -> None:
        alpha = _AlphaCapability().prime()
        beta = _BetaCapability().prime()
        env = make_env(
            EnvironmentBase, {"contract_alpha#0": alpha, "contract_beta#0": beta}
        )

        await env.observe(_negotiated({"contract_alpha.alpha", "contract_beta.beta"}))

        # Both phases, across both capabilities: announce, then resolve.
        assert alpha.log == ["begin:1", "resolve:contract_alpha.alpha"]
        assert beta.log == ["begin:1", "resolve:contract_beta.beta"]

    async def test_each_observe_opens_exactly_one_new_cycle(self) -> None:
        alpha = _AlphaCapability().prime()
        env = make_env(EnvironmentBase, {"contract_alpha#0": alpha})
        keys = _negotiated({"contract_alpha.alpha"})

        for _ in range(3):
            await env.observe(keys)

        assert alpha.cycles == [1, 2, 3]

    async def test_environments_count_their_own_cycles(self) -> None:
        """No global counter: two environments in one run are independent."""
        one, two = _AlphaCapability().prime(), _AlphaCapability().prime()
        env_a = make_env(EnvironmentBase, {"contract_alpha#0": one})
        env_b = make_env(EnvironmentBase, {"contract_alpha#0": two})
        keys = _negotiated({"contract_alpha.alpha"})

        await env_a.observe(keys)
        await env_a.observe(keys)
        await env_b.observe(keys)

        assert one.cycles == [1, 2]
        assert two.cycles == [1]


class TestPartialFailureIsIsolated:
    """One capability's bad day does not discard everyone else's capture."""

    async def test_failure_never_fabricates_a_fragment(self) -> None:
        alpha = _AlphaCapability().prime()
        beta = _BetaCapability().prime()
        beta.boom = TimeoutError("screen grab timed out")
        env = make_env(
            EnvironmentBase, {"contract_alpha#0": alpha, "contract_beta#0": beta}
        )

        obs = await env.observe(
            _negotiated({"contract_alpha.alpha", "contract_beta.beta"})
        )
        failed = _fragment(obs, "contract_beta.beta")

        assert isinstance(failed, FragmentFailure), (
            "A capability that could not capture must not produce an empty "
            "fragment — the reader has to be able to tell the two apart."
        )
        assert failed.error == "TimeoutError"
        assert "screen grab" in failed.detail
        assert failed.capability == "contract_beta"
        assert failed.fragment_id == "contract_beta.beta"
        # The peer's capture is still there, whole.
        assert _fragment(obs, "contract_alpha.alpha").value == "alpha"

    async def test_required_failure_raises_and_names_everything(self) -> None:
        alpha = _AlphaCapability().prime()
        beta = _BetaCapability().prime()
        beta.boom = RuntimeError("container died")
        env = make_env(
            EnvironmentBase, {"contract_alpha#0": alpha, "contract_beta#0": beta}
        )

        with pytest.raises(ObservationFailedError) as caught:
            await env.observe(
                _negotiated(
                    {"contract_alpha.alpha", "contract_beta.beta"},
                    required={"contract_beta.beta"},
                )
            )

        assert caught.value.fatal == ("contract_beta.beta",)
        assert caught.value.owners["contract_beta.beta"] == "contract_beta"
        assert isinstance(caught.value.__cause__, RuntimeError)
        # The peer still ran — failures are collected, not short-circuited.
        assert alpha.log == ["begin:1", "resolve:contract_alpha.alpha"]

    async def test_a_capability_that_cannot_open_the_cycle_resolves_nothing(
        self,
    ) -> None:
        """Serving the previous capture instead is the bug, not the fallback."""
        alpha = _AlphaCapability().prime()
        beta = _BetaCapability().prime()
        beta.begin_boom = RuntimeError("session gone")
        env = make_env(
            EnvironmentBase, {"contract_alpha#0": alpha, "contract_beta#0": beta}
        )

        obs = await env.observe(
            _negotiated({"contract_alpha.alpha", "contract_beta.beta"})
        )

        assert beta.log == ["begin:1"], "no resolve may follow a failed begin"
        failed = _fragment(obs, "contract_beta.beta")
        assert isinstance(failed, FragmentFailure)
        assert failed.detail == "session gone"

    async def test_a_fragment_no_capability_owns_is_named(self) -> None:
        alpha = _AlphaCapability().prime()
        env = make_env(EnvironmentBase, {"contract_alpha#0": alpha})

        with pytest.raises(UnroutableFragmentError, match="contract_beta.beta"):
            await env.observe(
                _negotiated({"contract_alpha.alpha", "contract_beta.beta"})
            )


class TestRealCapabilitySurvivesAPeersFailure:
    """The multi-capability clause, run against each shipped capability.

    A stub stands in for the second capability because there is only one
    real one so far; what is being pinned is the environment's behaviour
    around a real resolver, not the stub's.
    """

    async def test_peer_failure_leaves_this_capability_observed(
        self, harness: CaptureHarness
    ) -> None:
        peer = _BetaCapability().prime()
        peer.boom = RuntimeError("peer is down")
        cap = harness.capability
        env = make_env(
            EnvironmentBase,
            {f"{cap.capability_id}#0": cap, "contract_beta#0": peer},
        )
        keys = set(harness.fragments) | {"contract_beta.beta"}

        obs = await env.observe(_negotiated(keys))

        for key in harness.fragments:
            fragment = _fragment(obs, key)
            assert not isinstance(fragment, FragmentFailure), (
                f"{key} was discarded because an unrelated capability failed."
            )
            assert fragment.captured_at > 0
        assert isinstance(_fragment(obs, "contract_beta.beta"), FragmentFailure)
        assert harness.captures() == 1


class TestAFailedObservationDoesNotScaleWithFragmentCount:
    """The failed-cycle clause, driven through the real ``observe()``.

    The contract clause calls ``begin_observation`` / ``resolve_fragment``
    by hand. This one goes through ``Environment.observe``, because the
    multiplication being guarded against is a *joint* property: the
    environment isolating failures per fragment is what makes the
    capability's slice continue after the first raise. Either half alone
    is fine; together, without the capability remembering its failed
    cycle, one bad step cost a full retry budget per negotiated fragment.
    """

    async def test_one_failed_observe_costs_what_a_one_fragment_one_does(
        self, harness: CaptureHarness
    ) -> None:
        harness.break_capture()
        cap = harness.capability
        env = make_env(EnvironmentBase, {f"{cap.capability_id}#0": cap})

        before = harness.captures()
        obs = await env.observe(_negotiated(set(harness.fragments)))
        whole = harness.captures() - before

        before = harness.captures()
        await env.observe(_negotiated({harness.fragments[0]}))
        single = harness.captures() - before

        assert whole == single, (
            f"{cap.capability_id}: one failed observation of "
            f"{len(harness.fragments)} fragments cost {whole} captures against "
            f"{single} for a single fragment. A dom+screenshot+meta step then "
            f"pays three full retry budgets to fail once."
        )
        # And it failed honestly: every slot says so, none was fabricated.
        for key in harness.fragments:
            assert isinstance(_fragment(obs, key), FragmentFailure)

    async def test_the_whole_slice_reports_the_same_cause(
        self, harness: CaptureHarness
    ) -> None:
        """One capture failed once, so one reason is owed to every fragment.

        Not cosmetic: a capability that re-captured per fragment could
        report a different error per slot from a world that moved between
        attempts, and the trace would suggest several independent problems
        where there was one.
        """
        harness.break_capture()
        cap = harness.capability
        env = make_env(EnvironmentBase, {f"{cap.capability_id}#0": cap})

        obs = await env.observe(_negotiated(set(harness.fragments)))

        details = {_fragment(obs, key).detail for key in harness.fragments}
        assert len(details) == 1, (
            f"{cap.capability_id}: one failed capture produced "
            f"{len(details)} different reasons: {sorted(details)}"
        )


class TestForeignActionPolicy:
    """Whether a peer's action costs me a re-capture is mine to declare."""

    async def test_independent_capability_is_not_disturbed(self) -> None:
        actor = _ActingCapability()
        peer = _BetaCapability().prime()  # default: False
        env = make_env(
            EnvironmentBase, {"contract_actor#0": actor, "contract_beta#0": peer}
        )

        await env.act(Action(name="contract_actor.poke", params={}))

        assert peer.cycles == [], (
            "A foreign action opened a cycle on a capability that declares "
            "itself independent — that is an ADB round trip per browser click."
        )

    async def test_shared_surface_capability_is_invalidated(self) -> None:
        actor = _ActingCapability()
        peer = _SharedSurfaceCapability().prime()
        env = make_env(
            EnvironmentBase,
            {"contract_actor#0": actor, "contract_shared_surface#0": peer},
        )

        await env.act(Action(name="contract_actor.poke", params={}))

        assert peer.cycles == [1]

    async def test_the_cycle_counter_stays_monotonic_across_actions(self) -> None:
        actor = _ActingCapability()
        peer = _SharedSurfaceCapability().prime()
        env = make_env(
            EnvironmentBase,
            {"contract_actor#0": actor, "contract_shared_surface#0": peer},
        )
        keys = _negotiated({"contract_shared_surface.surface"})

        await env.observe(keys)
        await env.act(Action(name="contract_actor.poke", params={}))
        await env.observe(keys)

        assert peer.cycles == [1, 2, 3]
        assert peer.cycles == sorted(peer.cycles)

    async def test_a_failed_invalidation_does_not_lose_the_action_result(
        self,
    ) -> None:
        actor = _ActingCapability()
        peer = _SharedSurfaceCapability().prime()
        peer.begin_boom = RuntimeError("cannot reach device")
        env = make_env(
            EnvironmentBase,
            {"contract_actor#0": actor, "contract_shared_surface#0": peer},
        )

        result = await env.act(Action(name="contract_actor.poke", params={}))

        assert result.success is True


class _WrongCapability:
    """The two ways of getting the cycle wrong, as runnable code.

    Not a registered capability — nothing should be able to configure
    these. They exist so the contract can be shown to have teeth: a suite
    that no plausible mistake fails is a suite that proves nothing.

    ``mode``:

    - ``"sticky"``: captures once and never again. What a capability that
      simply ignores ``begin_observation`` does.
    - ``"served"``: the heuristic this work deleted — a *second read of
      one key* is taken as the next observation, and the cycle boundary is
      ignored.

    Both also make the third mistake, which is independent of the mode and
    is what the failed-cycle clause is for: once ``broken``, they re-enter
    the capture for every fragment of the slice instead of remembering
    that this cycle's capture already failed.
    """

    capability_id = "wrong"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.captures = 0
        self.broken = False
        self._served: set[str] = set()
        self._fragments: dict[str, Any] = {}

    async def begin_observation(self, cycle: int) -> None:
        return None  # both modes ignore the announcement

    async def resolve_fragment(self, fragment_key: str) -> Any:
        if self.mode == "served" and fragment_key in self._served:
            self._fragments.clear()
            self._served.clear()
        if not self._fragments:
            self.captures += 1
            if self.broken:
                # Nothing is recorded about this cycle, so the next
                # fragment of the same slice pays for the capture again.
                raise RuntimeError("capture lost the race")
            tag = f"capture-{self.captures}"
            self._fragments = {
                "contract_alpha.alpha": _AlphaObs(value=tag),
                "contract_beta.beta": _BetaObs(value=tag),
            }
        self._served.add(fragment_key)
        return self._fragments[fragment_key]

    def _break(self) -> None:
        self.broken = True
        self._fragments.clear()

    def harness(self) -> CaptureHarness:
        return CaptureHarness(
            capability=self,  # type: ignore[arg-type]
            fragments=("contract_alpha.alpha", "contract_beta.beta"),
            captures=lambda: self.captures,
            break_capture=self._break,
        )


class TestTheContractHasTeeth:
    """Each clause fails the mistake it exists to catch."""

    async def test_a_capability_that_never_resamples_is_caught(self) -> None:
        harness = _WrongCapability("sticky").harness()
        await assert_one_cycle_is_one_capture(harness)  # this half it gets right
        with pytest.raises(AssertionError, match="stale data as current"):
            await assert_next_cycle_resamples(_WrongCapability("sticky").harness())

    async def test_the_deleted_served_key_heuristic_is_caught(self) -> None:
        with pytest.raises(AssertionError, match="One cycle is one capture"):
            await assert_one_cycle_is_one_capture(_WrongCapability("served").harness())
        # And the clause that reads a *different* key each cycle: the
        # heuristic has never served it, so it answers from the previous
        # capture. This is the recorded failure — a step's screenshot 5.5
        # seconds older than the page it claims to show.
        with pytest.raises(AssertionError, match="did not drop cycle 1's capture"):
            await assert_begin_observation_clears_the_cache(
                _WrongCapability("served").harness()
            )
        # Note it passes assert_next_cycle_resamples, which reads every
        # fragment in both cycles: that clause cannot tell the two apart.
        await assert_next_cycle_resamples(_WrongCapability("served").harness())

    async def test_re_capturing_per_fragment_on_a_bad_cycle_is_caught(self) -> None:
        """The clause that would have caught the 2xN retry cost.

        None of the other four notice it: they all run on a capability
        whose captures succeed, and this mistake only shows up once one
        fails. That is exactly why the failure path needed a clause of its
        own rather than a comment in ``_populate_cycle``.
        """
        sticky = _WrongCapability("sticky").harness()
        await assert_one_cycle_is_one_capture(sticky)  # happy path is fine
        with pytest.raises(AssertionError, match="scaling with the number of"):
            await assert_a_failed_cycle_costs_one_capture(
                _WrongCapability("sticky").harness()
            )


def _fragment(obs: Any, fragment_key: str) -> Any:
    """Read a fragment off the negotiated observation by its full key."""
    return getattr(obs, fragment_key.rsplit(".", 1)[-1])
