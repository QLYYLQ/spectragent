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
