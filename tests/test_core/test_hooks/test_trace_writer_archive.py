"""TraceWriter as the archive consumer: pull at the commit points, fail closed.

``docs/architecture_design.md`` §4.6 ("记录器从环境拉取证据") and §5.2–§5.4. Two
drivers, as in ``test_trace_writer.py``:

- **direct** — the hook's methods called the way the runtime calls them. The
  environment is either the fake archiving capability in its environment,
  negotiated and bound exactly as ``AgentRuntime.prepare`` would
  (:class:`Bench`), or a scripted raw environment whose ``collect_archive``
  hands over prepared batches (:class:`ScriptedEnv`) — for contract breaches,
  failures and cancellations;
- **a real** ``AgentRuntime`` over ``make_archive_run`` — for what only the
  runtime exercises: a failed collection ending the run, a failed close-out
  collection failing a run that succeeded, the zero-overhead path.

Everything is read back through ``RunTrace``'s archive accessors.
"""

import asyncio
import errno
import json
import unicodedata
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.weaver import Weaver
from spectragent.core.archive import ArchiveBatch, ArchiveContractError, ArchiveFile
from spectragent.core.benchmark_runner import BenchmarkRunner
from spectragent.core.environments.errors import ArchiveCollectionError
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops.errors import RunAbort
from spectragent.core.negotiation import ArchiveNegotiationResult, negotiate_archive
from spectragent.core.runtime import AgentRuntime
from spectragent.core.step_recorder import RecordingFailedError
from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import ARCHIVE_META_KEY, sha256_hex
from spectragent.core.trace.models import TraceManifest, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.task import TaskContext
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    PEER_A,
    PEER_A_KEY,
    RUN_RECORD_NAME,
    STEPS_KEY,
    UNPROVIDED_KEY,
    CollectSpy,
    ContractArchiveEnv,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeStepArchiver,
    PeerAArchiver,
    PeerArchivingCapability,
    SurfacelessEnv,
    make_archive_env,
    make_archive_run,
)
from tests._helpers.assembly import AGENT_ID, make_env
from tests._helpers.trace_builder import (
    agent_result,
    archive_file,
    archive_item,
    step_record,
    usage,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")

#: What a negotiation binding both fake archivers yields — for the scripted env.
BOTH_BOUND = ArchiveNegotiationResult(
    active_archivers=frozenset({STEPS_KEY, BLOBS_KEY}),
    archive_bindings={STEPS_KEY: CAPABILITY_ID, BLOBS_KEY: CAPABILITY_ID},
)


def _done() -> Any:
    return agent_result()


def _failed(error: str = "the run failed") -> Any:
    return agent_result(agent_done=AgentDoneStatus.ERROR, agent_reported_success=None, error=error)


async def _call(writer: TraceWriter, text: str = "go") -> None:
    """One LLM call through the advice, the way the AOP proxy issues it."""

    async def proceed() -> Any:
        return ChatInvokeCompletion(completion="ok", usage=usage(), stop_reason="end_turn")

    await writer.around_ainvoke(proceed=proceed, args=([UserMessage(content=text)],), kwargs={})


async def _commit(writer: TraceWriter, step: int) -> None:
    await writer.after_record_step(None, args=(step_record(step),), kwargs={})


def _manifest_on_disk(run_dir: Path) -> TraceManifest:
    return TraceManifest.model_validate_json((run_dir / "manifest.json").read_text())


def _files_under(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


class ScriptedEnv:
    """A raw environment whose collections hand over prepared outcomes, in order.

    An outcome that is an exception is raised; anything else is returned as
    is (so a breach can hand over something that is not an ``ArchiveBatch``).
    Past the script, every collection is an empty batch.
    """

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def collect_archive(self) -> Any:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else ArchiveBatch()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Bench:
    """A TraceWriter archiving the fake capability, negotiated and bound as ``prepare`` does."""

    def __init__(self, tmp_path: Path, *, archive: list[Any] | None = None) -> None:
        self.cap = FakeArchivingCapability()
        self.env: ContractArchiveEnv = make_archive_env(self.cap)
        self.writer = TraceWriter(
            trace_dir=str(tmp_path / "traces"),
            archive=archive if archive is not None else [FakeStepArchiver(), FakeBlobArchiver()],
        )
        self.negotiation = negotiate_archive([self.writer], self.env)
        self.env.bind_archivers(self.writer.archive)
        self.spy = CollectSpy(self.env)

    async def start(self) -> Path:
        await self.writer.on_run_start(
            env=self.spy,
            task="t",
            config={},
            archive_negotiation=self.negotiation,
        )
        assert self.writer.run_dir is not None
        return self.writer.run_dir

    async def step(self, step: int, *, call: str | None = None) -> None:
        """One step of activity the capability captures, then the commit point."""
        await self.cap.produce(step)
        if call is not None:
            await _call(self.writer, call)
        await _commit(self.writer, step)

    def trace(self) -> RunTrace:
        assert self.writer.run_dir is not None
        return RunTrace(self.writer.run_dir)


class TwoCapabilities:
    """The fake capability and a peer archiving capability, archived by one TraceWriter.

    Negotiated and bound as ``AgentRuntime.prepare`` does. The environment
    collects from both and merges the batches in capability order — the
    fake's files first — so one collection can carry a run record of each.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.cap = FakeArchivingCapability()
        self.peer = PeerArchivingCapability(PEER_A, [PEER_A_KEY])
        self.env = make_env(ContractArchiveEnv, {CAPABILITY_ID: self.cap, "peer#0": self.peer})
        self.writer = TraceWriter(
            trace_dir=str(tmp_path / "traces"), archive=[FakeStepArchiver(), PeerAArchiver()]
        )
        self.negotiation = negotiate_archive([self.writer], self.env)
        self.env.bind_archivers(self.writer.archive)
        self.spy = CollectSpy(self.env)

    async def start(self) -> Path:
        await self.writer.on_run_start(
            env=self.spy,
            task="t",
            config={},
            archive_negotiation=self.negotiation,
        )
        assert self.writer.run_dir is not None
        return self.writer.run_dir

    async def step(self, step: int, *peer_files: ArchiveFile) -> None:
        """The peer queues *peer_files*, the fake captures one step of activity, the commit point."""
        self.peer.files.extend(peer_files)
        await self.cap.produce(step)
        await _commit(self.writer, step)

    def handed_over(self, capability_id: str) -> list[bytes]:
        """The run records *capability_id* handed over, in collection order."""
        return [
            item.data
            for batch in self.spy.batches
            for item in batch.files
            if item.scope == "run" and item.capability_id == capability_id
        ]

    def trace(self) -> RunTrace:
        assert self.writer.run_dir is not None
        return RunTrace(self.writer.run_dir)


#: The fake capability's first run record, byte for byte.
_FAKE_RUN_RECORD_V0 = json.dumps(
    {"capability": CAPABILITY_ID, "generation": 0}, sort_keys=True
).encode("utf-8")


def _run_records(trace: RunTrace, capability_id: str) -> list[tuple[str, int | None, int]]:
    """``(path, version, generation)`` of every committed run record of *capability_id*, by version."""
    found = [
        (record.path, record.run_version, json.loads(trace.read_extra(record.path))["generation"])
        for rel in trace._extra_indexes
        for record in trace._archive_records(rel)
        if record.scope == "run" and record.capability_id == capability_id
    ]
    return sorted(found, key=lambda row: row[1] if row[1] is not None else -1)


def _lose_power_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next manifest write fails — the commit point is never reached — then writes work."""
    real = fsio.write_durable
    failed: list[Path] = []

    def write_durable(path: Path, data: bytes) -> set[Path]:
        if path.name == "manifest.json" and not failed:
            failed.append(path)
            raise OSError(errno.EIO, "power lost before the commit point", str(path))
        return real(path, data)

    monkeypatch.setattr(fsio, "write_durable", write_durable)


def _latest_run_record(trace: RunTrace, capability_id: str) -> tuple[str, int | None] | None:
    """``(path, version)`` of *capability_id*'s latest committed run record, if any."""
    latest = trace.archive_run_file(capability_id)
    return None if latest is None else (latest.path, latest.run_version)


async def _scripted_start(tmp_path: Path, env: ScriptedEnv) -> tuple[TraceWriter, Path]:
    writer = TraceWriter(
        trace_dir=str(tmp_path / "traces"), archive=[FakeStepArchiver(), FakeBlobArchiver()]
    )
    await writer.on_run_start(env=env, task="t", config={}, archive_negotiation=BOTH_BOUND)
    assert writer.run_dir is not None
    return writer, writer.run_dir


# ── the consumer's run start ──────────────────────────────────────────


class TestTheConsumerTakesTheRawEnvironment:
    async def test_an_empty_archive_touches_no_environment(self, tmp_path) -> None:
        env = ScriptedEnv()
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        await writer.on_run_start(env=env, task="t", config={})
        assert writer._env is None  # not even a reference
        await _call(writer)
        await _commit(writer, 0)
        await writer.on_run_end(result=_done())
        await writer.on_run_error(error=RuntimeError("late"), partial_steps=[])

        assert env.calls == 0
        assert writer.run_dir is not None
        assert not (writer.run_dir / "env").exists()
        assert RunTrace(writer.run_dir).step(0) is not None

    async def test_an_archive_negotiated_and_bound_is_pulled_from_the_raw_env(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        assert bench.writer._env is bench.spy
        await bench.step(0)
        assert bench.spy.calls == 1
        assert bench.cap.collect_calls == 1
        await bench.writer.on_run_end(result=_done())
        assert bench.writer._env is None  # released at close-out

    @pytest.mark.parametrize(
        ("negotiation", "env", "why"),
        [
            pytest.param(None, ScriptedEnv(), "archive_negotiation=None", id="no negotiation"),
            pytest.param(
                ArchiveNegotiationResult(
                    active_archivers=frozenset({STEPS_KEY}),
                    archive_bindings={STEPS_KEY: CAPABILITY_ID},
                ),
                ScriptedEnv(),
                "not this hook's archive",
                id="another archive",
            ),
            pytest.param(BOTH_BOUND, None, "env=None", id="no environment"),
        ],
    )
    async def test_an_archive_prepare_did_not_bind_is_refused_before_any_trace(
        self, tmp_path, negotiation, env, why
    ) -> None:
        writer = TraceWriter(
            trace_dir=str(tmp_path / "traces"), archive=[FakeStepArchiver(), FakeBlobArchiver()]
        )
        with pytest.raises(RuntimeError, match=r"AgentRuntime\.prepare") as exc_info:
            await writer.on_run_start(env=env, task="t", config={}, archive_negotiation=negotiation)
        assert why in str(exc_info.value)
        assert repr(sorted([STEPS_KEY, BLOBS_KEY])) in str(exc_info.value)
        assert writer.run_dir is None
        assert not (tmp_path / "traces").exists()


# ── evidence rides the step commit ────────────────────────────────────


class TestEvidenceRidesTheStepCommit:
    async def test_each_step_commits_exactly_what_its_collection_handed_over(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        for step in range(3):
            await bench.step(step)

        trace = bench.trace()
        assert len(bench.spy.batches) == 3
        for step, batch in enumerate(bench.spy.batches):
            handed = {
                (item.archiver_id, item.name, sha256_hex(item.data)): item
                for item in batch.files
                if item.scope == "step"
            }
            records = trace.archive_files(step, STEPS_KEY) + trace.archive_files(step, BLOBS_KEY)
            assert {(r.archiver_id, r.name, r.sha256) for r in records} == set(handed)
            for record in records:
                item = handed[(record.archiver_id, record.name, record.sha256)]
                assert (
                    record.media_type,
                    record.scope,
                    record.content_addressed,
                    record.captured_at,
                    record.cycle,
                    record.ids,
                    record.size,
                    record.run_version,
                ) == (
                    item.media_type,
                    item.scope,
                    item.content_addressed,
                    item.meta.captured_at,
                    item.meta.cycle,
                    dict(item.meta.ids),
                    len(item.data),
                    None,
                )
                assert trace.read_extra(record.path) == item.data
        # The capability never saw a step number; the cycle it stamped is its own.
        assert [r.path for r in trace.archive_files(1, STEPS_KEY)] == [
            "env/steps/001/contract_archive.steps/cycle_002.json"
        ]
        assert [r.cycle for r in trace.archive_files(1, STEPS_KEY)] == [2]

    async def test_a_body_two_steps_share_is_one_file_listed_by_both(self, tmp_path):
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        for step in range(3):  # actions 1 and 3 capture the same body
            await bench.step(step)

        trace = bench.trace()
        (first,) = trace.archive_files(0, BLOBS_KEY)
        (second,) = trace.archive_files(1, BLOBS_KEY)
        (third,) = trace.archive_files(2, BLOBS_KEY)
        assert first.path == third.path != second.path
        assert first.path.startswith("env/blobs/sha256/")
        assert len(_files_under(run_dir / "env" / "blobs")) == 2
        assert trace.blob(first.sha256) == trace.read_extra(first.path)

    async def test_the_pull_happens_before_the_manifest_commit(self, tmp_path):
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        seen: list[int] = []
        bench.spy.on_collect = lambda: seen.append(_manifest_on_disk(run_dir).last_step_completed)
        for step in range(3):
            await bench.step(step)
        # Step t's pull sees the manifest of step t-1: its commit comes after.
        assert seen == [-1, 0, 1]

    async def test_a_run_record_is_committed_once_per_content(self, tmp_path):
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        await bench.step(0)  # the first collection after the bind: version 0
        bench.cap.fake.run_record_due = True  # the same record again
        await bench.step(1)
        bench.cap.change_run_record()  # a changed one
        await bench.step(2)

        trace = bench.trace()
        assert [
            sum(1 for item in batch.files if item.scope == "run") for batch in bench.spy.batches
        ] == [1, 1, 1]
        assert sorted(path.name for path in (run_dir / "env").glob("run.*")) == [
            "run.contract_archive.1.json",
            "run.contract_archive.json",
        ]
        # The identical re-emission is in no index.
        assert not [
            record
            for record in trace.extra_files(1)
            if record.meta[ARCHIVE_META_KEY]["scope"] == "run"
        ]
        latest = trace.archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert (latest.path, latest.run_version, latest.name) == (
            "env/run.contract_archive.1.json",
            1,
            RUN_RECORD_NAME,
        )
        assert json.loads(trace.read_extra(latest.path)) == {
            "capability": CAPABILITY_ID,
            "generation": 1,
        }
        # Run records are not step evidence.
        assert trace.archive_files(0, CAPABILITY_ID) == []

    async def test_a_run_record_may_name_a_bound_archiver_instead_of_its_capability(
        self, tmp_path
    ) -> None:
        """A run record belongs to its capability, under its bare id or any bound key."""
        record = archive_item(STEPS_KEY, "fp.json", b'{"fp": 1}', scope="run")
        env = ScriptedEnv(ArchiveBatch((record,)))
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)

        latest = RunTrace(run_dir).archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert (latest.archiver_id, latest.path, latest.run_version) == (
            STEPS_KEY,
            "env/run.contract_archive.json",
            0,
        )

    async def test_the_trace_says_it_recorded_evidence(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        config = bench.trace().config
        assert config.negotiation is not None
        assert config.negotiation.active_archivers == [BLOBS_KEY, STEPS_KEY]
        assert config.negotiation.archive_bindings == {
            BLOBS_KEY: CAPABILITY_ID,
            STEPS_KEY: CAPABILITY_ID,
        }


class TestRunRecordsArePerCapability:
    """Run records are counted, versioned and deduplicated per capability.

    ``docs/architecture_design.md`` §4.9, §5.3, §5.4: a batch carries at most
    one run record *per capability*; a run record's capability may be any
    capability the run bound; each capability's first version is
    ``env/run.<capability_id>.json``; and "unchanged" means equal to the
    latest version of *that* capability. With a single archiving capability
    a writer that kept one count per batch, one version table for the run,
    or only the first binding's capability behaves exactly like this one —
    so the rules are pinned with two.
    """

    async def test_one_batch_carries_a_run_record_of_each_capability(self, tmp_path) -> None:
        two = TwoCapabilities(tmp_path)
        await two.start()
        await two.step(0, two.peer.run_record(b'{"peer": 0}'), two.peer.evidence(PEER_A_KEY))
        await two.writer.on_run_end(result=_done())

        assert sorted(
            item.capability_id for item in two.spy.batches[0].files if item.scope == "run"
        ) == [CAPABILITY_ID, PEER_A]
        trace = two.trace()
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert _latest_run_record(trace, CAPABILITY_ID) == ("env/run.contract_archive.json", 0)
        assert _latest_run_record(trace, PEER_A) == ("env/run.contract_archive_peer_a.json", 0)
        assert trace.read_extra("env/run.contract_archive_peer_a.json") == b'{"peer": 0}'
        assert [r.name for r in trace.archive_files(0, PEER_A_KEY)] == ["evidence.json"]

    async def test_each_capability_numbers_its_own_versions(self, tmp_path) -> None:
        two = TwoCapabilities(tmp_path)
        run_dir = await two.start()
        await two.step(0, two.peer.run_record(b'{"peer": 0}'))  # both: version 0
        await two.step(1, two.peer.run_record(b'{"peer": 1}'))  # the peer's changed: its v1
        two.cap.change_run_record()  # the fake's changed: its v1; the peer's comes again
        await two.step(2, two.peer.run_record(b'{"peer": 1}'))
        two.peer.files.append(two.peer.run_record(b'{"peer": 2}'))  # rides the tail: its v2
        await two.writer.on_run_end(result=_done())

        trace = two.trace()
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert _latest_run_record(trace, CAPABILITY_ID) == ("env/run.contract_archive.1.json", 1)
        assert _latest_run_record(trace, PEER_A) == ("env/run.contract_archive_peer_a.2.json", 2)
        assert sorted(path.name for path in (run_dir / "env").glob("run.*")) == [
            "run.contract_archive.1.json",
            "run.contract_archive.json",
            "run.contract_archive_peer_a.1.json",
            "run.contract_archive_peer_a.2.json",
            "run.contract_archive_peer_a.json",
        ]
        assert [
            trace.read_extra(f"env/run.contract_archive_peer_a{suffix}.json")
            for suffix in ("", ".1", ".2")
        ] == [b'{"peer": 0}', b'{"peer": 1}', b'{"peer": 2}']

    @pytest.mark.parametrize("when", ["in the same batch", "in a later batch"])
    async def test_a_record_equal_to_another_capabilitys_is_still_committed(
        self, tmp_path, when
    ) -> None:
        """Equal to the *other* capability's latest is not "unchanged": it is this one's first.

        Deduplicating against one table for the whole run dropped it without a
        word — the run completed, and ``archive_run_file`` had no record of
        that capability at all.
        """
        two = TwoCapabilities(tmp_path)
        await two.start()
        twin = two.peer.run_record(_FAKE_RUN_RECORD_V0)
        if when == "in the same batch":
            await two.step(0, twin)
        else:
            await two.step(0)
            await two.step(1, twin)
        await two.writer.on_run_end(result=_done())

        # The premise: the twin really is byte for byte the fake's own record.
        assert two.handed_over(CAPABILITY_ID) == [_FAKE_RUN_RECORD_V0]
        assert two.handed_over(PEER_A) == [_FAKE_RUN_RECORD_V0]
        trace = two.trace()
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert _latest_run_record(trace, CAPABILITY_ID) == ("env/run.contract_archive.json", 0)
        assert _latest_run_record(trace, PEER_A) == ("env/run.contract_archive_peer_a.json", 0)
        assert trace.read_extra("env/run.contract_archive_peer_a.json") == _FAKE_RUN_RECORD_V0


# ── the close-out ─────────────────────────────────────────────────────


class TestTheCloseOut:
    async def test_the_tail_is_trailing_evidence_and_claims_no_step(self, tmp_path):
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        for step in range(2):
            await bench.step(step)
        await bench.cap.produce(2)  # activity after the last commit point
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert trace.manifest.last_step_completed == 1
        assert trace.manifest.status == TraceStatus.COMPLETE
        (entry,) = trace.manifest.trailing
        assert entry.llm_calls == []
        assert entry.extra_index == "steps/trailing_00.extra.json"
        steps = [(step, r.path) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)]
        assert steps[-1] == (None, "env/trailing/00/contract_archive.steps/cycle_003.json")
        assert [step for step, _ in trace.iter_archive(STEPS_KEY)] == [0, 1]
        assert (run_dir / "env/trailing/00/contract_archive.steps/cycle_003.json").is_file()

    async def test_unfinished_calls_and_the_tail_are_separate_trailing_commits(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await _call(bench.writer, "step 1, never surrendered")
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        calls_entry, tail_entry = trace.manifest.trailing
        assert calls_entry.llm_calls == ["messages/001_call_00.json"]
        assert calls_entry.extra_index is None
        assert tail_entry.llm_calls == []
        assert tail_entry.extra_index == "steps/trailing_01.extra.json"
        assert [r.path for _, r in trace.iter_archive(STEPS_KEY, include_trailing=True)][-1] == (
            "env/trailing/01/contract_archive.steps/cycle_002.json"
        )
        assert trace.trailing_calls[0].messages[0].text == "step 1, never surrendered"

    async def test_the_close_out_collection_happens_at_most_once(self, tmp_path):
        """The rule itself, before anything releases the environment."""
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        first = await bench.writer._collect_tail()
        await bench.cap.produce(2)
        second = await bench.writer._collect_tail()

        assert first is not None
        assert [item.name for item in first.files if item.archiver_id == STEPS_KEY] == [
            "cycle_002.json"
        ]
        assert second is None
        assert bench.spy.calls == 2

    async def test_on_run_error_after_on_run_end_collects_nothing_more(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await bench.writer.on_run_end(result=_failed())
        collected = bench.spy.calls
        trailing = len(bench.trace().manifest.trailing)
        await bench.cap.produce(2)
        await bench.writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        assert bench.spy.calls == collected
        assert len(bench.trace().manifest.trailing) == trailing
        # The run failed — its result says so; the trace recorded all of it.
        assert bench.trace().manifest.status == TraceStatus.COMPLETE

    async def test_a_driver_that_only_dispatches_on_run_error_still_collects_once(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await _call(bench.writer, "lonely")
        await bench.writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])
        await bench.writer.on_run_error(error=RuntimeError("again"), partial_steps=[])

        trace = bench.trace()
        assert bench.spy.calls == 2
        assert bench.writer._env is None
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.trailing_calls[0].messages[0].text == "lonely"
        assert [step for step, _ in trace.iter_archive(STEPS_KEY, include_trailing=True)] == [
            0,
            None,
        ]

    async def test_a_failed_tail_on_a_run_that_succeeded_leaves_the_trace_incomplete(
        self, tmp_path
    ):
        """The outcome is decided: kept, and nothing raised — the trace says what it lost."""
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await _call(bench.writer, "the last word")
        bench.cap.broken = True
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        reason = trace.manifest.incomplete_reason or ""
        assert reason.startswith("the close-out evidence collection failed: ArchiveCollectionError")
        assert "broken on purpose" in reason
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE
        assert trace.trailing_calls[0].messages[0].text == "the last word"
        # And never again: ``on_run_error`` does not retry the collection.
        await bench.writer.on_run_error(error=RuntimeError("x"), partial_steps=[])
        assert bench.spy.calls == 2

    async def test_a_failed_tail_on_a_run_that_failed_is_logged_not_raised(self, tmp_path, caplog):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        bench.cap.broken = True
        await bench.writer.on_run_end(result=_failed("the run's own error"))

        assert bench.trace().manifest.status == TraceStatus.INCOMPLETE
        assert "the close-out evidence collection failed" in caplog.text

    @pytest.mark.parametrize("result", [_done, _failed], ids=["succeeded", "failed"])
    async def test_a_cancelled_tail_still_closes_the_trace_out_then_re_raises(
        self, tmp_path, result
    ) -> None:
        """Whatever the result: a failed run's cancellation is not a failure to log away.

        A Ctrl-C, or a sweep's ``asyncio.timeout``, landing while a failed
        task's tail is collected must still reach the caller — swallowed, the
        sweep would go on to the next task.
        """
        env = ScriptedEnv(ArchiveBatch(), asyncio.CancelledError())
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await _call(writer, "in flight at the cancellation")
        with pytest.raises(asyncio.CancelledError):
            await writer.on_run_end(result=result())

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.result is not None
        assert trace.trailing_calls[0].messages[0].text == "in flight at the cancellation"
        await writer.on_run_error(error=asyncio.CancelledError(), partial_steps=[])
        assert env.calls == 2

    @pytest.mark.parametrize("hook", ["on_run_end", "on_run_error"])
    async def test_a_cancelled_tail_outranks_a_close_out_that_fails_under_it(
        self, tmp_path, monkeypatch, caplog, hook
    ) -> None:
        """A full disk under a cancelled close-out is logged; the cancellation is what is raised.

        Raising the ``OSError`` instead would turn Ctrl-C into an ordinary task
        failure — a sweep's ``except Exception`` records it and starts the next
        task — and a caller's ``asyncio.timeout`` would not see its
        ``TimeoutError`` (``docs/architecture_design.md`` §4.2).
        """
        env = ScriptedEnv(ArchiveBatch(), asyncio.CancelledError())
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await _call(writer, "in flight at the cancellation")
        wal = writer._wal
        assert wal is not None

        def full_disk(**kwargs: Any) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(wal, "commit_trailing", full_disk)
        with pytest.raises(asyncio.CancelledError):
            if hook == "on_run_end":
                await writer.on_run_end(result=_done())
            else:  # a driver that dispatches only on_run_error
                await writer.on_run_error(error=asyncio.CancelledError(), partial_steps=[])

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        reason = trace.manifest.incomplete_reason or ""
        assert "the close-out evidence collection was cancelled" in reason
        assert "No space left on device" in reason
        assert "No space left on device" in caplog.text
        # Not committed, and dropped: the close-out happens once, and a later
        # one a driver dispatches retries nothing (``docs/architecture_design.md``
        # §5.2).
        assert trace.trailing_calls == []
        monkeypatch.undo()
        await writer.on_run_error(error=asyncio.CancelledError(), partial_steps=[])
        assert RunTrace(run_dir).trailing_calls == []

    async def test_the_environment_is_released_at_close_out(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.writer.on_run_end(result=_done())
        with pytest.raises(RecordingFailedError, match="closed out") as exc_info:
            await bench.step(1)
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert bench.spy.calls == 2


# ── one writer, several runs ──────────────────────────────────────────


async def _second_run(bench: Bench) -> RunTrace:
    """What a reused runtime does for its next task: ``prepare`` re-binds, then a run."""
    bench.env.bind_archivers(bench.writer.archive)
    await bench.writer.on_run_start(
        env=bench.spy,
        task="the next task",
        config={},
        run_id="second",
        archive_negotiation=bench.negotiation,
    )
    await bench.step(0)
    await bench.writer.on_run_end(result=_done())
    return bench.trace()


class TestAReusedWriterStartsEachRunClean:
    """One ``ComposedRun`` may drive many tasks, so one TraceWriter records many runs.

    Its archive state is per run (``docs/architecture_design.md`` §5.4) and
    ``on_run_start`` resets it: a run must not inherit the last one's
    closed-out archive, its collection failure, its run-record versions or
    the batches it still owed.
    """

    async def test_after_a_clean_close_out(self, tmp_path) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.writer.on_run_end(result=_done())

        second = await _second_run(bench)
        assert second.manifest.status == TraceStatus.COMPLETE
        assert [r.name for r in second.archive_files(0, STEPS_KEY)] == ["cycle_002.json"]
        # The same fingerprint as the first run's: in this trace, it is version 0.
        latest = second.archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert (latest.path, latest.run_version) == ("env/run.contract_archive.json", 0)

    async def test_after_a_failed_collection(self, tmp_path) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        bench.cap.broken = True
        await bench.writer.on_run_end(result=_failed())  # the close-out collection fails
        bench.cap.broken = False

        second = await _second_run(bench)
        assert second.step(0) is not None
        assert second.manifest.status == TraceStatus.COMPLETE

    async def test_without_the_batches_the_last_run_still_owed(self, tmp_path, monkeypatch):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        real = fsio.write_durable

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json":
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        with pytest.raises(RecordingFailedError):
            await _commit(bench.writer, 1)  # step 1's batch is owed now
        # The close-out cannot write either: logged, never raised; the trace
        # stays 'writing' and what it owed is dropped.
        await bench.writer.on_run_end(result=_failed())
        await bench.writer.on_run_error(error=OSError("disk gone"), partial_steps=[])
        assert bench.trace().manifest.status == TraceStatus.WRITING
        monkeypatch.setattr(fsio, "write_durable", real)

        second = await _second_run(bench)
        # The first run's cycle_002.json is that run's debt, never this trace's.
        assert [
            (step, r.name) for step, r in second.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_003.json")]

    async def test_through_a_runtime_that_drives_two_tasks(self, tmp_path) -> None:
        cap = FakeArchivingCapability()
        writer = _archiving_writer(tmp_path)
        runtime = _runtime(writer, cap=cap, steps=1)
        run_dirs = []
        for run_id in ("first", "second"):
            result = await runtime.execute("t", run_id=run_id, timeout=10)
            assert result.agent_done == AgentDoneStatus.DONE
            run_dirs.append(writer.run_dir)

        for run_dir in run_dirs:
            assert run_dir is not None
            trace = RunTrace(run_dir)
            assert trace.manifest.status == TraceStatus.COMPLETE
            assert len(trace.archive_files(0, STEPS_KEY)) == 1
            latest = trace.archive_run_file(CAPABILITY_ID)
            assert latest is not None and latest.run_version == 0


# ── fail closed ───────────────────────────────────────────────────────


def _forged(name: str) -> ArchiveFile:
    """An ``ArchiveFile`` whose name got past its own validation — the consumer checks it again."""
    item = archive_item(STEPS_KEY, "placeholder.json", b"{}")
    object.__setattr__(item, "name", name)
    return item


def _forged_id(value: str) -> ArchiveFile:
    """An ``ArchiveFile`` whose provenance id got past its own validation."""
    item = archive_item(STEPS_KEY, "ids.json", b"{}")
    object.__setattr__(item.meta, "ids", MappingProxyType({"frame": value}))
    return item


#: One name, two spellings a normalization-insensitive filesystem (APFS) stores as one file.
_CAFE_NFC = unicodedata.normalize("NFC", "café.json")
_CAFE_NFD = unicodedata.normalize("NFD", "café.json")


#: A batch that breaks the contract → a phrase of the error naming why.
BREACHES: dict[str, tuple[Any, str]] = {
    "step evidence of an unbound archiver": (
        ArchiveBatch((archive_item(UNPROVIDED_KEY, "x.json", b"{}"),)),
        "which this run did not bind",
    ),
    "a run record of a capability holding no bound archiver": (
        ArchiveBatch((archive_item("contract_archive_peer_a", "fp.json", b"{}", scope="run"),)),
        "holds none of this run's archivers",
    ),
    "a run record under an unbound key": (
        ArchiveBatch((archive_item(UNPROVIDED_KEY, "fp.json", b"{}", scope="run"),)),
        "which this run did not bind",
    ),
    "two run records of one capability": (
        ArchiveBatch(
            (
                archive_item(CAPABILITY_ID, "a.json", b"1", scope="run"),
                archive_item(CAPABILITY_ID, "b.json", b"2", scope="run"),
            )
        ),
        "at most one per capability",
    ),
    "two files on one path": (
        ArchiveBatch(
            (
                archive_item(STEPS_KEY, "Cycle.json", b"1"),
                archive_item(STEPS_KEY, "cycle.json", b"2"),
            )
        ),
        "land on the same path",
    ),
    "the same name spelled two ways": (
        ArchiveBatch(
            (
                archive_item(STEPS_KEY, _CAFE_NFC, b"1"),
                archive_item(STEPS_KEY, _CAFE_NFD, b"2"),
            )
        ),
        "land on the same path",
    ),
    "a body of an unbound archiver": (
        ArchiveBatch(
            (
                archive_item(
                    UNPROVIDED_KEY,
                    "b.bin",
                    b"a body nobody asked to record",
                    media_type="application/octet-stream",
                    content_addressed=True,
                ),
            )
        ),
        "which this run did not bind",
    ),
    "a path the WAL would refuse": (ArchiveBatch((_forged("a/../../b"),)), "segment"),
    "a name longer than a file name may be": (
        ArchiveBatch((_forged("n" * 256),)),
        "longer than the 255",
    ),
    "a name UTF-8 cannot encode": (
        ArchiveBatch((_forged("x\udc80y.json"),)),
        "not encodable as UTF-8",
    ),
    "an id UTF-8 cannot encode": (
        ArchiveBatch((_forged_id("\udc80"),)),
        "not encodable as UTF-8",
    ),
    "not a batch at all": ([archive_item(STEPS_KEY, "x.json", b"{}")], "not an ArchiveBatch"),
}


class TestFailClosed:
    @pytest.mark.parametrize("breach", list(BREACHES))
    async def test_a_batch_that_breaks_the_contract_commits_nothing_of_its_step(
        self, tmp_path, breach
    ) -> None:
        batch, why = BREACHES[breach]
        env = ScriptedEnv(batch)
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _call(writer, "the step's call")
        with pytest.raises(RecordingFailedError, match=why) as caught:
            await _commit(writer, 0)
        assert isinstance(caught.value.__cause__, ArchiveContractError)

        trace = RunTrace(run_dir)
        assert trace.manifest.last_step_completed == -1
        assert trace.step(0) is None
        assert not (run_dir / "env").exists()
        # A Loop that swallowed it gets no later step committed either.
        with pytest.raises(
            RecordingFailedError, match="since the collection at step 0"
        ) as exc_info:
            await _commit(writer, 1)
        cause = exc_info.value.__cause__
        assert isinstance(cause, RuntimeError)
        assert isinstance(cause.__cause__, ArchiveContractError)
        assert env.calls == 1
        # The close-out does not collect, keeps the call, and says incomplete
        # even for a run that reports success: the archive has a hole.
        await writer.on_run_end(result=_done())
        trace = RunTrace(run_dir)
        assert env.calls == 1
        assert trace.trailing_calls[0].messages[0].text == "the step's call"
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert (trace.manifest.incomplete_reason or "").startswith(
            "recording failed at step 0: ArchiveContractError"
        )

    @pytest.mark.parametrize("breach", list(BREACHES))
    @pytest.mark.parametrize("succeeded", [True, False], ids=["succeeded", "failed"])
    async def test_a_tail_that_breaks_the_contract_writes_nothing(
        self, tmp_path, caplog, breach, succeeded
    ) -> None:
        """The close-out batch is checked exactly like a step's, before a byte is written.

        It is where everything captured after the last commit point lands; an
        unchecked tail would commit an unbound archiver's evidence — a body
        the user left out on purpose — into a trace that then says
        ``complete``. Whatever the run's outcome, the breach is logged and
        named in the trace, never raised over the outcome.
        """
        batch, why = BREACHES[breach]
        env = ScriptedEnv(ArchiveBatch(), batch)
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await writer.on_run_end(result=_done() if succeeded else _failed())
        assert "ArchiveContractError" in caplog.text
        assert why in caplog.text
        assert why in (RunTrace(run_dir).manifest.incomplete_reason or "")

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.trailing == []
        assert not (run_dir / "env").exists()
        # Refused, not retried: a later close-out collects nothing more.
        await writer.on_run_error(error=RuntimeError("late"), partial_steps=[])
        assert env.calls == 2
        assert RunTrace(run_dir).manifest.trailing == []

    async def test_a_collection_that_raises_fails_its_step_and_every_later_commit(self, tmp_path):
        boom = RuntimeError("socket gone")
        env = ScriptedEnv(ArchiveBatch(), boom)
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await _call(writer, "step 1")
        with pytest.raises(RecordingFailedError, match="socket gone") as first:
            await _commit(writer, 1)
        with pytest.raises(
            RecordingFailedError, match="since the collection at step 1"
        ) as exc_info:
            await _commit(writer, 2)

        assert first.value.__cause__ is boom
        cause = exc_info.value.__cause__
        assert cause is not None and cause.__cause__ is boom
        assert env.calls == 2
        await writer.on_run_end(result=_failed())
        trace = RunTrace(run_dir)
        assert trace.manifest.last_step_completed == 0
        assert trace.step(1) is None
        assert trace.trailing_calls[0].messages[0].text == "step 1"
        assert env.calls == 2
        # The first hole is the trace's reason; the later refusal follows from it.
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        assert trace.manifest.incomplete_reason == (
            "recording failed at step 1: RuntimeError: socket gone"
        )

    async def test_a_cancelled_collection_is_not_a_collection_failure(self, tmp_path):
        tail = archive_item(STEPS_KEY, "tail.json", b"{}")
        env = ScriptedEnv(ArchiveBatch(), asyncio.CancelledError(), ArchiveBatch((tail,)))
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        with pytest.raises(asyncio.CancelledError):
            await _commit(writer, 1)
        await writer.on_run_end(result=_failed("Execution cancelled"))

        trace = RunTrace(run_dir)
        assert env.calls == 3  # the close-out still collected
        assert trace.step(1) is None
        # No collection failed, but step 1's record is lost: incomplete from there.
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.incomplete_reason == (
            "recording step 1 was cut off by a cancellation"
        )
        assert trace.manifest.complete_through == 0
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(None, "tail.json")]

    async def test_a_batch_whose_commit_failed_lands_as_trailing_evidence(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        await bench.step(0)
        bench.cap.change_run_record()
        await bench.cap.produce(1)
        await _call(bench.writer, "step 1")

        real = fsio.write_durable
        failed: list[Path] = []

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json" and not failed:
                failed.append(path)
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        with pytest.raises(RecordingFailedError, match="power lost"):
            await _commit(bench.writer, 1)
        orphan = "env/steps/001/contract_archive.steps/cycle_002.json"
        assert (run_dir / orphan).is_file()  # the failed attempt wrote it

        await bench.writer.on_run_end(result=_failed())
        trace = bench.trace()
        assert trace.step(1) is None
        assert trace.archive_files(1, STEPS_KEY) == []
        # The step's calls and its owed batch are separate trailing commits:
        # the calls first, so a batch the filesystem refuses cannot take them
        # down with it.
        calls_entry, batch_entry = trace.manifest.trailing
        assert (calls_entry.llm_calls, calls_entry.extra_index) == (
            ["messages/001_call_00.json"],
            None,
        )
        assert (batch_entry.llm_calls, batch_entry.extra_index) == (
            [],
            "steps/trailing_01.extra.json",
        )
        assert [r.path for _, r in trace.iter_archive(STEPS_KEY, include_trailing=True)] == [
            "env/steps/000/contract_archive.steps/cycle_001.json",
            "env/trailing/01/contract_archive.steps/cycle_002.json",
        ]
        latest = trace.archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert (latest.path, latest.run_version) == ("env/run.contract_archive.1.json", 1)
        with pytest.raises(KeyError):
            trace.read_extra(orphan)

    async def test_a_failed_trailing_commit_is_dropped_not_retried(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        wal = bench.writer._wal
        assert wal is not None
        real = wal.commit_trailing

        def failing(**kwargs: Any) -> None:
            raise OSError("disk hiccup")

        monkeypatch.setattr(wal, "commit_trailing", failing)
        await bench.writer.on_run_end(result=_done())  # never raised over the outcome
        # Not ``complete``: the trace does not hold everything the run owed it.
        trace = bench.trace()
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert "OSError: disk hiccup" in (trace.manifest.incomplete_reason or "")

        # Dropped, not retried: a later close-out a driver dispatches commits nothing.
        monkeypatch.setattr(wal, "commit_trailing", real)
        await bench.writer.on_run_error(error=OSError("disk hiccup"), partial_steps=[])
        trace = bench.trace()
        assert [r.name for _, r in trace.iter_archive(STEPS_KEY, include_trailing=True)] == [
            "cycle_001.json",
        ]
        assert bench.spy.calls == 2

    async def test_a_batch_that_can_never_be_written_costs_only_itself(
        self, tmp_path, monkeypatch
    ) -> None:
        """A commit that fails every time takes neither the calls nor the tail down with it.

        A failed step commit leaves its batch owed; if that batch cannot be
        committed for a reason no retry fixes, the unfinished step's calls
        and the evidence collected at close-out must still land — each owed
        thing is its own trailing commit (``docs/architecture_design.md``
        §5.2, "提交失败不丢调用").
        """
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await _call(bench.writer, "step 1, never committed")
        real_write = fsio.write_durable
        failed: list[Path] = []

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json" and not failed:
                failed.append(path)
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real_write(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        with pytest.raises(RecordingFailedError, match="power lost"):
            await _commit(bench.writer, 1)  # step 1's batch is owed now
        wal = bench.writer._wal
        assert wal is not None
        real_trailing = wal.commit_trailing

        def refuses_step_1s_batch(**kwargs: Any) -> None:
            if any("cycle_002" in item.rel_path for item in kwargs["extra_files"]):
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            real_trailing(**kwargs)

        monkeypatch.setattr(wal, "commit_trailing", refuses_step_1s_batch)
        await bench.cap.produce(2)  # the tail
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert "File name too long" in (trace.manifest.incomplete_reason or "")
        assert [call.messages[0].text for call in trace.trailing_calls] == [
            "step 1, never committed"
        ]
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (None, "cycle_003.json")]
        # A later close-out a driver dispatches retries nothing and commits nothing twice.
        trailing = len(trace.manifest.trailing)
        await bench.writer.on_run_error(error=OSError("x"), partial_steps=[])
        assert len(bench.trace().manifest.trailing) == trailing

    async def test_a_trailing_run_record_is_numbered_after_an_owed_one(
        self, tmp_path, monkeypatch
    ) -> None:
        """Versions taken by a trailing commit stay taken for the next one."""
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)  # run record version 0
        bench.cap.change_run_record()  # generation 1 rides step 1's batch
        await bench.cap.produce(1)
        real = fsio.write_durable
        failed: list[Path] = []

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json" and not failed:
                failed.append(path)
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        with pytest.raises(RecordingFailedError, match="power lost"):
            await _commit(bench.writer, 1)  # owed: generation 1 becomes version 1 at close-out
        bench.cap.change_run_record()  # generation 2 rides the tail
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        latest = trace.archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert (latest.path, latest.run_version) == ("env/run.contract_archive.2.json", 2)
        assert json.loads(trace.read_extra(latest.path))["generation"] == 2
        assert trace.manifest.status == TraceStatus.INCOMPLETE

    async def test_an_owed_run_record_older_than_a_committed_one_is_not_the_latest(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """Versions follow capture order, not commit order.

        A driver that kept going after a failed step commit (``_flush_trailing``
        names it) commits step 2's newer record first; the close-out then
        commits the batch step 1 still owes. Its record is the older one:
        committed as the next version, it would read back as the latest
        fingerprint, and replay or branching would rebuild the environment
        from a state it had already left.
        """
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)  # generation 0: version 0
        bench.cap.change_run_record()  # generation 1 rides step 1's batch
        await bench.cap.produce(1)
        _lose_power_once(monkeypatch)
        with pytest.raises(RecordingFailedError, match="power lost"):
            await _commit(bench.writer, 1)  # owed now
        bench.cap.change_run_record()  # generation 2 rides step 2's batch
        await bench.step(2)
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        assert _run_records(trace, CAPABILITY_ID) == [
            ("env/run.contract_archive.json", 0, 0),
            ("env/run.contract_archive.1.json", 1, 2),
        ]
        latest = trace.archive_run_file(CAPABILITY_ID)
        assert latest is not None and json.loads(trace.read_extra(latest.path))["generation"] == 2
        # Only the stale record is left out: the owed batch's evidence still lands.
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (2, "cycle_003.json"), (None, "cycle_002.json")]
        assert "collection 3's run record of capability 'contract_archive' is newer" in caplog.text

    async def test_an_unchanged_record_still_counts_as_the_latest_capture(
        self, tmp_path, monkeypatch
    ) -> None:
        """Dropping a re-emission that equals the latest version still moves "latest" forward.

        Step 2's capability is back in the state version 0 recorded, so its
        record adds nothing and is dropped — but it is the newest capture,
        and the generation-1 record step 1 still owes is older than it.
        """
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)  # generation 0: version 0
        bench.cap.change_run_record()  # generation 1 rides step 1's batch
        await bench.cap.produce(1)
        _lose_power_once(monkeypatch)
        with pytest.raises(RecordingFailedError, match="power lost"):
            await _commit(bench.writer, 1)
        bench.cap.fake.run_generation = 0  # back to what version 0 recorded
        bench.cap.fake.run_record_due = True
        await bench.step(2)
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        assert _run_records(trace, CAPABILITY_ID) == [("env/run.contract_archive.json", 0, 0)]

    async def test_every_owed_batch_lands_in_a_trailing_commit_of_its_own(
        self, tmp_path, monkeypatch
    ) -> None:
        """A driver that kept going after failed commits owes several batches; none is dropped."""
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        real = fsio.write_durable
        failing = {"on": True}

        def write_durable(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json" and failing["on"]:
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", write_durable)
        await bench.cap.produce(1)
        with pytest.raises(RecordingFailedError):
            await _commit(bench.writer, 1)
        await bench.cap.produce(2)
        with pytest.raises(RecordingFailedError):
            await _commit(bench.writer, 1)  # a second owed batch
        failing["on"] = False
        await bench.writer.on_run_end(result=_failed())

        rows = [
            (step, r.path)
            for step, r in bench.trace().iter_archive(STEPS_KEY, include_trailing=True)
        ]
        assert rows == [
            (0, "env/steps/000/contract_archive.steps/cycle_001.json"),
            (None, "env/trailing/00/contract_archive.steps/cycle_002.json"),
            (None, "env/trailing/01/contract_archive.steps/cycle_003.json"),
        ]


# ── through a real AgentRuntime ───────────────────────────────────────


def _runtime(writer: TraceWriter, *, cap: Any = None, env: Any = None, steps: int = 1) -> Any:
    run = make_archive_run(hooks=[writer], capability=cap, env=env, steps=steps)
    return AgentRuntime.for_agent(run, AGENT_ID)


def _archiving_writer(tmp_path: Path) -> TraceWriter:
    return TraceWriter(
        trace_dir=str(tmp_path / "traces"), archive=[FakeStepArchiver(), FakeBlobArchiver()]
    )


def _break_after(cap: FakeArchivingCapability, collections: int) -> None:
    """Let *collections* collections through, then make every later one raise."""
    real = cap.collect_archive

    async def collect_archive() -> ArchiveBatch:
        batch = await real()
        if cap.collect_calls >= collections:
            cap.broken = True
        return batch

    cap.__dict__["collect_archive"] = collect_archive


class _PullSpy(HookBase):
    """Intercepts ``collect_archive`` on the woven environment — and must never see the pull."""

    __registry_key__ = "contract_archive_pull_spy"
    __intercepts__ = [Intercept("collect_archive", role="env", advice="before")]

    def __init__(self) -> None:
        super().__init__()
        self.seen = 0

    async def before_collect_archive(self, *, args: tuple, kwargs: dict, **ctx: Any) -> None:
        self.seen += 1


class _RiskGuard(HookBase):
    """A safety guard woven onto ``env.act``: vetoes the act that lands on a risk-control page.

    What else that moment breaks is *breaks*: by default the capability's
    evidence capture — the collection at the vetoed step fails, which is
    exactly when a veto must not get lost.
    """

    __registry_key__ = "contract_archive_risk_guard"
    __intercepts__ = [Intercept("act", role="env", advice="after")]

    def __init__(
        self,
        cap: FakeArchivingCapability,
        *,
        veto_at: int,
        breaks: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.cap = cap
        self.veto_at = veto_at
        self.breaks = breaks
        self.acts = 0

    @property
    def blocking(self) -> bool:
        return True  # a guard that vetoes must be blocking, or its veto is logged and dropped

    async def after_act(self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any) -> None:
        self.acts += 1
        if self.acts - 1 == self.veto_at:
            if self.breaks is None:
                self.cap.broken = True
            else:
                self.breaks()
            raise RunAbort("risk-control page detected", source="risk_guard")


class _FillingDisk:
    """The trace disk: full from the moment :meth:`fill` is called, and full for good.

    *refuses* is what a full disk refuses (``ENOSPC``):

    - ``"commits"`` — the WAL's step and trailing commits, while the small
      ``result.json`` and manifest rewrites still fit, so the trace can still
      say ``incomplete``;
    - ``"every write"`` — every durable write, the manifest included, which
      therefore stays ``writing``: nothing can be written at all.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, refuses: str) -> None:
        self.full = False
        if refuses == "commits":
            for name in ("commit_step", "commit_trailing"):
                monkeypatch.setattr(WALWriter, name, self._refusing(getattr(WALWriter, name)))
        else:
            monkeypatch.setattr(fsio, "write_durable", self._refusing(fsio.write_durable))

    def fill(self) -> None:
        self.full = True

    def _refusing(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def write(*args: Any, **kwargs: Any) -> Any:
            if self.full:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*args, **kwargs)

        return write


def _dies_at_cycle(
    cap: FakeArchivingCapability, cycle: int, *, breaks: Callable[[], None] | None = None
) -> None:
    """Observing *cycle* fails like a dead CDP socket — and the same death hits collection.

    *breaks* is what it does to collection: by default the next one raises.
    """
    real = cap.resolve_fragment

    async def resolve_fragment(fragment_key: str) -> Any:
        if cap.fake.cycle >= cycle:
            if breaks is None:
                cap.broken = True
            else:
                breaks()
            raise ConnectionError("CDP websocket closed")
        return await real(fragment_key)

    cap.__dict__["resolve_fragment"] = resolve_fragment


def _stalls_next_collection(cap: FakeArchivingCapability) -> Callable[[], None]:
    """Arm a stall: once armed, the next collection hangs until it is cancelled.

    A collection of a heavy page — the challenge page a guard refused — that
    outlasts the run's own timeout. It never reaches the capability, so what
    it would have taken is still there for the close-out.
    """
    real = cap.collect_archive
    armed = {"on": False}

    async def collect_archive() -> ArchiveBatch:
        if armed["on"]:
            armed["on"] = False
            await asyncio.Event().wait()  # until the run's timeout cancels it
        return await real()

    cap.__dict__["collect_archive"] = collect_archive
    return lambda: armed.__setitem__("on", True)


class _SweepLoader:
    """Two tasks, duck-typed the way ``BenchmarkRunner`` reads a loader."""

    benchmark_name = "contract_archive_bench"
    subset = ""

    class _Task:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id
            self.task_text = f"do {task_id}"

    def task_ids(self) -> list[str]:
        return ["t-1", "t-2"]

    def __getitem__(self, task_id: str) -> Any:
        return self._Task(task_id)

    def get_task_context(self, task_id: str) -> TaskContext:
        return TaskContext()


class TestThroughTheRuntime:
    async def test_a_run_records_each_steps_evidence_and_its_run_record(self, tmp_path):
        cap = FakeArchivingCapability()
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute("t", timeout=10)

        assert result.agent_done == AgentDoneStatus.DONE
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert [(step, r.name) for step, r in trace.iter_archive(STEPS_KEY)] == [
            (0, "cycle_001.json"),
            (1, "cycle_002.json"),
            (2, "cycle_003.json"),
        ]
        assert [len(trace.archive_files(step, BLOBS_KEY)) for step in range(3)] == [1, 1, 1]
        run = trace.archive_run_file(CAPABILITY_ID)
        assert run is not None and run.run_version == 0
        # Three step pulls and one close-out pull; nothing was left for the tail.
        assert cap.collect_calls == 4
        assert trace.manifest.trailing == []
        assert trace.config.env_evidence_privacy is not None

    @pytest.mark.parametrize("failing_step", [0, 1])
    async def test_a_failed_collection_stops_the_run_and_raises(self, tmp_path, failing_step):
        cap = FakeArchivingCapability()
        if failing_step == 0:
            cap.broken = True
        else:
            _break_after(cap, failing_step)
        writer = _archiving_writer(tmp_path)
        with pytest.raises(
            RecordingFailedError, match=f"recording failed at step {failing_step}: Archive"
        ) as caught:
            await _runtime(writer, cap=cap, steps=3).execute("t", timeout=10, raise_on_error=True)
        assert isinstance(caught.value.__cause__, ArchiveCollectionError)
        assert "broken on purpose" in str(caught.value)

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.last_step_completed == failing_step - 1
        assert trace.manifest.complete_through == failing_step - 1
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.ERROR
        assert (trace.result.error or "").startswith(f"recording failed at step {failing_step}")
        assert trace.step(failing_step) is None
        assert trace.archive_files(failing_step, STEPS_KEY) == []
        assert [call.step for call in trace.trailing_calls] == [failing_step]
        # No further collection: not for a later step, not for the tail.
        assert cap.collect_calls == failing_step + 1

    async def test_without_raise_on_error_the_result_names_the_recording_failure(
        self, tmp_path
    ) -> None:
        cap = FakeArchivingCapability()
        cap.broken = True
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute(
            "t", timeout=10, raise_on_error=False
        )

        assert result.agent_done == AgentDoneStatus.ERROR
        assert result.error is not None
        assert result.error.startswith("recording failed at step 0: ArchiveCollectionError")
        assert writer.run_dir is not None
        assert RunTrace(writer.run_dir).manifest.status == TraceStatus.INCOMPLETE

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_hole_on_the_done_step_keeps_the_answer(self, tmp_path, raise_on_error):
        """The agent's done is decided before its step is recorded: the hole never rewrites it.

        Two ledgers: result.json keeps ``done`` and the answer; the trace is
        incomplete, complete through the step before, and says why. Nothing
        is raised — the run was ending anyway.
        """
        cap = FakeArchivingCapability()
        cap.broken = True  # step 0 is the done step, and its collection fails
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=1).execute(
            "t", timeout=10, raise_on_error=raise_on_error
        )

        assert result.agent_done == AgentDoneStatus.DONE
        assert (result.answer, result.error) == ("42", None)
        assert result.action_history
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.result is not None and trace.result.answer == "42"
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == -1
        assert (trace.manifest.incomplete_reason or "").startswith(
            "recording failed at step 0: ArchiveCollectionError"
        )
        assert trace.step(0) is None
        assert [call.step for call in trace.trailing_calls] == [0]
        assert cap.collect_calls == 1  # no tail after the hole

    async def test_a_failed_terminal_observation_keeps_the_outcome(self, tmp_path):
        """The terminal step is a tail too: the run's done stands, the trace says what it lost."""
        from spectragent.core.loops import ReactLoop
        from tests._helpers.archive_fakes import (
            ContractArchiveAgent,
            ContractArchiveLLM,
            ContractArchiveMemory,
        )
        from tests._helpers.assembly import make_run

        cap = FakeArchivingCapability()
        _break_after(cap, 1)  # step 0's pull works, the terminal step's does not
        writer = _archiving_writer(tmp_path)
        run = make_run(
            agent=ContractArchiveAgent(keep_going_steps=0),
            loop=ReactLoop(max_steps=5, terminal_observation=True),
            env=make_archive_env(cap),
            llm=ContractArchiveLLM(),
            memory=ContractArchiveMemory(),
            hooks=[writer],
        )
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "t", timeout=10, raise_on_error=True
        )

        assert (result.agent_done, result.answer, result.error) == (
            AgentDoneStatus.DONE,
            "42",
            None,
        )
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0  # the policy step is whole
        assert (trace.manifest.incomplete_reason or "").startswith(
            "recording failed at step 1: ArchiveCollectionError"
        )
        assert trace.step(1) is None

    async def test_a_failed_tail_on_a_successful_run_keeps_its_outcome(self, tmp_path):
        """Not raised from ``execute``: the outcome is decided; the trace says what it lost."""
        cap = FakeArchivingCapability()
        _break_after(cap, 1)  # step 0's pull works, the close-out's does not
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=1).execute(
            "t", timeout=10, raise_on_error=True
        )

        assert result.agent_done == AgentDoneStatus.DONE
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        assert "the close-out evidence collection failed" in (
            trace.manifest.incomplete_reason or ""
        )
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE
        assert trace.step(0) is not None
        assert [r.name for _, r in trace.iter_archive(STEPS_KEY)] == ["cycle_001.json"]

    async def test_a_veto_at_a_step_whose_collection_fails_is_still_a_veto(self, tmp_path):
        """The veto propagates past ``raise_on_error=False``; the collection failure rides along.

        The recording of the vetoed step fails — fail closed, so the step is
        not committed and the trace closes ``incomplete`` — but that failure must
        not take the veto's place: ``execute`` would then return an ordinary
        error result, and a sweep would start its next task.
        """
        cap = FakeArchivingCapability()
        writer = _archiving_writer(tmp_path)
        guard = _RiskGuard(cap, veto_at=1)
        run = make_archive_run(hooks=[writer, guard], capability=cap, steps=3)
        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, raise_on_error=False
            )

        assert any("ArchiveCollectionError" in note for note in caught.value.__notes__)
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.last_step_completed == 0
        assert trace.step(1) is None  # its collection failed: not committed
        assert [call.step for call in trace.trailing_calls] == [1]
        assert trace.result is not None and "risk-control page detected" in (
            trace.result.error or ""
        )
        assert trace.result.agent_done == AgentDoneStatus.VETOED
        assert cap.collect_calls == 2  # step 0, the vetoed step 1 — and no tail

    async def test_an_infrastructure_failure_at_such_a_step_still_names_its_root_cause(
        self, tmp_path
    ) -> None:
        cap = FakeArchivingCapability()
        _dies_at_cycle(cap, 2)
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute(
            "t", timeout=10, raise_on_error=False
        )

        assert result.agent_done == AgentDoneStatus.ERROR
        assert result.error is not None
        root_cause, _, unrecorded = result.error.partition(" (recording this step failed too: ")
        assert root_cause.startswith("observe failed at step 1: ObservationFailedError")
        assert "ConnectionError: CDP websocket closed" in root_cause
        assert unrecorded.startswith(
            "RecordingFailedError: recording failed at step 1: ArchiveCollectionError"
        )
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.step(1) is None
        assert trace.result is not None and trace.result.error == result.error

    async def test_such_a_veto_still_stops_the_sweep(self, tmp_path) -> None:
        caps: list[FakeArchivingCapability] = []

        def compose() -> Any:
            cap = FakeArchivingCapability()
            caps.append(cap)
            hooks: list[Any] = [_archiving_writer(tmp_path)]
            if len(caps) == 1:  # the first task lands on the risk-control page
                hooks.append(_RiskGuard(cap, veto_at=0))
            return make_archive_run(hooks=hooks, capability=cap, steps=2)

        summary = await BenchmarkRunner(loader=_SweepLoader(), compose=compose, timeout=10).run()

        assert summary.was_aborted
        assert summary.aborted is not None and "risk-control page detected" in summary.aborted
        (vetoed,) = summary.outcomes  # shown, as the task that stopped the sweep
        assert vetoed.agent_done == "vetoed"
        assert len(caps) == 1  # the next task never started

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_a_veto_whose_step_the_runs_timeout_cuts_off_is_still_a_veto(
        self, tmp_path, caplog, raise_on_error
    ) -> None:
        """The run's own timeout lands in the vetoed step's collection; the veto still wins.

        Collecting the vetoed step's evidence is the first thing on the veto's
        way out that can take long — a heavy challenge page — and the run's
        timeout used to replace the veto there: ``execute`` returned an
        ordinary timeout result whatever ``raise_on_error`` said, and nothing
        anywhere said a guard had said stop (``docs/architecture_design.md``
        §4.2, "否决总是上抛").
        """
        cap = FakeArchivingCapability()
        writer = _archiving_writer(tmp_path)
        guard = _RiskGuard(cap, veto_at=1, breaks=_stalls_next_collection(cap))
        run = make_archive_run(hooks=[writer, guard], capability=cap, steps=3)
        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=0.5, raise_on_error=raise_on_error
            )

        assert caught.value.__notes__ == [
            "The run's own timeout (0.5s) expired while the vetoed step was still being "
            "recorded; the veto stands."
        ]
        assert "run aborted during act at step 1" in caplog.text
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.last_step_completed == 0
        assert trace.step(1) is None  # cut off: never committed
        assert [call.step for call in trace.trailing_calls] == [1]
        # The stalled collection took nothing; the close-out took the vetoed page's evidence.
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (None, "cycle_002.json")]
        assert trace.manifest.incomplete_reason == (
            "recording step 1 was cut off by a cancellation"
        )
        assert trace.result is not None
        assert trace.result.agent_done == AgentDoneStatus.VETOED
        assert trace.result.error == (
            "RunAbort: risk_guard: risk-control page detected (the run's timeout of 0.5s "
            "expired while the vetoed step was being recorded)"
        )
        assert cap.collect_calls == 2  # step 0 and the close-out

    async def test_an_infrastructure_failure_whose_step_the_timeout_cuts_off_names_its_cause(
        self, tmp_path
    ) -> None:
        """The run had ended on a dead browser; the timeout must not report a slow run."""
        cap = FakeArchivingCapability()
        _dies_at_cycle(cap, 2, breaks=_stalls_next_collection(cap))
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute(
            "t", timeout=0.5, raise_on_error=True
        )

        assert result.agent_done == AgentDoneStatus.ERROR
        assert result.error is not None
        root_cause, _, remark = result.error.partition(" (recording this step was cut off ")
        assert root_cause.startswith("observe failed at step 1: ObservationFailedError")
        assert "ConnectionError: CDP websocket closed" in root_cause
        assert remark == "by the run's timeout of 0.5s)"
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.step(1) is None
        assert trace.result is not None and trace.result.error == result.error

    async def test_such_a_veto_stops_the_sweep_when_the_timeout_cuts_its_step_off(
        self, tmp_path
    ) -> None:
        caps: list[FakeArchivingCapability] = []

        def compose() -> Any:
            cap = FakeArchivingCapability()
            caps.append(cap)
            hooks: list[Any] = [_archiving_writer(tmp_path)]
            if len(caps) == 1:  # the first task lands on the risk-control page
                hooks.append(_RiskGuard(cap, veto_at=1, breaks=_stalls_next_collection(cap)))
            return make_archive_run(hooks=hooks, capability=cap, steps=3)

        summary = await BenchmarkRunner(loader=_SweepLoader(), compose=compose, timeout=0.5).run()

        assert summary.was_aborted
        assert summary.aborted is not None and "risk-control page detected" in summary.aborted
        assert [outcome.agent_done for outcome in summary.outcomes] == ["vetoed"]
        assert len(caps) == 1  # the next task was never composed

    @pytest.mark.parametrize("refuses", ["commits", "every write"])
    @pytest.mark.parametrize("archiving", [True, False], ids=["archiving", "plain"])
    async def test_a_veto_survives_a_disk_that_stays_full_through_the_close_out(
        self, tmp_path, monkeypatch, caplog, archiving, refuses
    ) -> None:
        """The close-out cannot write either, and the veto is still what ``execute`` raises.

        The disk fills at the vetoed step and stays full: the step's commit
        fails (the Loop notes that on the veto), and so does the close-out,
        which must commit the step's calls — archiving, also its owed batch
        and the tail — as trailing. That second failure once replaced the
        veto: ``execute(raise_on_error=False)`` raised the ``OSError`` and a
        sweep went on to its next task (``docs/architecture_design.md``
        §4.2, "否决总是上抛"). The close-out now raises nothing at all: what
        it could not write is in the trace's incomplete reason, as far as the
        disk lets the manifest say so.
        """
        disk = _FillingDisk(monkeypatch, refuses)
        cap = FakeArchivingCapability()
        writer = (
            _archiving_writer(tmp_path)
            if archiving
            else TraceWriter(trace_dir=str(tmp_path / "traces"))
        )
        guard = _RiskGuard(cap, veto_at=1, breaks=disk.fill)
        run = make_archive_run(hooks=[writer, guard], capability=cap, steps=3)
        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, raise_on_error=False
            )

        notes = caught.value.__notes__
        assert [note.partition(": ")[0] for note in notes] == [
            "Recording the vetoed step failed too",
        ]
        assert all("No space left on device" in note for note in notes)
        assert "No space left on device" in caplog.text
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.last_step_completed == 0
        assert trace.step(1) is None
        assert trace.trailing_calls == []  # dropped: nothing could take them
        if refuses == "commits":
            assert trace.manifest.status == TraceStatus.INCOMPLETE
            assert "No space left on device" in (trace.manifest.incomplete_reason or "")
            assert trace.result is not None and "risk-control page detected" in (
                trace.result.error or ""
            )
        else:
            # Not even the terminal state could be written.
            assert trace.manifest.status == TraceStatus.WRITING
            assert trace.result is None
        # Step 0, the vetoed step 1 (handed over, then owed), the close-out.
        assert cap.collect_calls == (3 if archiving else 0)

    @pytest.mark.parametrize("archiving", [True, False], ids=["archiving", "plain"])
    async def test_such_a_veto_stops_the_sweep_while_the_disk_stays_full(
        self, tmp_path, monkeypatch, archiving
    ) -> None:
        """No next task: not while the disk is full, not once it frees up again."""
        disk = _FillingDisk(monkeypatch, "commits")
        caps: list[FakeArchivingCapability] = []

        def compose() -> Any:
            cap = FakeArchivingCapability()
            caps.append(cap)
            writer = (
                _archiving_writer(tmp_path)
                if archiving
                else TraceWriter(trace_dir=str(tmp_path / "traces"))
            )
            hooks: list[Any] = [writer]
            if len(caps) == 1:  # the first task lands on the risk-control page
                hooks.append(_RiskGuard(cap, veto_at=0, breaks=disk.fill))
            return make_archive_run(hooks=hooks, capability=cap, steps=2)

        summary = await BenchmarkRunner(loader=_SweepLoader(), compose=compose, timeout=10).run()

        assert summary.was_aborted
        assert summary.aborted is not None and "risk-control page detected" in summary.aborted
        assert [outcome.agent_done for outcome in summary.outcomes] == ["vetoed"]
        assert len(caps) == 1  # the next task was never composed, let alone set up

    async def test_an_empty_archive_costs_nothing(self, tmp_path):
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        env = SurfacelessEnv(make_archive_env())
        result = await _runtime(writer, env=env, steps=2).execute("t", timeout=10)

        assert result.agent_done == AgentDoneStatus.DONE
        assert writer.run_dir is not None
        assert not (writer.run_dir / "env").exists()
        trace = RunTrace(writer.run_dir)
        assert trace.step_count == 2
        assert trace.config.env_evidence_privacy is None
        assert trace.config.negotiation is not None
        assert trace.config.negotiation.active_archivers == []

    async def test_an_archiving_capability_is_not_touched_by_a_run_that_does_not_archive(
        self, tmp_path
    ) -> None:
        cap = FakeArchivingCapability()
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        await _runtime(writer, cap=cap, steps=2).execute("t", timeout=10)
        assert (cap.bind_calls, cap.collect_calls) == (0, 0)
        assert writer.run_dir is not None
        assert not (writer.run_dir / "env").exists()

    async def test_the_pull_goes_to_the_raw_environment_no_hook_sees_it(self, tmp_path):
        """No hook intercepts the pull, so nothing recurses and nothing can rewrite evidence."""
        cap = FakeArchivingCapability()
        spy = _PullSpy()
        run = make_archive_run(hooks=[_archiving_writer(tmp_path), spy], capability=cap, steps=2)
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute("t", timeout=10)

        assert result.agent_done == AgentDoneStatus.DONE
        assert cap.collect_calls == 3  # two steps and the close-out
        assert spy.seen == 0
        # Control: the same hook does see a pull made through a woven environment.
        woven = Weaver([spy]).wrap(make_archive_env(FakeArchivingCapability()), role="env")
        await woven.collect_archive()
        assert spy.seen == 1


