"""Archive negotiation: route each requested archiver to the capability that provides it.

The demand is a consumer *instance*'s ``archive`` list, always fully
qualified, so the rules are a routing check — but a routing check that
reports every problem at once, names both parties in each line, and uses
exactly the wording ``docs/architecture_design.md`` §4.5 pins.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.environments import EnvironmentBase
from spectragent.core.negotiation import (
    ArchiveNegotiationResult,
    IncompatibleArchiveError,
    NegotiationResultRegistrar,
    negotiate_archive,
)
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    CONSUMER_HOOK_ID,
    ENV_TYPE,
    ORPHAN_KEY,
    STEPS_KEY,
    UNPROVIDED_KEY,
    ArchiveConsumerHook,
    ContractArchiveEnv,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeOrphanArchiver,
    FakeStepArchiver,
    FakeUnprovidedArchiver,
    StructuralCapability,
    StructuralCapabilityArchiver,
    make_archive_env,
)
from tests._helpers.assembly import make_env

_REQ = CONSUMER_HOOK_ID
_BOTH = sorted([BLOBS_KEY, STEPS_KEY])


def _problems(excinfo: pytest.ExceptionInfo[IncompatibleArchiveError]) -> list[str]:
    return str(excinfo.value).split("\n")


class TestSuccess:
    def test_binds_every_requested_archiver_to_its_capability(self) -> None:
        consumer = ArchiveConsumerHook([FakeStepArchiver(), FakeBlobArchiver()])
        result = negotiate_archive([consumer], make_archive_env())
        assert isinstance(result, ArchiveNegotiationResult)
        assert result.active_archivers == frozenset({STEPS_KEY, BLOBS_KEY})
        assert dict(result.archive_bindings) == {STEPS_KEY: CAPABILITY_ID, BLOBS_KEY: CAPABILITY_ID}

    def test_bindings_name_the_capability_registry_key_not_its_instance_id(self) -> None:
        env = make_env(ContractArchiveEnv, {"archive#7": FakeArchivingCapability()})
        result = negotiate_archive([ArchiveConsumerHook([FakeStepArchiver()])], env)
        assert dict(result.archive_bindings) == {STEPS_KEY: CAPABILITY_ID}

    def test_registered_as_the_archive_result_type(self) -> None:
        assert NegotiationResultRegistrar.get("archive") is ArchiveNegotiationResult
        result = negotiate_archive([ArchiveConsumerHook([FakeStepArchiver()])], make_archive_env())
        assert result.result_type == "archive"

    def test_nothing_is_bound_by_negotiating(self) -> None:
        env = make_archive_env()
        negotiate_archive([ArchiveConsumerHook([FakeStepArchiver()])], env)
        assert env.capabilities[CAPABILITY_ID].bind_calls == 0  # type: ignore[attr-defined]


class TestEachProblemNamesBothParties:
    def test_no_capability_for_the_prefix(self) -> None:
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([ArchiveConsumerHook([FakeOrphanArchiver()])], make_archive_env())
        assert _problems(excinfo) == [
            f"{_REQ} requests archiver {ORPHAN_KEY!r}, but environment {ENV_TYPE!r} has no "
            f"capability 'contract_nowhere' (its capabilities: [{CAPABILITY_ID!r}])."
        ]

    def test_the_capability_does_not_provide_the_key(self) -> None:
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([ArchiveConsumerHook([FakeUnprovidedArchiver()])], make_archive_env())
        assert _problems(excinfo) == [
            f"{_REQ} requests archiver {UNPROVIDED_KEY!r}, but environment {ENV_TYPE!r} provides "
            f"archivers: {_BOTH} (capability {CAPABILITY_ID!r} declares {_BOTH})."
        ]

    def test_a_capability_that_does_not_archive_declares_none(self) -> None:
        """The design doc's own example shape: an environment that provides nothing."""
        env = make_env(EnvironmentBase, {"structural#0": StructuralCapability()})
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([ArchiveConsumerHook([StructuralCapabilityArchiver()])], env)
        assert _problems(excinfo) == [
            f"{_REQ} requests archiver 'contract_archive_structural.x', but environment "
            f"'EnvironmentBase' provides archivers: [] (capability "
            f"'contract_archive_structural' declares none)."
        ]

    def test_one_list_naming_an_archiver_twice(self) -> None:
        consumer = ArchiveConsumerHook([FakeStepArchiver(), FakeStepArchiver(redact=False)])
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([consumer], make_archive_env())
        assert _problems(excinfo) == [
            f"{_REQ} lists archiver {STEPS_KEY!r} more than once; an archive list names each "
            f"archiver at most once."
        ]

    def test_one_archiver_in_two_consumers(self) -> None:
        first = ArchiveConsumerHook([FakeStepArchiver()])
        second = ArchiveConsumerHook([FakeStepArchiver()])
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([first, second], make_archive_env())
        assert (
            f"archiver {STEPS_KEY!r} is requested by more than one hook ({_REQ}, {_REQ}); a run "
            f"binds each archiver once."
        ) in _problems(excinfo)

    def test_two_consumers_with_disjoint_archives(self) -> None:
        first = ArchiveConsumerHook([FakeStepArchiver()])
        second = ArchiveConsumerHook([FakeBlobArchiver()])
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([first, second], make_archive_env())
        assert _problems(excinfo) == [
            f"more than one hook requests archivers ({_REQ}: [{STEPS_KEY!r}]; {_REQ}: "
            f"[{BLOBS_KEY!r}]); a run binds the archive of at most one hook."
        ]

    def test_no_environment(self) -> None:
        consumer = ArchiveConsumerHook([FakeStepArchiver(), FakeBlobArchiver()])
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([consumer], None)
        assert _problems(excinfo) == [
            f"{_REQ} requests archivers {_BOTH}, but this run has no environment."
        ]
        assert excinfo.value.provided == frozenset()
        assert excinfo.value.missing == frozenset({STEPS_KEY, BLOBS_KEY})


