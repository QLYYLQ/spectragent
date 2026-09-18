"""Tests for Environment observe() delegation to Capability resolvers."""

import asyncio
from typing import ClassVar, Literal

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.schemas.observation._registrar import FragmentBase
from tests._helpers.assembly import make_env


class _TestFragObs(FragmentBase):
    __registry_key__ = "test_env_obs_frag"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str


class ObsCap(CapabilityBase):
    __registry_key__ = "obs_test_cap"
    __wiring__ = {"observation": ("observation", ["test_env_obs_frag"])}

    async def resolve_fragment(self, frag_key, **kw):
        return _TestFragObs(value="resolved_data")


class TestObserveDelegation:
    def setup_method(self):
        self.env = make_env(EnvironmentBase, {"obs_test_cap": ObsCap()})

    def test_provided_fragments(self) -> None:
        assert "test_env_obs_frag" in self.env.provided_fragments

    def test_observe_resolves_fragment(self) -> None:
        negotiated = ObservationNegotiationResult(
            active_fragments=frozenset({"test_env_obs_frag"}),
            env_sourced=frozenset({"test_env_obs_frag"}),
            loop_sourced=frozenset(),
        )
        obs = asyncio.run(self.env.observe(negotiated))
        assert obs.test_env_obs_frag.value == "resolved_data"

    def test_observe_empty_negotiation(self) -> None:
        negotiated = ObservationNegotiationResult(
            active_fragments=frozenset(),
            env_sourced=frozenset(),
            loop_sourced=frozenset(),
        )
        obs = asyncio.run(self.env.observe(negotiated))
        assert obs.model_dump() == {}


# ── One observe call is one observation ──────────────────────────────


class _OnceA(FragmentBase):
    __registry_key__ = "test_env_obs_once_a"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str


class _OnceB(FragmentBase):
    __registry_key__ = "test_env_obs_once_b"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str


class _OnceC(FragmentBase):
    __registry_key__ = "test_env_obs_once_c"
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    value: str


_ONCE_FRAGMENTS: dict[str, type[FragmentBase]] = {
    "test_env_obs_once_a": _OnceA,
    "test_env_obs_once_b": _OnceB,
    "test_env_obs_once_c": _OnceC,
}


class _RecordingCap(CapabilityBase):
    """Logs every resolution as an enter/exit pair, yielding in between.

    ``log`` is attached after construction rather than taken by an
    ``__init__`` of its own: this class is registered like any capability,
    and a custom ``__init__`` would hide the inherited ``id`` knob from
    config extraction (``test_no_dropped_annotated_fields``).
    """

    __registry_key__ = "obs_once_test_cap"
    __wiring__ = {"observation": ("observation", list(_ONCE_FRAGMENTS))}

    log: list[tuple[str, str]]

    async def resolve_fragment(self, frag_key, **kw):
        self.log.append(("enter", frag_key))
        # A concurrent observe would start another resolution here.
        await asyncio.sleep(0)
        self.log.append(("exit", frag_key))
        return _ONCE_FRAGMENTS[frag_key](value=frag_key)


class TestOneObserveResolvesEachFragmentOnce:
    """One capability's slice is resolved once each, in sequence.

    Not a cache rule — the cycle boundary is announced by
    ``begin_observation`` now, so a fragment read twice inside a cycle is
    explicitly the *same* observation and costs nothing extra (see
    ``test_browser_lazy_batch.py``). What this pins is the other half of
    ``observe``'s concurrency plan: capabilities run in parallel, but a
    single capability's own fragments run **one at a time**, because they
    share one capture. Firing them at once would either duplicate the
    capture or have several coroutines race for it, and the fragments of
    one observation could then come from two different captures — the
    agent shown two worlds in one step.

    The "exactly once" half is about cost rather than correctness: each
    fragment of a slice is asked for once per cycle, so the environment
    never pays for a resolve the negotiation did not ask for.
    """

    def test_every_env_fragment_once_per_call_one_at_a_time(self) -> None:
        log: list[tuple[str, str]] = []
        cap = _RecordingCap()
        cap.log = log
        env = make_env(EnvironmentBase, {"obs_once_test_cap": cap})
        keys = frozenset(_ONCE_FRAGMENTS)
        negotiated = ObservationNegotiationResult(
            active_fragments=keys, env_sourced=keys, loop_sourced=frozenset()
        )

        for _call in range(2):
            log.clear()
            obs = asyncio.run(env.observe(negotiated))

            # Each fragment exactly once, and nothing else.
            assert sorted(k for event, k in log if event == "enter") == sorted(keys)
            # One at a time: every resolution exits before the next enters.
            assert len(log) == 2 * len(keys)
            for enter, leave in zip(log[::2], log[1::2], strict=True):
                assert enter[0] == "enter"
                assert leave == ("exit", enter[1])
            assert {getattr(obs, k).value for k in keys} == set(keys)
