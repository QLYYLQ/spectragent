"""The environment's half of the archive axis: route the binding, collect the evidence.

Three properties, each the environment's own guarantee rather than a
convention its capabilities happen to follow:

- **binding validates everything before binding anything**, routes by key
  prefix exactly like ``act()``, and clears every archiving capability that
  receives nothing;
- **collection is concurrent, complete and fail-closed** — every bound
  capability is waited for, a failure anywhere fails the whole collection
  naming every failure, and a cancellation is never swallowed;
- **nothing bound costs nothing** — no capability is touched.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from spectragent.core.archive import ArchiveBatch, ArchiveContractError
from spectragent.core.environments import (
    ArchiveCollectionError,
    Environment,
    EnvironmentBase,
    UnroutableArchiverError,
)
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    ORPHAN_KEY,
    PEER_A,
    PEER_B,
    STEPS_KEY,
    UNPROVIDED_KEY,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeOrphanArchiver,
    FakeStepArchiver,
    FakeUnprovidedArchiver,
    PeerAArchiver,
    PeerArchivingCapability,
    PeerBArchiver,
    QuietCapability,
    StructuralCapability,
    StructuralCapabilityArchiver,
)
from tests._helpers.assembly import make_env

pytestmark = pytest.mark.usefixtures("archive_fakes")

_PEER_A_KEY = "contract_archive_peer_a.events"
_PEER_B_KEY = "contract_archive_peer_b.events"


def _peers() -> tuple[PeerArchivingCapability, PeerArchivingCapability, EnvironmentBase]:
    a = PeerArchivingCapability(PEER_A, [_PEER_A_KEY])
    b = PeerArchivingCapability(PEER_B, [_PEER_B_KEY])
    env = make_env(EnvironmentBase, {"a#0": a, "b#0": b})
    env.bind_archivers([PeerAArchiver(), PeerBArchiver()])
    return a, b, env


class TestTheProtocolDeclaresTheThreeAxesAlike:
    @pytest.mark.parametrize(
        "member",
        [
            "provided_fragments",
            "provided_actions",
            "provided_archivers",
            "bind_archivers",
            "collect_archive",
        ],
    )
    def test_member_is_declared(self, member: str) -> None:
        assert hasattr(Environment, member)

    @pytest.mark.parametrize("owner", [Environment, EnvironmentBase])
    def test_collection_takes_no_step(self, owner: type) -> None:
        """No step number or cycle ever reaches a capability."""
        assert list(inspect.signature(owner.collect_archive).parameters) == ["self"]


class TestProvidedArchivers:
    def test_union_over_the_capabilities_that_archive(self) -> None:
        env = make_env(
            EnvironmentBase,
            {
                "archive#0": FakeArchivingCapability(),
                "quiet#0": QuietCapability(),
                "structural#0": StructuralCapability(),
                "peer#0": PeerArchivingCapability(PEER_A, [_PEER_A_KEY]),
            },
        )
        assert env.provided_archivers == frozenset({STEPS_KEY, BLOBS_KEY, _PEER_A_KEY})

    def test_an_environment_that_archives_nothing(self) -> None:
        env = make_env(EnvironmentBase, {"quiet#0": QuietCapability()})
        assert env.provided_archivers == frozenset()


class TestBinding:
    def test_routes_each_archiver_to_its_capability_by_prefix(self) -> None:
        cap = FakeArchivingCapability()
        peer = PeerArchivingCapability(PEER_A, [_PEER_A_KEY])
        env = make_env(EnvironmentBase, {"archive#0": cap, "peer#0": peer})
        steps, blobs, events = FakeStepArchiver(), FakeBlobArchiver(), PeerAArchiver()

        env.bind_archivers([steps, events, blobs])

        assert cap.fake.bound == (steps, blobs)
        assert peer.bound == (events,)

    def test_the_bound_instances_are_the_ones_handed_in(self) -> None:
        """Knobs travel with the instance — the capability reads them off it."""
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap})
        blobs = FakeBlobArchiver(payload_bytes=64)
        env.bind_archivers([blobs])
        assert cap.fake.bound[0] is blobs

    def test_a_capability_that_gets_nothing_is_cleared(self) -> None:
        cap = FakeArchivingCapability()
        peer = PeerArchivingCapability(PEER_A, [_PEER_A_KEY])
        env = make_env(EnvironmentBase, {"archive#0": cap, "peer#0": peer})
        env.bind_archivers([FakeStepArchiver(), PeerAArchiver()])

        env.bind_archivers([PeerAArchiver()])

        assert cap.fake.bound == ()
        assert cap.bind_calls == 2
        assert len(peer.bound) == 1

    def test_an_empty_binding_clears_every_archiving_capability(self) -> None:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap, "quiet#0": QuietCapability()})
        env.bind_archivers([FakeStepArchiver()])
        env.bind_archivers([])
        assert cap.fake.bound == ()

    def test_a_capability_without_the_surface_is_never_called(self) -> None:
        """``StructuralCapability`` has no ``bind_archivers`` — a call would raise."""
        env = make_env(
            EnvironmentBase,
            {"archive#0": FakeArchivingCapability(), "structural#0": StructuralCapability()},
        )
        env.bind_archivers([FakeStepArchiver()])
        env.bind_archivers([])

    def test_binding_acquires_nothing(self) -> None:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap})
        env.bind_archivers([FakeStepArchiver(), FakeBlobArchiver()])
        assert cap.fake.setup_calls == 0
        assert cap.collect_calls == 0


class TestUnroutable:
    def _env(self) -> tuple[FakeArchivingCapability, EnvironmentBase]:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap, "structural#0": StructuralCapability()})
        return cap, env

    def test_no_capability_for_the_prefix(self) -> None:
        cap, env = self._env()
        with pytest.raises(
            UnroutableArchiverError, match="no capability 'contract_nowhere'"
        ) as excinfo:
            env.bind_archivers([FakeOrphanArchiver()])
        assert set(excinfo.value.problems) == {ORPHAN_KEY}
        assert excinfo.value.capability_ids == frozenset(
            {CAPABILITY_ID, "contract_archive_structural"}
        )

    def test_a_capability_that_does_not_archive(self) -> None:
        _cap, env = self._env()
        with pytest.raises(UnroutableArchiverError, match="ArchivingCapability"):
            env.bind_archivers([StructuralCapabilityArchiver()])

    def test_a_key_the_capability_does_not_provide(self) -> None:
        _cap, env = self._env()
        with pytest.raises(UnroutableArchiverError, match="does not provide"):
            env.bind_archivers([FakeUnprovidedArchiver()])

    def test_every_problem_at_once_and_nothing_bound(self) -> None:
        """Validate first, bind second: the routable archiver is not bound either."""
        cap, env = self._env()
        env.bind_archivers([FakeBlobArchiver()])
        with pytest.raises(UnroutableArchiverError) as excinfo:
            env.bind_archivers(
                [
                    FakeStepArchiver(),
                    FakeOrphanArchiver(),
                    FakeUnprovidedArchiver(),
                    StructuralCapabilityArchiver(),
                ]
            )
        assert set(excinfo.value.problems) == {
            ORPHAN_KEY,
            UNPROVIDED_KEY,
            "contract_archive_structural.x",
        }
        # The earlier binding survives untouched.
        assert cap.bind_calls == 1
        assert cap.bound_keys == (BLOBS_KEY,)


class TestNothingBound:
    async def test_collect_touches_no_capability(self) -> None:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap})
        batch = await env.collect_archive()
        assert batch == ArchiveBatch()
        assert cap.collect_calls == 0

    async def test_nor_after_an_empty_binding(self) -> None:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap})
        env.bind_archivers([FakeStepArchiver()])
        env.bind_archivers([])
        assert (await env.collect_archive()).is_empty
        assert cap.collect_calls == 0


class TestCollection:
    async def test_bound_capabilities_are_collected_concurrently(self) -> None:
        """Each waits for the other to have *started* — sequential collection
        would deadlock here, and the timeout turns that into a failure."""
        a, b, env = _peers()
        a.wait_for, b.wait_for = b.entered, a.entered
        a.files.append(a.evidence(_PEER_A_KEY))
        b.files.append(b.evidence(_PEER_B_KEY))

        batch = await asyncio.wait_for(env.collect_archive(), timeout=5)

        assert [f.archiver_id for f in batch.files] == [_PEER_A_KEY, _PEER_B_KEY]

    async def test_batches_merge_in_capability_order(self) -> None:
        cap = FakeArchivingCapability()
        peer = PeerArchivingCapability(PEER_A, [_PEER_A_KEY])
        env = make_env(EnvironmentBase, {"peer#0": peer, "archive#0": cap})
        env.bind_archivers([FakeStepArchiver(), PeerAArchiver()])
        await cap.begin_observation(1)
        peer.files.append(peer.evidence(_PEER_A_KEY))

        batch = await env.collect_archive()

        owners = [f.capability_id for f in batch.files]
        assert owners[0] == PEER_A
        assert set(owners[1:]) == {CAPABILITY_ID}

    async def test_only_bound_capabilities_are_collected(self) -> None:
        cap = FakeArchivingCapability()
        peer = PeerArchivingCapability(PEER_A, [_PEER_A_KEY])
        env = make_env(EnvironmentBase, {"archive#0": cap, "peer#0": peer})
        env.bind_archivers([PeerAArchiver()])
        await env.collect_archive()
        assert peer.collect_calls == 1
        assert cap.collect_calls == 0

    async def test_one_failure_fails_the_collection_after_every_task_finished(self) -> None:
        a, b, env = _peers()
        boom = RuntimeError("disk on fire")
        a.fail_with = boom
        # The healthy peer is still working when the failure happens.
        b.wait_for = a.entered
        b.yields_after_wait = 3
        b.files.append(b.evidence(_PEER_B_KEY))

        with pytest.raises(ArchiveCollectionError) as excinfo:
            await env.collect_archive()

        err = excinfo.value
        assert list(err.failures) == [PEER_A]
        assert err.failures[PEER_A] is boom
        assert err.__cause__ is boom
        assert PEER_A in str(err) and "disk on fire" in str(err)
        # No orphan task: the healthy collection ran to its end first.
        assert b.finished is True
        assert b.collect_calls == 1

    async def test_every_failure_is_named_in_capability_order(self) -> None:
        a, b, env = _peers()
        a.fail_with = RuntimeError("first")
        b.fail_with = ValueError("second")

        with pytest.raises(ArchiveCollectionError) as excinfo:
            await env.collect_archive()

        assert list(excinfo.value.failures) == [PEER_A, PEER_B]
        assert excinfo.value.__cause__ is a.fail_with
        assert "first" in str(excinfo.value) and "second" in str(excinfo.value)

    async def test_evidence_under_another_capabilitys_prefix_is_a_failure(self) -> None:
        a, _b, env = _peers()
        a.files.append(a.evidence(_PEER_B_KEY))

        with pytest.raises(ArchiveCollectionError) as excinfo:
            await env.collect_archive()

        failure = excinfo.value.failures[PEER_A]
        assert isinstance(failure, ArchiveContractError)
        assert _PEER_B_KEY in str(failure)

    async def test_a_capability_that_returns_no_batch_is_a_named_failure(self) -> None:
        """A contract failure naming the capability — not an ``AttributeError`` from the merge."""

        class _ListReturning(PeerArchivingCapability):
            async def collect_archive(self) -> Any:
                await super().collect_archive()
                return []

        cap = _ListReturning(PEER_A, [_PEER_A_KEY])
        env = make_env(EnvironmentBase, {"a#0": cap})
        env.bind_archivers([PeerAArchiver()])

        with pytest.raises(ArchiveCollectionError) as excinfo:
            await env.collect_archive()

        failure = excinfo.value.failures[PEER_A]
        assert isinstance(failure, ArchiveContractError)
        assert "not an ArchiveBatch" in str(failure)

    async def test_a_cancellation_is_never_swallowed(self) -> None:
        a, _b, env = _peers()
        a.fail_with = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await env.collect_archive()

    async def test_a_cancelled_collection_leaves_no_sibling_running(self) -> None:
        """``gather`` would let the sibling of a cancelled child run on, unobserved."""
        a, b, env = _peers()
        a.fail_with = asyncio.CancelledError()
        b.wait_for = asyncio.Event()  # never set: b would wait forever

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(env.collect_archive(), timeout=5)

        assert b.was_cancelled is True

    async def test_cancelling_the_caller_cancels_every_collection(self) -> None:
        a, b, env = _peers()
        a.wait_for = b.wait_for = asyncio.Event()  # never set
        task = asyncio.ensure_future(env.collect_archive())
        await asyncio.wait_for(a.entered.wait(), timeout=5)
        await asyncio.wait_for(b.entered.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert a.was_cancelled and b.was_cancelled

    async def test_the_fake_capabilitys_evidence_comes_through(self) -> None:
        cap = FakeArchivingCapability()
        env = make_env(EnvironmentBase, {"archive#0": cap})
        env.bind_archivers([FakeStepArchiver()])
        await cap.begin_observation(1)

        batch = await env.collect_archive()

        names = sorted(f.name for f in batch.files)
        assert names == ["cycle_001.json", "fingerprint.json"]
        assert cap.collect_calls == 1
        # Handed over and forgotten: the next collection is empty.
        assert (await env.collect_archive()).is_empty