class TestTheReport:
    def test_every_problem_at_once_one_line_each_sorted(self) -> None:
        consumer = ArchiveConsumerHook(
            [FakeOrphanArchiver(), FakeUnprovidedArchiver(), FakeStepArchiver(), FakeStepArchiver()]
        )
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([consumer], make_archive_env())
        lines = _problems(excinfo)
        assert len(lines) == 3
        assert lines == sorted(lines)
        assert excinfo.value.problems == tuple(lines)

    def test_attributes(self) -> None:
        consumer = ArchiveConsumerHook(
            [FakeOrphanArchiver(), FakeUnprovidedArchiver(), FakeStepArchiver()]
        )
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([consumer], make_archive_env())
        err = excinfo.value
        assert err.missing == frozenset({ORPHAN_KEY, UNPROVIDED_KEY})
        assert err.provided == frozenset({STEPS_KEY, BLOBS_KEY})
        assert err.requested_by == {
            ORPHAN_KEY: (_REQ,),
            STEPS_KEY: (_REQ,),
            UNPROVIDED_KEY: (_REQ,),
        }
        assert list(err.requested_by) == sorted(err.requested_by)


class _EqualConsumer:
    """Two of these always compare equal — and are still two consumers."""

    __registry_key__ = "contract_archive_equal_consumer"

    def __init__(self, archive: list[Any]) -> None:
        self.archive = archive

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EqualConsumer)

    __hash__ = object.__hash__


class TestConsumersAreComparedByIdentity:
    def test_two_equal_consumers_are_two_consumers(self) -> None:
        first, second = _EqualConsumer([FakeStepArchiver()]), _EqualConsumer([FakeBlobArchiver()])
        assert first == second
        with pytest.raises(IncompatibleArchiveError, match="more than one hook"):
            negotiate_archive([first, second], make_archive_env())

    def test_the_same_consumer_listed_twice_is_one(self) -> None:
        consumer = ArchiveConsumerHook([FakeStepArchiver()])
        result = negotiate_archive([consumer, consumer], make_archive_env())
        assert result.active_archivers == frozenset({STEPS_KEY})

    def test_the_label_is_the_consumers_registry_key(self) -> None:
        with pytest.raises(
            IncompatibleArchiveError, match="^contract_archive_equal_consumer requests"
        ):
            negotiate_archive([_EqualConsumer([FakeOrphanArchiver()])], make_archive_env())


class TestProgrammingErrors:
    def test_no_consumer_at_all(self) -> None:
        with pytest.raises(ValueError, match="at least one consumer"):
            negotiate_archive([], make_archive_env())

    def test_configuration_in_place_of_an_archiver(self) -> None:
        """The name-not-instance defect, refused by name."""
        consumer = ArchiveConsumerHook()
        consumer.archive = [{"archiver_id": STEPS_KEY}]  # type: ignore[list-item]
        with pytest.raises(TypeError, match=f"{_REQ}'s archive holds a dict"):
            negotiate_archive([consumer], make_archive_env())


class _NonBlockingConsumer(ArchiveConsumerHook):
    """A consumer whose handlers would finish after the commit point."""

    __abstract__ = True

    @property
    def blocking(self) -> bool:
        return False


class TestTheConsumerMustBlock:
    """Its evidence must land before the step is committed (§4.6, ``blocking``)."""

    def test_a_consumer_that_does_not_block_is_refused(self) -> None:
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([_NonBlockingConsumer([FakeStepArchiver()])], make_archive_env())
        assert _problems(excinfo) == [
            f"{_REQ} requests archivers {[STEPS_KEY]} but is not blocking (blocking=False); a "
            f"non-blocking hook's handlers finish after the step is committed, so the evidence "
            f"it pulls would reach no trace: an archive consumer must be blocking."
        ]

    def test_it_is_reported_with_every_other_problem(self) -> None:
        consumer = _NonBlockingConsumer([FakeOrphanArchiver()])
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            negotiate_archive([consumer], make_archive_env())
        problems = _problems(excinfo)
        assert len(problems) == 2
        assert any("is not blocking" in p for p in problems)
        assert any(ORPHAN_KEY in p and "has no capability" in p for p in problems)

    def test_a_consumer_that_does_not_say_is_taken_as_blocking(self) -> None:
        """The weaver's reading too (``core/aop/proxy.py``)."""
        result = negotiate_archive([_EqualConsumer([FakeStepArchiver()])], make_archive_env())
        assert result.active_archivers == frozenset({STEPS_KEY})
