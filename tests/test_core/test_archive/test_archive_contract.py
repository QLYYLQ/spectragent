"""Every archiving capability, run through the shared archiving contract.

The clauses live in ``tests/_helpers/archive_contract.py`` so a capability
inherits them by adding one harness factory; this module runs every clause
against every factory, guards that no shipped archiving capability goes
without one, and shows each clause failing the mistake it exists to catch.

Read ``tests/_helpers/archive_contract.py`` first — it says what the
contract is and why each clause exists.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from conscribe import discover

from spectragent.core.archive import (
    ArchiveBatch,
    ArchiveFile,
    ArchiveProvenance,
    ArchiverBase,
)
from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.trace.encoding import sha256_hex
from tests._helpers.archive_contract import (
    HARNESS_FACTORIES,
    ArchiveHarness,
    assert_a_broken_collection_raises,
    assert_a_failed_collection_fails_the_trace_closed,
    assert_bind_routes_by_prefix_and_clears,
    assert_collect_hands_over_and_forgets,
    assert_collect_takes_no_step,
    assert_crash_window_hides_uncommitted_evidence,
    assert_empty_archive_costs_nothing,
    assert_every_file_belongs_to_a_bound_archiver,
    assert_files_carry_provenance,
    assert_run_records_stay_per_capability,
    assert_steps_commit_and_read_back,
    assert_tail_lands_at_run_end,
    assert_unbound_capability_archives_nothing,
    fake_archive_harness,
    missing_harnesses,
    provided_archivers_of,
)
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    STEPS_KEY,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeStepArchiver,
    QuietCapability,
    StructuralCapability,
)

FACTORY_IDS = sorted(HARNESS_FACTORIES)


@pytest.fixture(params=FACTORY_IDS)
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[ArchiveHarness]:
    """A fresh harness per test, for each archiving capability — closed afterwards."""
    built = HARNESS_FACTORIES[request.param]()
    yield built
    await built.aclose()


# ── The contract, run against every archiving capability ──────────────


class TestTheArchivingContract:
    """What the capability owes the pull — every clause, every factory."""

    def test_collect_takes_no_step(self, harness: ArchiveHarness) -> None:
        assert_collect_takes_no_step(harness)

    async def test_an_unbound_capability_archives_nothing(self, harness: ArchiveHarness) -> None:
        await assert_unbound_capability_archives_nothing(harness)

    async def test_bind_routes_by_prefix_and_clears(self, harness: ArchiveHarness) -> None:
        await assert_bind_routes_by_prefix_and_clears(harness)

    async def test_collect_hands_over_and_forgets(self, harness: ArchiveHarness) -> None:
        await assert_collect_hands_over_and_forgets(harness)

    async def test_every_file_belongs_to_a_bound_archiver(self, harness: ArchiveHarness) -> None:
        await assert_every_file_belongs_to_a_bound_archiver(harness)

    async def test_files_carry_provenance(self, harness: ArchiveHarness) -> None:
        await assert_files_carry_provenance(harness, now=time.time)

    async def test_a_broken_collection_raises(self, harness: ArchiveHarness) -> None:
        await assert_a_broken_collection_raises(harness)


class TestThroughTheTraceHook:
    """What the trace makes of it: commit, crash window, tail, failure, zero cost, a peer."""

    async def test_steps_commit_and_read_back(self, harness: ArchiveHarness, tmp_path) -> None:
        await assert_steps_commit_and_read_back(harness, tmp_path)

    async def test_the_crash_window_hides_uncommitted_evidence(
        self, harness: ArchiveHarness, tmp_path
    ) -> None:
        await assert_crash_window_hides_uncommitted_evidence(harness, tmp_path)

    async def test_the_tail_lands_at_run_end(self, harness: ArchiveHarness, tmp_path) -> None:
        await assert_tail_lands_at_run_end(harness, tmp_path)

    async def test_a_failed_collection_fails_the_trace_closed(
        self, harness: ArchiveHarness, tmp_path
    ) -> None:
        await assert_a_failed_collection_fails_the_trace_closed(harness, tmp_path)

    async def test_an_empty_archive_costs_nothing(self, harness: ArchiveHarness, tmp_path) -> None:
        await assert_empty_archive_costs_nothing(harness, tmp_path)

    async def test_run_records_stay_per_capability(self, harness: ArchiveHarness, tmp_path) -> None:
        await assert_run_records_stay_per_capability(harness, tmp_path)


class TestTheFactories:
    def test_each_factory_is_keyed_by_its_capability_id(self) -> None:
        for key, factory in HARNESS_FACTORIES.items():
            assert factory().capability_id == key

    def test_a_factory_builds_a_fresh_capability_each_time(self) -> None:
        for factory in HARNESS_FACTORIES.values():
            assert factory().capability is not factory().capability

    async def test_the_reference_harness_exercises_every_kind_of_file(self) -> None:
        """The fake is the contract's reference, so it must give every clause work to do.

        A step file per archiver, a content-addressed body that repeats (the
        blob store has something to deduplicate) and a run record — or the
        trace clauses would pass over an empty archive.
        """
        harness = fake_archive_harness()
        harness.capability.bind_archivers(list(harness.archivers))
        files: list[ArchiveFile] = []
        for step in range(3):
            await harness.produce(step)
            files += (await harness.capability.collect_archive()).files
        assert {f.archiver_id for f in files if f.scope == "step"} == {STEPS_KEY, BLOBS_KEY}
        bodies = [sha256_hex(f.data) for f in files if f.content_addressed]
        assert len(bodies) == 3 and len(set(bodies)) == 2
        assert [f.scope for f in files].count("run") == 1


# ── Completeness: no shipped archiving capability goes unchecked ──────


def test_every_shipped_archiving_capability_has_a_harness() -> None:
    """A capability that provides archivers with no harness is one nobody checked.

    Vacuous in M28 — no shipped capability provides archivers yet — and
    the moment M29's ``BrowserCapability`` declares ``__wiring__["archiver"]``
    this fails and names the file to edit. The planted cases below show it
    is not asserting over nothing.
    """
    discover("spectragent.core.capabilities")
    shipped = [
        cls
        for cls in CapabilityRegistrar.get_all().values()
        if getattr(cls, "__module__", "").startswith("spectragent.")
    ]
    assert "browser" in {getattr(cls, "__registry_key__", "") for cls in shipped}, (
        "the browser capability was not examined — the guard would pass vacuously"
    )
    missing = missing_harnesses(shipped, HARNESS_FACTORIES)
    assert not missing, (
        f"Capabilities that provide archivers but have no archive contract harness: "
        f"{missing}. Add an ArchiveHarness factory to HARNESS_FACTORIES in "
        f"tests/_helpers/archive_contract.py — the archiving contract is a framework rule, "
        f"and a capability that breaks it records an archive that lies."
    )


class _ComputedProvides:
    """Provides archivers through its instance alone — no ``__wiring__`` declaration."""

    __registry_key__ = "contract_archive_computed"

    @property
    def provided_archivers(self) -> frozenset[str]:
        return frozenset({"contract_archive_computed.x"})


class _DeclaredButNotConstructible:
    """Declares archivers on the class and cannot be default-constructed."""

    __registry_key__ = "contract_archive_declared"
    __wiring__ = {"archiver": ("archiver", ["contract_archive_declared.x"])}

    def __init__(self, required: str) -> None:
        self.required = required


class TestTheCompletenessGuardHasTeeth:
    def test_an_archiving_capability_without_a_harness_is_named(self) -> None:
        assert missing_harnesses([FakeArchivingCapability], {}) == [CAPABILITY_ID]

    def test_a_harness_satisfies_it(self) -> None:
        assert missing_harnesses([FakeArchivingCapability], HARNESS_FACTORIES) == []

    def test_a_capability_that_archives_nothing_needs_none(self) -> None:
        assert provided_archivers_of(QuietCapability) == frozenset()
        assert missing_harnesses([QuietCapability, StructuralCapability], {}) == []

    def test_provides_computed_by_the_instance_count(self) -> None:
        assert missing_harnesses([_ComputedProvides], {}) == ["contract_archive_computed"]

    def test_a_declaration_counts_without_a_default_instance(self) -> None:
        assert provided_archivers_of(_DeclaredButNotConstructible) == frozenset(
            {"contract_archive_declared.x"}
        )
        assert missing_harnesses([_DeclaredButNotConstructible], {}) == [
            "contract_archive_declared"
        ]


# ── The contract has teeth ────────────────────────────────────────────

PROVIDES = frozenset({STEPS_KEY, BLOBS_KEY})


class _WrongArchivingCapability(StructuralCapability):
    """An archiving capability by shape, getting one rule wrong at a time.

    Not registered — nothing should be able to configure it; it exists so
    the contract can be shown to have teeth. It provides the fake's keys
    (``contract_archive.steps`` / ``.blobs``) so the registered fake
    archivers can be bound to it; being unregistered, its capability id
    collides with nothing.

    ``mode``:

    - ``"correct"``: follows every rule — the baseline every mistake is a
      one-rule departure from.
    - ``"sticky"``: a collection hands over without forgetting.
    - ``"leaks"``: once anything is bound, captures for every archiver it
      provides.
    - ``"two_run_records"``: the first collection after a bind carries two
      run records.
    - ``"stale"``: stamps step files with a time from before they were
      produced.
    - ``"swallows"``: broken, returns an empty batch instead of raising.
    - ``"never_unbinds"``: ignores an empty bind.
    - ``"starts_bound"``: captures for everything until first bound.

    ``"takes_step"`` is :class:`_TakesAStep`: a signature, not a behaviour.
    """

    capability_id = CAPABILITY_ID

    def __init__(self, mode: str = "correct") -> None:
        self.mode = mode
        self.bound: tuple[str, ...] = tuple(sorted(PROVIDES)) if mode == "starts_bound" else ()
        self.buffer: list[ArchiveFile] = []
        self.broken = False
        self.run_record_due = False
        self.generation = 0
        self.captures = 0
        self.bodies = 0
        self.created_at = time.time()

    @property
    def provided_archivers(self) -> frozenset[str]:
        return PROVIDES

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        if not archivers and self.mode == "never_unbinds":
            return
        self.bound = tuple(archiver.archiver_id for archiver in archivers)
        self.run_record_due = bool(archivers)

    async def collect_archive(self) -> ArchiveBatch:
        if self.broken:
            if self.mode == "swallows":
                return ArchiveBatch()
            raise RuntimeError("wrong capability: collection broken on purpose")
        if self.run_record_due:
            self.run_record_due = False
            self.buffer.append(self._run_record("record.json"))
            if self.mode == "two_run_records":
                self.buffer.append(self._run_record("record_again.json"))
        files = tuple(self.buffer)
        if self.mode != "sticky":
            self.buffer = []
        return ArchiveBatch(files)

    # ── what the harness drives ──

    async def produce(self, step: int) -> None:
        keys = self.bound
        if keys and self.mode == "leaks":
            keys = tuple(sorted(PROVIDES))
        for key in keys:
            self.buffer.append(self._capture(key))

    def break_collect(self) -> None:
        self.broken = True

    def change_run_record(self) -> None:
        self.generation += 1
        self.run_record_due = True

    # ── internals ──

    def _meta(self, *, stale: bool = False) -> ArchiveProvenance:
        captured_at = self.created_at - 1.0 if stale else time.time()
        return ArchiveProvenance(captured_at=captured_at, ids={"capability": CAPABILITY_ID})

    def _capture(self, key: str) -> ArchiveFile:
        self.captures += 1
        stale = self.mode == "stale"
        if key == BLOBS_KEY:
            self.bodies += 1
            return ArchiveFile(
                archiver_id=key,
                name=f"body_{self.captures:03d}.bin",
                media_type="application/octet-stream",
                data=f"body #{self.bodies % 2}".encode(),
                meta=self._meta(stale=stale),
                content_addressed=True,
            )
        return ArchiveFile(
            archiver_id=key,
            name=f"evidence_{self.captures:03d}.json",
            media_type="application/json",
            data=json.dumps({"capture": self.captures}).encode(),
            meta=self._meta(stale=stale),
        )

    def _run_record(self, name: str) -> ArchiveFile:
        return ArchiveFile(
            archiver_id=CAPABILITY_ID,
            name=name,
            media_type="application/json",
            data=json.dumps({"generation": self.generation}).encode(),
            meta=self._meta(),
            scope="run",
        )


class _TakesAStep(_WrongArchivingCapability):
    """Lets the caller hand it a step number — the one thing a capability must never get."""

    async def collect_archive(self, step: int = 0) -> ArchiveBatch:  # type: ignore[override]
        return await super().collect_archive()


def _wrong_harness(mode: str) -> ArchiveHarness:
    cap = _TakesAStep() if mode == "takes_step" else _WrongArchivingCapability(mode)
    return ArchiveHarness(
        capability=cap,
        archivers=(FakeStepArchiver(), FakeBlobArchiver()),
        produce=cap.produce,
        break_collect=cap.break_collect,
        change_run_record=cap.change_run_record,
    )


#: Every clause that judges the capability itself, by name.
CAPABILITY_CLAUSES: dict[str, Callable[[ArchiveHarness], Any]] = {
    "collect takes no step": assert_collect_takes_no_step,
    "unbound archives nothing": assert_unbound_capability_archives_nothing,
    "bind routes and clears": assert_bind_routes_by_prefix_and_clears,
    "hands over and forgets": assert_collect_hands_over_and_forgets,
    "every file belongs to a bound archiver": assert_every_file_belongs_to_a_bound_archiver,
    "files carry provenance": functools.partial(assert_files_carry_provenance, now=time.time),
    "a broken collection raises": assert_a_broken_collection_raises,
}

#: Mistake → (the one clause that must fail, a phrase its message must carry).
MISTAKES: dict[str, tuple[str, str]] = {
    "sticky": ("hands over and forgets", "handed over .* again"),
    "leaks": ("every file belongs to a bound archiver", "which is not bound"),
    "two_run_records": ("every file belongs to a bound archiver", "2 run records in one batch"),
    "takes_step": ("collect takes no step", r"takes \['step'\]"),
    "stale": ("files carry provenance", "outside the window"),
    "swallows": ("a broken collection raises", "instead of raising"),
    "never_unbinds": ("bind routes and clears", "after an empty bind"),
    "starts_bound": ("unbound archives nothing", "never bound"),
}


async def _run(clause: Callable[..., Any], *args: Any) -> None:
    outcome = clause(*args)
    if inspect.isawaitable(outcome):
        await outcome


async def _failing_clauses(mode: str) -> dict[str, str]:
    """Clause name → its message, for every capability clause *mode* fails (fresh each)."""
    failed: dict[str, str] = {}
    for name, clause in CAPABILITY_CLAUSES.items():
        try:
            await _run(clause, _wrong_harness(mode))
        except AssertionError as exc:
            failed[name] = str(exc)
    return failed


class TestTheArchiveContractHasTeeth:
    """Each capability clause fails the mistake it exists to catch — and only that one.

    The trace-side clauses judge the trace hook and the reader rather than the
    capability; what is shown for them here is that they hold for *any*
    correct capability. What fails them is a broken ``TraceWriter`` (a
    commit filed under the wrong step, a tail never collected, a status that
    forgets a failed collection, one run-record table for every capability),
    not a wrong capability.
    """

    async def test_the_correct_shape_passes_every_capability_clause(self) -> None:
        """The scaffold every mistake departs from is itself right."""
        assert await _failing_clauses("correct") == {}

    @pytest.mark.parametrize("mode", sorted(MISTAKES))
    async def test_each_mistake_fails_exactly_its_clause(self, mode: str) -> None:
        clause, phrase = MISTAKES[mode]
        failed = await _failing_clauses(mode)
        assert set(failed) == {clause}, (
            f"mode {mode!r} should fail exactly {clause!r}; it failed {sorted(failed)}"
        )
        assert re.search(phrase, failed[clause]), failed[clause]

    @pytest.mark.parametrize(
        "clause",
        [
            assert_steps_commit_and_read_back,
            assert_crash_window_hides_uncommitted_evidence,
            assert_tail_lands_at_run_end,
            assert_a_failed_collection_fails_the_trace_closed,
            assert_empty_archive_costs_nothing,
            assert_run_records_stay_per_capability,
        ],
        ids=lambda clause: clause.__name__,
    )
    async def test_a_structural_capability_passes_the_trace_clauses(
        self, clause: Callable[..., Any], tmp_path
    ) -> None:
        """Portability: no clause leans on the fake's file names, keys or payloads.

        The correct structural capability names its files differently, is no
        ``CapabilityBase``, and is unregistered — and the trace-side clauses
        hold for it as they do for the fake. So a ``browser`` factory can join
        :data:`HARNESS_FACTORIES` unchanged.
        """
        await clause(_wrong_harness("correct"), tmp_path)
