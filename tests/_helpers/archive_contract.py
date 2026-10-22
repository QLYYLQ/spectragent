"""The archiving contract every capability that provides archivers has to satisfy.

A capability that declares ``__wiring__["archiver"]`` hands over environment
evidence when the trace hook pulls it (``docs/architecture_design.md`` §4.6,
§4.9). ``typing.Protocol`` can state ``bind_archivers`` and
``collect_archive``; it cannot state *hand over and forget*, *take no step*,
*capture nothing unbound*, or *never return a batch that looks complete
while evidence is missing* — and a capability that gets any of those wrong produces an archive
that lies: evidence attributed to the wrong step, evidence twice, evidence of
a run nobody asked to record, or a silent hole that poisons replay and
branching later. So the rules live here, as assertions, and every archiving
capability is run through them. M28 runs them against the fake
``contract_archive`` capability; M29 opts ``BrowserCapability`` in under fake
CDP.

**Opt in:** write an :class:`ArchiveHarness` factory and add it to
:data:`HARNESS_FACTORIES`. ``tests/test_core/test_archive/test_archive_contract.py``
runs every clause against every factory, and its completeness guard fails the
moment a shipped capability provides archivers without one.

Every clause is capability-agnostic: it uses only the harness, the
``ArchivingCapability`` surface, an environment built around the capability,
and the trace hook — never a file name, a key or a payload of one particular
capability. What is pinned:

Capability side (the archiving contract, §4.9):

1. :func:`assert_collect_takes_no_step` — ``collect_archive`` takes no
   argument at all: "what step *t*'s commit takes belongs to step *t*" is the
   recorder's rule, and the capability never learns a step number.
2. :func:`assert_unbound_capability_archives_nothing` — never bound, or bound
   to nothing: nothing is captured.
3. :func:`assert_bind_routes_by_prefix_and_clears` — every archiver key routes
   to the capability by its prefix, binding through the environment reaches
   it, and an empty bind clears the binding.
4. :func:`assert_collect_hands_over_and_forgets` — a collection hands over
   everything since the last one, and forgets it.
5. :func:`assert_every_file_belongs_to_a_bound_archiver` — step evidence comes
   from a bound archiver only; a batch carries at most one run record (JSON,
   owned by the capability or a bound key).
6. :func:`assert_files_carry_provenance` — every step file says when it was
   captured, inside the window it was produced in.
7. :func:`assert_a_broken_collection_never_looks_complete` — a collection
   that cannot collect raises, or names the loss in the batch's ``gaps``; it
   never returns a batch that looks complete.

Through the trace hook (``TraceWriter`` driven directly, negotiated and bound
exactly as ``AgentRuntime.prepare`` does — no agent, no loop, so any
capability fits):

8. :func:`assert_steps_commit_and_read_back` — several steps; what each
   step's collection handed over reads back byte for byte through
   ``RunTrace.archive_files`` / ``blob`` / ``archive_run_file``; every
   content-addressed body is written once.
9. :func:`assert_crash_window_hides_uncommitted_evidence` — a commit that
   never reached its manifest leaves its evidence on disk and invisible;
   hand-planted ``env/`` files are invisible. A failed sealed segment keeps
   its original identity; later segments may commit beyond its explicit gap.
10. :func:`assert_tail_lands_at_run_end` — evidence captured after the last
    commit point lands as trailing evidence at run end, claiming no step.
11. :func:`assert_a_failed_collection_leaves_the_trace_incomplete` — a step
    whose collection fails still commits, without the evidence, and its loss
    is a recording problem of that step; nothing is raised into execution.
    Later collection is still attempted, and the trace closes ``incomplete``
    independently of the run's successful outcome.
12. :func:`assert_empty_archive_costs_nothing` — a trace hook with no archive
    never touches the environment's archive surface and writes no ``env/``.
13. :func:`assert_run_records_stay_per_capability` — beside another archiving
    capability in the same environment, each capability's run records are
    counted, numbered and deduplicated as its own: one batch may carry one of
    each, and a record byte-equal to the other capability's is still
    committed.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

from conscribe.wiring import parse_wiring

from spectragent.core.archive import (
    RUN_RECORD_MEDIA_TYPE,
    ArchiveBatch,
    ArchiveFile,
    ArchiveProvenance,
    ArchiverBase,
)
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.negotiation import negotiate_archive
from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import (
    ENV_DIR,
    archive_blob_path,
    archive_run_path,
    archive_step_path,
    archive_trailing_path,
    sha256_hex,
)
from spectragent.core.trace.models import ArchiveRecord, TraceStatus
from spectragent.core.trace.reader import RunTrace
from tests._helpers.archive_fakes import (
    PEER_A,
    PEER_A_KEY,
    CollectSpy,
    ContractArchiveEnv,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeStepArchiver,
    PeerAArchiver,
    PeerArchivingCapability,
)
from tests._helpers.assembly import make_env
from tests._helpers.trace_builder import agent_result, step_record


@dataclass
class ArchiveHarness:
    """One archiving capability, ready to capture, with its bad day on a switch.

    Attributes:
        capability: The instance under contract — a capability that is also
            an ``ArchivingCapability``. Set up as far as *capturing* goes, and
            no further: no network, no browser, no container. A contract test
            must run anywhere, in milliseconds, or it stops being run.
        archivers: The archiver instances to bind — knobs and all, as the
            trace hook would hold them. Every key is ``<capability>.<id>``
            under this capability's prefix and provided by it. Clause 5 binds
            them one at a time as well as together, so a harness with two or
            more is what catches evidence leaking from one archiver into
            another.
        produce: ``produce(step)`` makes the capability capture what one step
            of activity would, for whichever archivers are bound. *step* is
            the caller's bookkeeping only — it must never reach the
            capability, which numbers what it captures itself.
        break_collect: Make every ``collect_archive`` from now on fail.
            Required, not optional: what a capability does on its bad day is
            half of this contract, and a harness that cannot produce one
            cannot check it.
        change_run_record: Optional. Make the next collection carry a run
            record whose content changed (the fake's ``change_run_record``;
            M29's fingerprint moving). When given, the crash-window clause
            puts a new run-record version into the step that never commits.
        close: Optional. Release what ``produce`` acquired (a capability that
            set up scopes and connections tears them down); the test fixture
            awaits it after every clause.
    """

    capability: Any
    archivers: tuple[ArchiverBase, ...]
    produce: Callable[[int], Awaitable[None]]
    break_collect: Callable[[], None]
    change_run_record: Callable[[], None] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    _env: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.archivers = tuple(self.archivers)
        if not self.archivers:
            raise ValueError(
                "An archive harness binds at least one archiver — the contract is about "
                "what a capability captures for the archivers it is bound to."
            )
        keys = [archiver.archiver_id for archiver in self.archivers]
        if len(set(keys)) != len(keys):
            raise ValueError(f"An archive harness names each archiver once; got {keys}.")

    @property
    def capability_id(self) -> str:
        return str(self.capability.capability_id)

    @property
    def keys(self) -> tuple[str, ...]:
        """The archiver keys, in harness order."""
        return tuple(archiver.archiver_id for archiver in self.archivers)

    def environment(self) -> ContractArchiveEnv:
        """The environment around :attr:`capability` — one per harness."""
        if self._env is None:
            self._env = make_env(ContractArchiveEnv, {self.capability_id: self.capability})
        return self._env

    async def aclose(self) -> None:
        """Run :attr:`close`, if the harness has one."""
        if self.close is not None:
            await self.close()


# ── Capability side: the archiving contract ───────────────────────────


def assert_collect_takes_no_step(harness: ArchiveHarness) -> None:
    """``collect_archive`` is a coroutine function that takes nothing — no step, no cycle."""
    collect = harness.capability.collect_archive
    assert inspect.iscoroutinefunction(collect), (
        f"{harness.capability_id}: collect_archive is not a coroutine function. The trace "
        f"hook awaits it at its commit points."
    )
    parameters = list(inspect.signature(collect).parameters)
    assert not parameters, (
        f"{harness.capability_id}: collect_archive takes {parameters}. It takes no argument "
        f"at all — no step, no cycle, not even an optional one. What step t's commit takes "
        f"belongs to step t: that is the recorder's rule, and a capability that is handed "
        f"a step number has been invited to attribute evidence itself."
    )


async def assert_unbound_capability_archives_nothing(harness: ArchiveHarness) -> None:
    """Never bound, or bound to nothing, a capability captures nothing at all.

    A run that records no evidence must cost nothing and leave nothing — and
    a capability that captures by default hands evidence of a run nobody
    asked to record to whoever collects next.
    """
    cap = harness.capability
    await harness.produce(0)
    never_bound = await cap.collect_archive()
    assert isinstance(never_bound, ArchiveBatch)
    assert never_bound.is_empty, (
        f"{harness.capability_id}: never bound, it captured "
        f"{_labels(never_bound.files)}. Nothing is archived until bind_archivers names "
        f"what to archive — capturing by default records a run nobody asked to record."
    )
    cap.bind_archivers([])
    await harness.produce(1)
    bound_to_nothing = await cap.collect_archive()
    assert bound_to_nothing.is_empty, (
        f"{harness.capability_id}: bound to nothing, it captured {_labels(bound_to_nothing.files)}."
    )


async def assert_bind_routes_by_prefix_and_clears(harness: ArchiveHarness) -> None:
    """Every key routes here by its prefix; binding through the environment reaches the
    capability; an empty bind clears the binding.

    Judged on what is captured *after* the clear: evidence handed over again
    from before it is clause 4's business, not this one's.
    """
    cap = harness.capability
    unroutable = [
        key
        for key in harness.keys
        if key.partition(".")[0] != harness.capability_id or key not in cap.provided_archivers
    ]
    assert not unroutable, (
        f"{harness.capability_id}: the harness binds {unroutable}, which do not route to this "
        f"capability — a key's leading segment names the capability that provides it, and the "
        f"capability's provided_archivers must list it."
    )
    env = harness.environment()
    env.bind_archivers(list(harness.archivers))
    await harness.produce(0)
    routed = await env.collect_archive()
    assert routed.files, (
        f"{harness.capability_id}: bound through the environment, produce(0) captured nothing "
        f"— the binding did not reach the capability, or the harness produces nothing."
    )
    seen = _Seen()
    seen.fresh(routed)

    env.bind_archivers([])
    await harness.produce(1)
    assert (await env.collect_archive()).is_empty
    leaked = seen.fresh(await cap.collect_archive())
    assert not leaked, (
        f"{harness.capability_id}: after an empty bind it still captured {_labels(leaked)}. "
        f"bind_archivers replaces the whole binding, and an empty list unbinds — the runtime "
        f"relies on it to clear a capability no archiver routes to."
    )


async def assert_collect_hands_over_and_forgets(harness: ArchiveHarness) -> None:
    """A collection hands over everything since the last one — and forgets it.

    The batch belongs to the caller from then on. A capability that keeps it
    hands the same evidence to the next step's commit, and the archive
    records one response under two steps.
    """
    cap = harness.capability
    cap.bind_archivers(list(harness.archivers))
    await harness.produce(0)
    first = await cap.collect_archive()
    assert isinstance(first, ArchiveBatch), (
        f"{harness.capability_id}: collect_archive returned {type(first).__name__}, not an "
        f"ArchiveBatch."
    )
    assert first.files, (
        f"{harness.capability_id}: with every archiver bound, produce(0) captured nothing — "
        f"this harness cannot show anything being handed over."
    )
    again = await cap.collect_archive()
    assert again.is_empty, (
        f"{harness.capability_id}: a second collection with nothing produced in between "
        f"handed over {_labels(again.files)} again. collect_archive hands over and forgets: "
        f"the batch is the caller's, and the next step's commit must not take it a second time."
    )
    await harness.produce(1)
    later = await cap.collect_archive()
    assert later.files, f"{harness.capability_id}: produce(1) captured nothing."
    repeated = [item for item in later.files if any(item is old for old in first.files)]
    assert not repeated, (
        f"{harness.capability_id}: {_labels(repeated)} came back in a later collection."
    )


async def assert_every_file_belongs_to_a_bound_archiver(harness: ArchiveHarness) -> None:
    """Step evidence comes only from bound archivers; at most one run record per batch.

    Bound one archiver at a time, then all together. Judged on what each
    binding newly captured — a file handed over again is clause 4's.
    """
    cap = harness.capability
    seen = _Seen()
    bindings: list[tuple[ArchiverBase, ...]] = [(archiver,) for archiver in harness.archivers]
    if len(harness.archivers) > 1:
        bindings.append(harness.archivers)
    for step, binding in enumerate(bindings):
        bound = {archiver.archiver_id for archiver in binding}
        cap.bind_archivers(list(binding))
        await harness.produce(step)
        batch = await cap.collect_archive()
        assert isinstance(batch, ArchiveBatch)
        problems: list[str] = []
        files = seen.fresh(batch)
        for item in files:
            label = f"{item.archiver_id}/{item.name}"
            if item.capability_id != harness.capability_id:
                problems.append(f"{label!r} carries another capability's prefix")
            if item.scope == "step" and item.archiver_id not in bound:
                problems.append(
                    f"{label!r} comes from archiver {item.archiver_id!r}, which is not bound "
                    f"(bound: {sorted(bound)})"
                )
            if item.scope == "run":
                if item.archiver_id not in bound | {harness.capability_id}:
                    problems.append(
                        f"run record {label!r} is owned by neither the capability nor a bound key"
                    )
                if item.media_type != RUN_RECORD_MEDIA_TYPE or item.content_addressed:
                    problems.append(f"run record {label!r} is not a plain JSON document")
        run_records = [item for item in files if item.scope == "run"]
        if len(run_records) > 1:
            problems.append(
                f"{len(run_records)} run records in one batch ({_labels(run_records)}); a batch "
                f"carries at most one per capability"
            )
        assert not problems, (
            f"{harness.capability_id}, bound to {sorted(bound)}: " + "; ".join(problems) + "."
        )


async def assert_files_carry_provenance(
    harness: ArchiveHarness, *, now: Callable[[], float]
) -> None:
    """Every step file says when it was captured, inside the window it was produced in.

    Evidence of one step is captured at different instants, and replay keys
    on those instants (M30/M31) — not on the step directory, whose buckets
    are approximate by construction. A run record may describe something
    captured earlier (a fingerprint taken at setup), so it is only held to
    "not in the future".
    """
    cap = harness.capability
    cap.bind_archivers(list(harness.archivers))
    started = now()
    await harness.produce(0)
    batch = await cap.collect_archive()
    finished = now()
    assert batch.files, f"{harness.capability_id}: produce(0) captured nothing."
    for item in batch.files:
        label = f"{item.archiver_id}/{item.name}"
        assert isinstance(item.meta, ArchiveProvenance), (
            f"{harness.capability_id}: {label!r} carries no ArchiveProvenance."
        )
        captured_at = item.meta.captured_at
        if item.scope == "step":
            assert started <= captured_at <= finished, (
                f"{harness.capability_id}: {label!r} claims to have been captured at "
                f"{captured_at}, outside the window [{started}, {finished}] it was produced "
                f"in. Stamp the capture itself, not the buffer or the binding."
            )
        else:
            assert captured_at <= finished, (
                f"{harness.capability_id}: run record {label!r} is stamped {captured_at}, "
                f"after it was collected ({finished})."
            )


async def assert_a_broken_collection_never_looks_complete(harness: ArchiveHarness) -> None:
    """A collection that cannot collect raises or names its gaps — never a batch that looks whole.

    The archive was explicitly requested; the trace hook records a collection
    failure separately from execution. A batch without a gap instead is a
    silent hole nobody finds until replay.
    """
    cap = harness.capability
    cap.bind_archivers(list(harness.archivers))
    await harness.produce(0)
    harness.break_collect()
    try:
        batch = await cap.collect_archive()
    except Exception:
        return
    if getattr(batch, "gaps", ()):
        return
    raise AssertionError(
        f"{harness.capability_id}: after break_collect(), collect_archive() returned "
        f"{type(batch).__name__} ({len(getattr(batch, 'files', ()))} file(s), no gap) instead "
        f"of raising or naming a gap. Either the harness does not really break the collection, "
        f"or the "
        f"capability swallowed the failure — a batch that looks complete while evidence is "
        f"missing is exactly what fail-closed collection exists to prevent."
    )


# ── Through the trace hook ────────────────────────────────────────────


async def assert_steps_commit_and_read_back(
    harness: ArchiveHarness, trace_root: Path, *, steps: int = 3
) -> None:
    """What each step's collection handed over reads back, byte for byte, under that step."""
    recording = _Recording(harness, trace_root)
    run_dir = await recording.start()
    for step in range(steps):
        await recording.step(step)
    await recording.close()

    trace = RunTrace(run_dir)
    batches = recording.spy.batches
    assert len(batches) == steps + 1, (
        f"{harness.capability_id}: {steps} commits and a close-out should collect "
        f"{steps + 1} times; collected {len(batches)}."
    )
    committed = 0
    for step, batch in enumerate(batches[:steps]):
        for key in harness.keys:
            handed = [
                item for item in batch.files if item.scope == "step" and item.archiver_id == key
            ]
            records = trace.archive_files(step, key)
            assert [_record_identity(r) for r in records] == [_file_identity(i) for i in handed], (
                f"{harness.capability_id}: step {step}'s commit of {key!r} reads back as "
                f"{[r.name for r in records]}, not what its collection handed over "
                f"({[i.name for i in handed]})."
            )
            for item, record in zip(handed, records, strict=True):
                # Filed where the on-disk rule says: a body under its digest,
                # anything else under the step whose commit took it.
                filed = (
                    archive_blob_path(record.sha256)
                    if item.content_addressed
                    else archive_step_path(step, key, item.name)
                )
                assert record.path == filed, (
                    f"{harness.capability_id}: step {step}'s {key}/{item.name} is filed at "
                    f"{record.path}, not {filed}."
                )
                assert trace.read_extra(record.path) == item.data
                if item.content_addressed:
                    assert trace.blob(record.sha256) == item.data
            committed += len(records)
    assert committed, (
        f"{harness.capability_id}: {steps} steps committed no step evidence at all — the "
        f"harness's produce() captured nothing, and this clause proved nothing."
    )

    # A run record is committed once per content: an identical re-emission is
    # dropped, a changed one is the next version — and the latest version is
    # what reads back.
    versions: list[bytes] = []
    for batch in batches:
        for item in batch.files:
            if item.scope == "run" and (not versions or versions[-1] != item.data):
                versions.append(item.data)
    latest = trace.archive_run_file(harness.capability_id)
    if not versions:
        assert latest is None
    else:
        assert latest is not None, f"{harness.capability_id}: its run record was not committed."
        assert latest.run_version == len(versions) - 1, (
            f"{harness.capability_id}: {len(versions)} distinct run-record contents were "
            f"handed over, the latest committed version is {latest.run_version}."
        )
        assert trace.read_extra(latest.path) == versions[-1]

    # Every content-addressed body is on disk exactly once, however many
    # steps referenced it.
    digests = {
        sha256_hex(item.data) for batch in batches for item in batch.files if item.content_addressed
    }
    blob_root = run_dir / Path(archive_blob_path("0" * 64)).parent.parent
    on_disk = sorted(
        path.relative_to(run_dir).as_posix() for path in blob_root.rglob("*") if path.is_file()
    )
    assert on_disk == sorted(archive_blob_path(digest) for digest in digests), (
        f"{harness.capability_id}: the blob store holds {len(on_disk)} file(s) for "
        f"{len(digests)} distinct content-addressed bodies."
    )


async def assert_crash_window_hides_uncommitted_evidence(
    harness: ArchiveHarness, trace_root: Path
) -> None:
    """A failed sealed segment stays invisible; subsequent steps can still commit.

    Disable retries for this clause so a persistent failure exhausts its budget.
    The orphan files remain invisible and must not be relabelled as trailing
    evidence when execution goes on to the next step.
    """
    recording = _Recording(harness, trace_root, write_retries=0)
    run_dir = await recording.start()
    await recording.step(0)
    before = _archive_view(RunTrace(run_dir), harness)

    await harness.produce(1)
    if harness.change_run_record is not None:
        harness.change_run_record()
    with _manifest_writes_fail():
        await recording.commit(1)
    assert any(
        problem.step == 1 and problem.phase == "commit" and problem.error == "OSError"
        for problem in recording.writer.health.problems
    ), "the failed write must be visible as recording health, not an execution exception"
    crashed = recording.spy.batches[-1]
    crashed_steps = [item for item in crashed.files if item.scope == "step"]
    assert crashed_steps, (
        f"{harness.capability_id}: produce(1) captured no step evidence — the crash window "
        f"would be checked on an empty step."
    )
    orphans = [
        archive_step_path(1, item.archiver_id, item.name)
        for item in crashed_steps
        if not item.content_addressed
    ]
    assert all((run_dir / path).is_file() for path in orphans), (
        "the failed commit did not write its evidence before the manifest — the window this "
        "clause checks was never opened"
    )
    committed_blobs = set(before["blobs"])
    new_bodies = {
        sha256_hex(item.data)
        for item in crashed_steps
        if item.content_addressed
        and archive_blob_path(sha256_hex(item.data)) not in committed_blobs
    }

    trace = RunTrace(run_dir)
    assert _archive_view(trace, harness) == before, (
        f"{harness.capability_id}: evidence of a commit that never reached its manifest is visible."
    )
    for path in orphans:
        _assert_not_committed(trace, path)
    for digest in new_bodies:
        _assert_no_blob(trace, digest)

    planted_paths, planted_digest = _plant_orphans(run_dir, harness)
    trace = RunTrace(run_dir)
    assert _archive_view(trace, harness) == before, "a file planted under env/ became visible"
    for path in planted_paths:
        _assert_not_committed(trace, path)
    _assert_no_blob(trace, planted_digest)

    await recording.step(2)
    await recording.close()
    trace = RunTrace(run_dir)
    assert [record.step for record in trace.step_records] == [0, 2]
    assert trace.step_count == 2
    assert trace.manifest.complete_through == 0
    assert trace.manifest.status == TraceStatus.INCOMPLETE
    assert trace.result is not None and trace.result.agent_done == agent_result().agent_done
    for key in harness.keys:
        assert trace.archive_files(1, key) == [], (
            f"{harness.capability_id}: uncommitted evidence of step 1 became visible."
        )
        assert not [
            record
            for step, record in trace.iter_archive(key, include_trailing=True)
            if step is None
        ], "a sealed failed step must never be reassigned to close-out"
    assert any(trace.archive_files(2, key) for key in harness.keys), (
        "a failed earlier write must not disable later evidence collection or persistence"
    )
    for path in planted_paths:
        _assert_not_committed(trace, path)
    _assert_no_blob(trace, planted_digest)


async def assert_tail_lands_at_run_end(harness: ArchiveHarness, trace_root: Path) -> None:
    """Evidence captured after the last commit point is trailing evidence: owed, and no step's."""
    recording = _Recording(harness, trace_root)
    run_dir = await recording.start()
    await recording.step(0)
    await harness.produce(1)  # activity after the last commit point
    await recording.close()

    tail = recording.spy.batches[-1]
    tail_steps = [item for item in tail.files if item.scope == "step"]
    assert tail_steps, (
        f"{harness.capability_id}: what was captured after the last commit point did not "
        f"reach the close-out collection."
    )
    trace = RunTrace(run_dir)
    assert trace.manifest.last_step_completed == 0, (
        "trailing evidence advanced last_step_completed — it belongs to no committed step"
    )
    assert trace.manifest.status == TraceStatus.COMPLETE
    # The tail is the close-out's own commit: the last trailing entry.
    position = len(trace.manifest.trailing) - 1
    for key in harness.keys:
        handed = [item for item in tail_steps if item.archiver_id == key]
        trailing = [
            record
            for step, record in trace.iter_archive(key, include_trailing=True)
            if step is None
        ]
        assert [_record_identity(r) for r in trailing] == [_file_identity(i) for i in handed], (
            f"{harness.capability_id}: the tail of {key!r} reads back as "
            f"{[r.name for r in trailing]}, not what the close-out collection handed over "
            f"({[i.name for i in handed]})."
        )
        for item, record in zip(handed, trailing, strict=True):
            filed = (
                archive_blob_path(record.sha256)
                if item.content_addressed
                else archive_trailing_path(position, key, item.name)
            )
            assert record.path == filed, (
                f"{harness.capability_id}: tail evidence {key}/{item.name} is filed at "
                f"{record.path}, not under its trailing entry ({filed})."
            )
            assert trace.read_extra(record.path) == item.data
        assert trace.archive_files(1, key) == [], (
            f"{harness.capability_id}: tail evidence of {key!r} claims step 1, which never "
            f"committed."
        )


async def assert_a_failed_collection_leaves_the_trace_incomplete(
    harness: ArchiveHarness, trace_root: Path
) -> None:
    """Collection errors leave gaps and diagnostics without stopping execution or losing steps.

    The capability is deliberately broken for the remainder of this run. Both
    later steps and close-out must still attempt capture; none may pretend that
    the missing evidence is a successfully collected empty archive, and none
    may take the step's own record down with its evidence.
    """
    recording = _Recording(harness, trace_root)
    run_dir = await recording.start()
    await recording.step(0)
    before = _archive_view(RunTrace(run_dir), harness)

    await harness.produce(1)
    harness.break_collect()
    await recording.commit(1)
    failures = [problem for problem in recording.writer.health.problems if problem.step == 1]
    assert failures and all(problem.phase == "collect" for problem in failures)
    assert any(problem.channel.partition(".")[0] == harness.capability_id for problem in failures)
    trace = RunTrace(run_dir)
    assert trace.step(1) is not None, (
        f"{harness.capability_id}: a failed collection took step 1's record down with its evidence"
    )
    view = _archive_view(trace, harness)
    assert view["evidence"] == before["evidence"]
    assert view["run_record"] == before["run_record"]

    collected = recording.spy.calls
    await recording.commit(2)
    await recording.close()
    trace = RunTrace(run_dir)
    assert trace.manifest.status == TraceStatus.INCOMPLETE
    assert trace.manifest.complete_through == 0
    assert recording.writer.health.missing_steps == []
    assert [entry.step for entry in trace.manifest.steps] == [0, 1, 2]
    assert recording.spy.calls == collected + 2, "later steps and close-out still collect"
    assert trace.result is not None and trace.result.agent_done == agent_result().agent_done
    assert _archive_view(trace, harness)["evidence"] == before["evidence"]


async def assert_empty_archive_costs_nothing(harness: ArchiveHarness, trace_root: Path) -> None:
    """A trace hook with no archive never touches the archive surface and writes no ``env/``."""
    counter = _ArchiveSurfaceCounter(harness.environment())
    writer = TraceWriter(trace_dir=str(trace_root / "traces"))
    await writer.on_run_start(env=counter, task="archive contract: nothing archived", config={})
    for step in range(2):
        await harness.produce(step)
        await writer.after_record_step(None, args=(step_record(step),), kwargs={})
    await writer.on_run_end(result=agent_result())
    await writer.on_run_error(error=RuntimeError("a late failure"), partial_steps=[])

    assert not counter.touched, (
        f"{harness.capability_id}: a trace hook with an empty archive touched "
        f"{counter.touched} — a run that records no evidence makes no archive call."
    )
    run_dir = writer.run_dir
    assert run_dir is not None
    assert not (run_dir / ENV_DIR).exists(), "an empty archive wrote env/"
    trace = RunTrace(run_dir)
    assert trace.step_count == 2
    assert trace.config.env_evidence_privacy is None
    for key in harness.keys:
        assert list(trace.iter_archive(key, include_trailing=True)) == []
    assert trace.archive_run_file(harness.capability_id) is None


async def assert_run_records_stay_per_capability(harness: ArchiveHarness, trace_root: Path) -> None:
    """Beside another archiving capability, this capability's run records stay its own.

    The environment merges every bound capability's batch into one, and run
    records are per capability (``docs/architecture_design.md`` §4.9, §5.3,
    §5.4): a batch carries at most one *per capability*, each capability's
    versions are numbered from its own ``env/run.<capability_id>.json``, and
    "unchanged" means equal to *that* capability's latest. With a single
    capability a writer that counted per batch, kept one version table for
    the run or knew only the first binding's capability would pass every
    other clause; this one puts a structural peer
    (:class:`~tests._helpers.archive_fakes.PeerArchivingCapability`) in the
    same environment. The peer hands over a run record of its own in the
    same collection as this capability's first, then one whose bytes are this
    capability's latest record — which must still be committed, as the peer's.
    """
    peer = PeerArchivingCapability(PEER_A, [PEER_A_KEY])
    env = make_env(ContractArchiveEnv, {harness.capability_id: harness.capability, "peer#0": peer})
    writer = TraceWriter(
        trace_dir=str(trace_root / "traces"), archive=[*harness.archivers, PeerAArchiver()]
    )
    negotiation = negotiate_archive([writer], env)
    env.bind_archivers(writer.archive)
    spy = CollectSpy(env)
    await writer.on_run_start(
        env=spy,
        task="archive contract: two archiving capabilities",
        config={},
        archive_negotiation=negotiation,
    )
    run_dir = writer.run_dir
    assert run_dir is not None

    peer.files += [peer.run_record(b'{"peer": "first"}'), peer.evidence(PEER_A_KEY)]
    await harness.produce(0)
    await writer.after_record_step(None, args=(step_record(0),), kwargs={})
    own = _run_records(spy.batches, harness.capability_id)
    peer.files.append(peer.run_record(own[-1] if own else b'{"peer": "second"}'))
    await harness.produce(1)
    await writer.after_record_step(None, args=(step_record(1),), kwargs={})
    await writer.on_run_end(result=agent_result())

    trace = RunTrace(run_dir)
    assert trace.manifest.status == TraceStatus.COMPLETE, (
        f"{harness.capability_id}: beside a second archiving capability the trace closed out "
        f"as {trace.manifest.status.value}."
    )
    for capability_id in (harness.capability_id, PEER_A):
        versions = _consecutive_distinct(_run_records(spy.batches, capability_id))
        latest = trace.archive_run_file(capability_id)
        if not versions:
            assert latest is None, f"{capability_id}: a run record nobody handed over"
            continue
        assert latest is not None, (
            f"{capability_id}: it handed over {len(versions)} distinct run record(s) and none "
            f"was committed — deduplicated against another capability's record, or refused as "
            f"one too many in a batch that carried a run record of each."
        )
        expected = (archive_run_path(capability_id, len(versions) - 1), len(versions) - 1)
        assert (latest.path, latest.run_version) == expected, (
            f"{capability_id}: its latest run record is {latest.path} (version "
            f"{latest.run_version}), not {expected[0]} (version {expected[1]}): versions are "
            f"numbered per capability."
        )
        for version, data in enumerate(versions):
            assert trace.read_extra(archive_run_path(capability_id, version)) == data, (
                f"{capability_id}: run record version {version} does not read back as handed over."
            )
    for step, batch in enumerate(spy.batches[:2]):
        for key in harness.keys:
            handed = [
                sha256_hex(item.data)
                for item in batch.files
                if item.scope == "step" and item.archiver_id == key
            ]
            assert [record.sha256 for record in trace.archive_files(step, key)] == handed, (
                f"{harness.capability_id}: beside a second archiving capability, step {step}'s "
                f"evidence of {key!r} did not read back as handed over."
            )


# ── Completeness ──────────────────────────────────────────────────────


def provided_archivers_of(capability_class: type) -> frozenset[str]:
    """The archivers a capability class provides — declared, or by its default instance.

    The class-level ``__wiring__["archiver"]`` declaration counts even when
    the class cannot be default-constructed; a default instance's
    ``provided_archivers`` counts too, for a capability that computes it.
    """
    declared: set[str] = set()
    for spec in parse_wiring(capability_class):
        if spec.registry_name == "archiver":
            declared |= set(spec.allowed_keys or ()) | set(spec.optional_keys or ())
    try:
        instance = capability_class()
    except Exception:
        return frozenset(declared)
    return frozenset(declared) | frozenset(getattr(instance, "provided_archivers", frozenset()))


def missing_harnesses(
    capability_classes: Iterable[type], factories: Mapping[str, Callable[[], ArchiveHarness]]
) -> list[str]:
    """Capability ids that provide archivers but have no harness factory, sorted.

    Pure: takes the classes and the factories explicitly, so the completeness
    guard can be shown failing on planted inputs.
    """
    missing: list[str] = []
    for cls in capability_classes:
        capability_id = getattr(cls, "__registry_key__", None) or getattr(cls, "capability_id", "")
        if provided_archivers_of(cls) and capability_id not in factories:
            missing.append(str(capability_id))
    return sorted(missing)


# ── Harnesses, one per archiving capability ───────────────────────────


def fake_archive_harness() -> ArchiveHarness:
    """The reference: the fake ``contract_archive`` capability and both its archivers."""
    cap = FakeArchivingCapability()

    def break_collect() -> None:
        cap.broken = True

    return ArchiveHarness(
        capability=cap,
        archivers=(FakeStepArchiver(), FakeBlobArchiver()),
        produce=cap.produce,
        break_collect=break_collect,
        change_run_record=cap.change_run_record,
    )


def browser_archive_harness() -> ArchiveHarness:
    """``BrowserCapability`` and its four recording archivers, against the fake browser.

    ``none`` tier with ``require_replayable: false`` (a pinned tier adds
    nothing the contract checks); ``produce`` runs one committed cycle, one
    action (``browser.frames`` captures before it), one network exchange and
    a storage change; ``break_collect`` drops the
    recorder connection; ``change_run_record`` changes the user agent the
    fingerprint measures. See ``tests/_helpers/browser_archive.py``.
    """
    from tests._helpers.browser_archive import archiving_capability

    capability, driver = archiving_capability()
    return ArchiveHarness(
        capability=capability,
        archivers=driver.archivers,
        produce=driver.produce,
        break_collect=driver.break_collect,
        change_run_record=driver.change_run_record,
        close=driver.close,
    )


#: capability_id → harness factory. **Add your archiving capability here.**
#:
#: A fresh harness per test: the clauses bind, capture and collect, and a
#: shared instance would carry one test's binding and buffer into the next.
HARNESS_FACTORIES: dict[str, Callable[[], ArchiveHarness]] = {
    "contract_archive": fake_archive_harness,
    "browser": browser_archive_harness,
}


# ── Internals ─────────────────────────────────────────────────────────


class _Recording:
    """A trace hook archiving the harness's capability, negotiated and bound as ``prepare`` does.

    ``negotiate_archive([writer], env)`` and ``env.bind_archivers(writer.archive)``
    are exactly what ``AgentRuntime.prepare`` does; the hook pulls from a
    :class:`CollectSpy` around the environment, so every batch it took is on
    record.
    """

    def __init__(
        self, harness: ArchiveHarness, trace_root: Path, *, write_retries: int = 2
    ) -> None:
        self.harness = harness
        self.env = harness.environment()
        self.writer = TraceWriter(
            trace_dir=str(trace_root / "traces"),
            archive=list(harness.archivers),
            write_retries=write_retries,
        )
        self.negotiation = negotiate_archive([self.writer], self.env)
        self.env.bind_archivers(self.writer.archive)
        self.spy = CollectSpy(self.env)

    async def start(self) -> Path:
        await self.writer.on_run_start(
            env=self.spy,
            task="archive contract",
            config={},
            archive_negotiation=self.negotiation,
            env_evidence_privacy=sorted(
                {
                    marker
                    for archiver in self.harness.archivers
                    for marker in archiver.privacy_markers
                }
            ),
        )
        assert self.writer.run_dir is not None
        return self.writer.run_dir

    async def commit(self, step: int) -> None:
        """Seal one step, then explicitly wait for persistence in this test harness."""
        await self.writer.after_record_step(None, args=(step_record(step),), kwargs={})
        await self.writer.flush()

    async def step(self, step: int) -> None:
        """One step of activity the capability captures, then its commit point."""
        await self.harness.produce(step)
        await self.commit(step)

    async def close(self) -> None:
        await self.writer.on_run_end(result=agent_result())


class _Seen:
    """Files already handed over, by identity — so a clause judges only what is new.

    Holds the objects themselves, so an id is never reused under it.
    """

    def __init__(self) -> None:
        self._files: dict[int, ArchiveFile] = {}

    def fresh(self, batch: ArchiveBatch) -> list[ArchiveFile]:
        new = [item for item in batch.files if id(item) not in self._files]
        for item in new:
            self._files[id(item)] = item
        return new


class _ArchiveSurfaceCounter:
    """An environment that records every touch of its archive surface, and forwards the rest."""

    ARCHIVE_SURFACE = ("provided_archivers", "bind_archivers", "collect_archive")

    def __init__(self, env: Any) -> None:
        self._env = env
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in self.ARCHIVE_SURFACE:
            self.touched.append(name)
        return getattr(self._env, name)


@contextmanager
def _manifest_writes_fail() -> Any:
    """Every ``manifest.json`` write fails — the crash lands between content and commit point."""
    real = fsio.write_durable

    def write_durable(path: Path, data: bytes) -> set[Path]:
        if path.name == "manifest.json":
            raise OSError("power lost before the commit point")
        return real(path, data)

    with mock.patch.object(fsio, "write_durable", write_durable):
        yield


#: Where hand-planted orphans go: a step, a trailing position and a run-record
#: version no recording in these clauses ever reaches.
_PLANTED_STEP = 900
_PLANTED_TRAILING = 90
_PLANTED_VERSION = 90


def _plant_orphans(run_dir: Path, harness: ArchiveHarness) -> tuple[list[str], str]:
    """Write files under ``env/`` that no index lists; return their paths and the blob's digest."""
    key = harness.keys[0]
    body = b"planted by the archive contract: committed by nobody"
    digest = sha256_hex(body)
    planted = {
        archive_step_path(_PLANTED_STEP, key, "planted.json"): b"{}",
        archive_trailing_path(_PLANTED_TRAILING, key, "planted.json"): b"{}",
        archive_run_path(harness.capability_id, _PLANTED_VERSION): b'{"planted": true}',
        archive_blob_path(digest): body,
    }
    for rel, data in planted.items():
        path = run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return list(planted), digest


def _archive_view(trace: RunTrace, harness: ArchiveHarness) -> dict[str, Any]:
    """Everything the archive accessors answer about this harness's capability."""
    run = trace.archive_run_file(harness.capability_id)
    rows = {key: list(trace.iter_archive(key, include_trailing=True)) for key in harness.keys}
    return {
        "last_step_completed": trace.manifest.last_step_completed,
        "evidence": {
            key: [(step, record.path, record.sha256) for step, record in found]
            for key, found in rows.items()
        },
        "blobs": sorted(
            {
                record.path
                for found in rows.values()
                for _, record in found
                if record.content_addressed
            }
        ),
        "run_record": None if run is None else (run.path, run.sha256, run.run_version),
    }


def _assert_not_committed(trace: RunTrace, path: str) -> None:
    try:
        trace.read_extra(path)
    except KeyError:
        return
    raise AssertionError(f"{path} reads back although no committed index lists it")


def _assert_no_blob(trace: RunTrace, digest: str) -> None:
    try:
        trace.blob(digest)
    except KeyError:
        return
    raise AssertionError(f"blob {digest} reads back although no committed index lists it")


def _file_identity(item: ArchiveFile) -> tuple[Any, ...]:
    return (
        item.archiver_id,
        item.name,
        sha256_hex(item.data),
        len(item.data),
        item.media_type,
        item.content_addressed,
        item.meta.captured_at,
        item.meta.cycle,
        dict(item.meta.ids),
    )


def _record_identity(record: ArchiveRecord) -> tuple[Any, ...]:
    return (
        record.archiver_id,
        record.name,
        record.sha256,
        record.size,
        record.media_type,
        record.content_addressed,
        record.captured_at,
        record.cycle,
        dict(record.ids),
    )


def _labels(files: Iterable[ArchiveFile]) -> list[str]:
    return [f"{item.archiver_id}/{item.name}" for item in files]


def _run_records(batches: Iterable[ArchiveBatch], capability_id: str) -> list[bytes]:
    """The run records *capability_id* handed over across *batches*, in order."""
    return [
        item.data
        for batch in batches
        for item in batch.files
        if item.scope == "run" and item.capability_id == capability_id
    ]


def _consecutive_distinct(records: Iterable[bytes]) -> list[bytes]:
    """The contents a run-record table commits: each one that differs from the one before."""
    versions: list[bytes] = []
    for data in records:
        if not versions or versions[-1] != data:
            versions.append(data)
    return versions