# ── the fixtures ──────────────────────────────────────────────────────


class TestTheFixtureEncodesWhatTheWriterWrites:
    """``trace_builder.archive_file`` is what reader tests build traces from.

    It must produce the very index records ``TraceWriter`` commits — path,
    bytes, metadata — for a step file, a blob, a run record and trailing
    evidence alike.
    """

    async def test_step_blob_run_and_trailing_records(self, tmp_path) -> None:
        cycle = archive_item(STEPS_KEY, "cycle.json", b'{"c": 1}', cycle=4, ids={"frame": "f"})
        body = archive_item(
            BLOBS_KEY,
            "response.bin",
            b"body",
            media_type="application/octet-stream",
            content_addressed=True,
        )
        record = archive_item(CAPABILITY_ID, "fingerprint.json", b"{}", scope="run")
        late = archive_item(STEPS_KEY, "late.json", b'{"late": 1}')
        env = ScriptedEnv(ArchiveBatch((cycle, body, record)), ArchiveBatch((late,)))
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await writer.on_run_end(result=_done())

        trace = RunTrace(run_dir)
        expected = [
            archive_file(STEPS_KEY, "cycle.json", b'{"c": 1}', step=0, cycle=4, ids={"frame": "f"}),
            archive_file(
                BLOBS_KEY,
                "response.bin",
                b"body",
                step=0,
                media_type="application/octet-stream",
                content_addressed=True,
            ),
            archive_file(CAPABILITY_ID, "fingerprint.json", b"{}", scope="run", run_version=0),
            archive_file(STEPS_KEY, "late.json", b'{"late": 1}', trailing=0),
        ]
        committed = trace.extra_files(0) + trace.trailing_extra_files
        assert [(r.path, r.size, r.sha256, r.content_addressed, r.meta) for r in committed] == [
            (f.rel_path, len(f.data), sha256_hex(f.data), f.content_addressed, dict(f.meta))
            for f in expected
        ]
