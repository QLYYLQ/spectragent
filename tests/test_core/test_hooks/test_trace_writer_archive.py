"""TraceWriter seals archive batches and persists them independently of execution.

``docs/architecture_design.md`` §4.6 ("记录器从环境拉取证据") and §5.2–§5.4. Two
drivers, as in ``test_trace_writer.py``:

- **direct** — the hook's methods called the way the runtime calls them. The
  environment is either the fake archiving capability in its environment,
  negotiated and bound exactly as ``AgentRuntime.prepare`` would
  (:class:`Bench`), or a scripted raw environment whose ``collect_archive``
  hands over prepared batches (:class:`ScriptedEnv`) — for contract breaches,
  failures and cancellations;
- **a real** ``AgentRuntime`` over ``make_archive_run`` — for what only the
  runtime exercises: execution surviving recording failures, cancellation and
  veto precedence, and runs that select no archive.

Everything is read back through ``RunTrace``'s archive accessors.
"""

import asyncio
import errno
import json
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.weaver import Weaver
from spectragent.core.archive import ArchiveBatch, ArchiveFile
from spectragent.core.benchmark_runner import BenchmarkRunner
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops.errors import RunAbort
from spectragent.core.negotiation import ArchiveNegotiationResult, negotiate_archive
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import ARCHIVE_META_KEY, sha256_hex
from spectragent.core.trace.models import TraceManifest, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.recording import RecordingHealth
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
    as_chat,
    persisted,
    required,
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


@pytest.fixture(autouse=True)
async def close_started_writers(monkeypatch):
    """Every test gives the recording worker its public lifecycle close-out."""
    started: list[TraceWriter] = []
    original = TraceWriter.on_run_start

    async def start(writer: TraceWriter, **kwargs: Any) -> None:
        await original(writer, **kwargs)
        started.append(writer)

    monkeypatch.setattr(TraceWriter, "on_run_start", start)
    yield
    for writer in started:
        await writer.on_run_end(result=_done())


async def _call(writer: TraceWriter, text: str = "go") -> None:
    """One LLM call through the advice, the way the AOP proxy issues it."""

    async def proceed() -> Any:
        return ChatInvokeCompletion(completion="ok", usage=usage(), stop_reason="end_turn")

    await writer.around_ainvoke(proceed=proceed, args=([UserMessage(content=text)],), kwargs={})


async def _commit(writer: TraceWriter, step: int) -> None:
    """Hand off a step, then explicitly wait before inspecting durable evidence."""
    await writer.after_record_step(None, args=(step_record(step),), kwargs={})
    await persisted(writer)


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
    """Read each committed run record through the public extra-file indexes."""
    extras = [
        record for entry in trace.manifest.steps for record in trace.extra_files(entry.step)
    ] + trace.trailing_extra_files
    found = []
    for record in extras:
        archive = record.meta.get(ARCHIVE_META_KEY)
        if archive is None or archive["scope"] != "run":
            continue
        if archive["archiver_id"].partition(".")[0] == capability_id:
            found.append(
                (
                    record.path,
                    archive["run_version"],
                    json.loads(trace.read_extra(record.path))["generation"],
                )
            )
    return sorted(found, key=lambda row: row[1] if row[1] is not None else -1)


def _archive_extras(trace: RunTrace) -> list[Any]:
    """Trailing archive evidence, excluding the independent recording timing facts."""
    return [record for record in trace.trailing_extra_files if ARCHIVE_META_KEY in record.meta]


def _problem(health: RecordingHealth | None, *, step: int | None, error: str, detail: str) -> Any:
    """Find the public diagnostic for a deliberately injected recording fault."""
    return next(
        problem
        for problem in required(health).problems
        if problem.step == step and problem.error == error and detail in problem.detail
    )


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
        await bench.step(0)
        assert bench.spy.calls == 1
        assert bench.cap.collect_calls == 1
        await bench.writer.on_run_end(result=_done())

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
    async def test_an_unbound_archive_is_reported_without_starting_a_trace(
        self, tmp_path, negotiation, env, why
    ) -> None:
        writer = TraceWriter(
            trace_dir=str(tmp_path / "traces"), archive=[FakeStepArchiver(), FakeBlobArchiver()]
        )
        await writer.on_run_start(env=env, task="t", config={}, archive_negotiation=negotiation)
        problem = _problem(await persisted(writer), step=None, error="RuntimeError", detail=why)
        assert problem.phase == "initialize"
        assert "AgentRuntime.prepare" in problem.detail
        assert repr(sorted([STEPS_KEY, BLOBS_KEY])) in problem.detail
        assert not (tmp_path / "traces").exists()
        result = _done()
        await writer.on_run_end(result=result)
        assert result.agent_done == AgentDoneStatus.DONE
        assert result.error is None
        assert required(result.metrics.recording_health).status == "incomplete"


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
        await bench.cap.produce(2)
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert trace.manifest.last_step_completed == 1
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.trailing_calls == []
        rows = [(step, r) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)]
        assert [step for step, _ in rows] == [0, 1, None]
        tail = rows[-1][1]
        assert tail.path.startswith("env/trailing/")
        assert tail.name == "cycle_003.json"
        assert (run_dir / tail.path).is_file()
        assert [step for step, _ in trace.iter_archive(STEPS_KEY)] == [0, 1]

    async def test_unfinished_calls_and_the_tail_are_separate_trailing_commits(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await _call(bench.writer, "step 1, never surrendered")
        await bench.writer.on_run_end(result=_failed())

        trace = bench.trace()
        (calls_entry,) = [entry for entry in trace.manifest.trailing if entry.llm_calls]
        assert calls_entry.llm_calls == ["messages/001_call_00.json"]
        assert calls_entry.extra_index is None
        assert _archive_extras(trace)
        assert all(not entry.llm_calls for entry in trace.manifest.trailing if entry.extra_index)
        assert [r.name for _, r in trace.iter_archive(STEPS_KEY, include_trailing=True)][
            -1
        ] == "cycle_002.json"
        assert as_chat(trace.trailing_calls[0]).messages[0].text == "step 1, never surrendered"

    async def test_the_close_out_collection_happens_at_most_once(self, tmp_path):
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        await bench.writer.on_run_end(result=_done())
        await bench.cap.produce(2)
        await bench.writer.on_run_end(result=_done())

        assert [
            record.meta[ARCHIVE_META_KEY]["name"]
            for record in _archive_extras(bench.trace())
            if record.meta[ARCHIVE_META_KEY]["archiver_id"] == STEPS_KEY
        ] == ["cycle_002.json"]
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
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert as_chat(trace.trailing_calls[0]).messages[0].text == "lonely"
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
        result = _done()
        await bench.writer.on_run_end(result=result)

        trace = bench.trace()
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        reason = trace.manifest.incomplete_reason or ""
        assert f"{CAPABILITY_ID}: RuntimeError:" in reason
        assert "broken on purpose" in reason
        problem = _problem(
            result.metrics.recording_health,
            step=None,
            error="RuntimeError",
            detail="broken on purpose",
        )
        assert (problem.channel, problem.phase) == (CAPABILITY_ID, "closeout")
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE
        assert as_chat(trace.trailing_calls[0]).messages[0].text == "the last word"
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
        assert "Recording closeout failed" in caplog.text

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
        assert as_chat(trace.trailing_calls[0]).messages[0].text == "in flight at the cancellation"
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
        wal = writer.wal
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
        assert "closeout cancelled" in reason
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
        await bench.writer.after_record_step(None, args=(step_record(1),), kwargs={})
        assert bench.spy.calls == 2
        assert [entry.step for entry in bench.trace().manifest.steps] == [0]


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
        await _commit(bench.writer, 1)
        assert (await persisted(bench.writer)).missing_steps == [1]
        await bench.writer.on_run_end(result=_failed())
        await bench.writer.on_run_error(error=OSError("disk gone"), partial_steps=[])
        assert bench.trace().manifest.status == TraceStatus.WRITING
        monkeypatch.setattr(fsio, "write_durable", real)

        second = await _second_run(bench)
        assert [
            (step, r.name) for step, r in second.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_003.json")]
        assert second.manifest.recording_problems == []

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


# ── recording faults and archive integrity ────────────────────────────


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


class TestArchiveFaultsPreserveExecution:
    @pytest.mark.parametrize("breach", list(BREACHES))
    async def test_an_invalid_batch_loses_its_evidence_but_not_its_step(
        self, tmp_path, breach
    ) -> None:
        batch, why = BREACHES[breach]
        env = ScriptedEnv(batch)
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _call(writer, "the invalid batch's step call")
        await _commit(writer, 0)

        trace = RunTrace(run_dir)
        assert trace.step(0) is not None
        assert not (run_dir / "env").exists()
        problem = _problem(
            await persisted(writer), step=0, error="ArchiveContractError", detail=why
        )
        assert (problem.channel, problem.phase) == ("archive", "collect")
        assert not problem.recovered
        await _call(writer, "the later step's call")
        await _commit(writer, 1)
        result = _done()
        await writer.on_run_end(result=result)

        trace = RunTrace(run_dir)
        assert env.calls == 3
        assert result.agent_done == AgentDoneStatus.DONE and result.error is None
        assert trace.step(0) is not None and trace.step(1) is not None
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(0)] == [
            "the invalid batch's step call"
        ]
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == [
            "the later step's call"
        ]
        assert trace.trailing_calls == []
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.missing_steps == []
        assert trace.manifest.complete_through == -1
        assert why in (trace.manifest.incomplete_reason or "")
        assert not (run_dir / "env").exists()

    @pytest.mark.parametrize("breach", list(BREACHES))
    @pytest.mark.parametrize("succeeded", [True, False], ids=["succeeded", "failed"])
    async def test_an_invalid_tail_writes_no_archive_and_preserves_the_outcome(
        self, tmp_path, caplog, breach, succeeded
    ) -> None:
        batch, why = BREACHES[breach]
        env = ScriptedEnv(ArchiveBatch(), batch)
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        result = _done() if succeeded else _failed()
        outcome = (result.agent_done, result.error)
        await writer.on_run_end(result=result)
        assert (result.agent_done, result.error) == outcome
        assert why in caplog.text
        problem = _problem(
            result.metrics.recording_health, step=None, error="ArchiveContractError", detail=why
        )
        assert problem.channel == "archive" and problem.phase == "closeout"

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert why in (trace.manifest.incomplete_reason or "")
        assert trace.manifest.complete_through == 0
        assert _archive_extras(trace) == []
        assert not (run_dir / "env").exists()
        await writer.on_run_error(error=RuntimeError("late"), partial_steps=[])
        assert env.calls == 2
        assert _archive_extras(RunTrace(run_dir)) == []

    async def test_a_collection_failure_does_not_disable_later_collections(self, tmp_path):
        env = ScriptedEnv(ArchiveBatch(), RuntimeError("socket gone"))
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await _call(writer, "step 1")
        await _commit(writer, 1)
        await _call(writer, "step 2")
        await _commit(writer, 2)
        result = _done()
        await writer.on_run_end(result=result)

        trace = RunTrace(run_dir)
        assert env.calls == 4
        assert [entry.step for entry in trace.manifest.steps] == [0, 1, 2]
        assert trace.step_count == 3
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == ["step 1"]
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(2)] == ["step 2"]
        assert trace.trailing_calls == []
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.missing_steps == []
        assert trace.manifest.complete_through == 0
        assert "archive: RuntimeError: socket gone" in (trace.manifest.incomplete_reason or "")
        problem = _problem(
            result.metrics.recording_health, step=1, error="RuntimeError", detail="socket gone"
        )
        assert (problem.channel, problem.phase) == ("archive", "collect")

    async def test_one_archivers_gap_keeps_the_step_and_the_other_archivers_evidence(
        self, tmp_path
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0, call="whole step")
        bench.cap.failing.add(BLOBS_KEY)
        await bench.step(1, call="step without its bodies")
        bench.cap.failing.clear()
        await bench.step(2)
        result = _done()
        await bench.writer.on_run_end(result=result)

        trace = bench.trace()
        assert [entry.step for entry in trace.manifest.steps] == [0, 1, 2]
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == [
            "step without its bodies"
        ]
        assert [r.name for r in trace.archive_files(1, STEPS_KEY)] == ["cycle_002.json"]
        assert trace.archive_files(1, BLOBS_KEY) == []
        assert [len(trace.archive_files(step, BLOBS_KEY)) for step in (0, 2)] == [1, 1]
        (problem,) = required(result.metrics.recording_health).problems
        assert (problem.step, problem.channel, problem.phase) == (1, BLOBS_KEY, "collect")
        assert problem.detail == f"{BLOBS_KEY}: evidence lost on purpose"
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.missing_steps == []
        assert trace.manifest.complete_through == 0
        assert trace.manifest.incomplete_reason == (
            f"{BLOBS_KEY}: RuntimeError: {BLOBS_KEY}: evidence lost on purpose"
        )

    async def test_a_gap_at_close_out_keeps_the_tails_other_evidence(self, tmp_path) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        bench.cap.failing.add(BLOBS_KEY)
        result = _done()
        await bench.writer.on_run_end(result=result)

        trace = bench.trace()
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (None, "cycle_002.json")]
        (problem,) = required(result.metrics.recording_health).problems
        assert (problem.step, problem.channel, problem.phase) == (None, BLOBS_KEY, "closeout")
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE

    async def test_a_cancelled_collection_propagates_and_still_queues_its_step(self, tmp_path):
        """The step was sealed before the pull; the cancellation costs its evidence, not it."""
        tail = archive_item(STEPS_KEY, "tail.json", b"{}")
        env = ScriptedEnv(ArchiveBatch(), asyncio.CancelledError(), ArchiveBatch((tail,)))
        writer, run_dir = await _scripted_start(tmp_path, env)
        await _commit(writer, 0)
        await _call(writer, "cancelled collection's original call")
        with pytest.raises(asyncio.CancelledError):
            await writer.after_record_step(None, args=(step_record(1),), kwargs={})
        await writer.on_run_end(result=_failed("Execution cancelled"))

        trace = RunTrace(run_dir)
        assert env.calls == 3
        assert trace.step(1) is not None
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == [
            "cancelled collection's original call"
        ]
        assert trace.trailing_calls == []
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.missing_steps == []
        assert "collection cancelled" in (trace.manifest.incomplete_reason or "")
        assert trace.manifest.complete_through == 0
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(None, "tail.json")]

    async def test_a_failed_commit_retries_the_original_step_and_archive_paths(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        bench.cap.change_run_record()
        await bench.cap.produce(1)
        await _call(bench.writer, "step 1")
        _lose_power_once(monkeypatch)
        await _commit(bench.writer, 1)
        result = _done()
        await bench.writer.on_run_end(result=result)

        trace = bench.trace()
        assert trace.step(1) is not None
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == ["step 1"]
        assert trace.trailing_calls == []
        assert [
            (step, r.path) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [
            (0, "env/steps/000/contract_archive.steps/cycle_001.json"),
            (1, "env/steps/001/contract_archive.steps/cycle_002.json"),
        ]
        assert _latest_run_record(trace, CAPABILITY_ID) == ("env/run.contract_archive.1.json", 1)
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.manifest.missing_steps == []
        assert _problem(
            result.metrics.recording_health, step=1, error="OSError", detail="power lost"
        ).recovered
        assert trace.manifest.recording_problems

    async def test_a_permanent_trailing_failure_exhausts_only_its_bounded_retries(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        await bench.cap.produce(1)
        wal = bench.writer.wal
        assert wal is not None
        real = wal.commit_trailing
        attempts = []

        def failing(**kwargs: Any) -> None:
            attempts.append(kwargs)
            raise OSError("disk hiccup")

        monkeypatch.setattr(wal, "commit_trailing", failing)
        await bench.writer.on_run_end(result=_done())
        assert len(attempts) == bench.writer.write_retries + 1
        trace = bench.trace()
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert "OSError: disk hiccup" in (trace.manifest.incomplete_reason or "")
        monkeypatch.setattr(wal, "commit_trailing", real)
        await bench.writer.on_run_error(error=OSError("disk hiccup"), partial_steps=[])
        assert [
            r.name for _, r in bench.trace().iter_archive(STEPS_KEY, include_trailing=True)
        ] == ["cycle_001.json"]
        assert bench.spy.calls == 2

    async def test_an_unwritable_segment_does_not_take_later_steps_or_tail_down_with_it(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        await bench.step(0)
        wal = bench.writer.wal
        assert wal is not None
        real = wal.commit_step
        attempts = []

        def refuses_step_one(step: int, *args: Any, **kwargs: Any) -> None:
            if step == 1:
                attempts.append(step)
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            real(step, *args, **kwargs)

        monkeypatch.setattr(wal, "commit_step", refuses_step_one)
        await bench.step(1, call="failed segment")
        await bench.step(2, call="later segment")
        await bench.cap.produce(3)
        await _call(bench.writer, "genuinely unfinished step")
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert len(attempts) == bench.writer.write_retries + 1
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        assert trace.manifest.missing_steps == [1]
        assert "File name too long" in (trace.manifest.incomplete_reason or "")
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(2)] == [
            "later segment"
        ]
        assert [as_chat(call).messages[0].text for call in trace.trailing_calls] == [
            "genuinely unfinished step"
        ]
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (2, "cycle_003.json"), (None, "cycle_004.json")]
        assert trace.archive_files(1, STEPS_KEY) == []
        assert not (run_dir / "messages/001_call_00.json").exists()
        before = len(trace.manifest.trailing)
        await bench.writer.on_run_error(error=OSError("late"), partial_steps=[])
        assert len(bench.trace().manifest.trailing) == before

    async def test_orphan_archive_files_are_invisible_until_a_later_committed_reference(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        run_dir = await bench.start()
        await bench.step(0)
        real = fsio.write_durable
        attempts = []

        def refuse_step_one_manifest(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json" and any(
                entry["step"] == 1 for entry in json.loads(data)["steps"]
            ):
                attempts.append(path)
                raise OSError("power lost before step 1 committed")
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", refuse_step_one_manifest)
        await bench.step(1, call="uncommitted call")
        orphan = "env/steps/001/contract_archive.steps/cycle_002.json"
        assert (run_dir / orphan).is_file()
        body = next(item for item in bench.spy.batches[1].files if item.archiver_id == BLOBS_KEY)
        digest = sha256_hex(body.data)
        trace = bench.trace()
        assert trace.step(1) is None
        assert trace.llm_messages(1) == []
        with pytest.raises(KeyError):
            trace.read_extra(orphan)
        with pytest.raises(KeyError):
            trace.blob(digest)

        await bench.step(2, call="step 2")
        await bench.step(3, call="step 3")  # captures the failed segment's body again
        await bench.writer.on_run_end(result=_done())
        trace = bench.trace()
        assert len(attempts) == bench.writer.write_retries + 1
        assert [entry.step for entry in trace.manifest.steps] == [0, 2, 3]
        assert trace.manifest.missing_steps == [1]
        assert trace.manifest.complete_through == 0
        assert trace.step_count == 3
        assert trace.trailing_calls == []
        assert trace.blob(digest) == body.data
        assert trace.archive_files(3, BLOBS_KEY)[0].sha256 == digest
        with pytest.raises(KeyError):
            trace.read_extra(orphan)

    async def test_a_trailing_run_record_follows_the_recovered_steps_version(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        bench.cap.change_run_record()
        await bench.cap.produce(1)
        _lose_power_once(monkeypatch)
        await _commit(bench.writer, 1)
        bench.cap.change_run_record()
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert _run_records(trace, CAPABILITY_ID) == [
            ("env/run.contract_archive.json", 0, 0),
            ("env/run.contract_archive.1.json", 1, 1),
            ("env/run.contract_archive.2.json", 2, 2),
        ]
        assert _latest_run_record(trace, CAPABILITY_ID) == ("env/run.contract_archive.2.json", 2)
        assert trace.manifest.status == TraceStatus.COMPLETE

    @pytest.mark.parametrize("latest_generation", [0, 2], ids=["unchanged-latest", "new-latest"])
    async def test_a_retried_older_run_record_cannot_replace_a_later_capture(
        self, tmp_path, monkeypatch, caplog, latest_generation
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        bench.cap.change_run_record()
        await bench.cap.produce(1)
        wal = bench.writer.wal
        assert wal is not None
        real = wal.commit_step
        first_failed = threading.Event()
        release_first = threading.Event()
        order = []

        def fail_once(step: int, *args: Any, **kwargs: Any) -> None:
            if step == 1 and not first_failed.is_set():
                first_failed.set()
                if not release_first.wait(2):
                    raise RuntimeError("test did not release the first commit")
                raise OSError("first attempt failed")
            real(step, *args, **kwargs)
            order.append(step)

        monkeypatch.setattr(wal, "commit_step", fail_once)
        try:
            await bench.writer.after_record_step(None, args=(step_record(1),), kwargs={})
            assert await asyncio.to_thread(first_failed.wait, 2)
            bench.cap.fake.run_generation = latest_generation
            bench.cap.fake.run_record_due = True
            await bench.cap.produce(2)
            await bench.writer.after_record_step(None, args=(step_record(2),), kwargs={})
        finally:
            release_first.set()
        await persisted(bench.writer)
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert order == [2, 1]
        expected = [("env/run.contract_archive.json", 0, 0)]
        if latest_generation == 2:
            expected.append(("env/run.contract_archive.1.json", 1, 2))
        assert _run_records(trace, CAPABILITY_ID) == expected
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (1, "cycle_002.json"), (2, "cycle_003.json")]
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.manifest.complete_through == 2
        assert "collection 3's run record of capability 'contract_archive' is newer" in caplog.text

    async def test_each_retried_batch_keeps_its_own_step_call_and_archive_identity(
        self, tmp_path, monkeypatch
    ) -> None:
        bench = Bench(tmp_path)
        await bench.start()
        await bench.step(0)
        wal = bench.writer.wal
        assert wal is not None
        real = wal.commit_step
        failed = set()

        def fail_once_each(step: int, *args: Any, **kwargs: Any) -> None:
            if step in {1, 2} and step not in failed:
                failed.add(step)
                raise OSError("power lost before the commit point")
            real(step, *args, **kwargs)

        monkeypatch.setattr(wal, "commit_step", fail_once_each)
        for step in (1, 2):
            await bench.cap.produce(step)
            await _call(bench.writer, f"step {step}")
            await bench.writer.after_record_step(None, args=(step_record(step),), kwargs={})
        await bench.writer.on_run_end(result=_done())

        trace = bench.trace()
        assert failed == {1, 2}
        assert [
            (step, r.path) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [
            (0, "env/steps/000/contract_archive.steps/cycle_001.json"),
            (1, "env/steps/001/contract_archive.steps/cycle_002.json"),
            (2, "env/steps/002/contract_archive.steps/cycle_003.json"),
        ]
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(1)] == ["step 1"]
        assert [as_chat(call).messages[0].text for call in trace.llm_messages(2)] == ["step 2"]
        assert trace.trailing_calls == []
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.manifest.missing_steps == []


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
        self.result: AgentResult | None = None

    @property
    def blocking(self) -> bool:
        return True  # a guard that vetoes must be blocking, or its veto is logged and dropped

    @property
    def health(self) -> RecordingHealth | None:
        """The recording health on the result this run closed out with (the veto returns none)."""
        return self.result.metrics.recording_health if self.result is not None else None

    async def on_run_end(self, *, result: Any = None, **kwargs: Any) -> None:
        self.result = result

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
        assert _archive_extras(trace) == []
        assert trace.trailing_calls == []
        assert trace.config.env_evidence_privacy is not None

    @pytest.mark.parametrize("failing_step", [0, 1])
    async def test_a_failed_collection_preserves_all_remaining_execution_steps(
        self, tmp_path, failing_step
    ):
        cap = FakeArchivingCapability()
        if failing_step == 0:
            cap.broken = True
        else:
            _break_after(cap, failing_step)
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute(
            "t", timeout=10, raise_on_error=True
        )

        assert (result.agent_done, result.answer, result.error) == (
            AgentDoneStatus.DONE,
            "42",
            None,
        )
        assert result.metrics.steps == 3
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == failing_step - 1
        assert trace.manifest.missing_steps == []
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE
        assert trace.result.error is None
        assert [entry.step for entry in trace.manifest.steps] == [0, 1, 2]
        for step in range(failing_step, 3):
            assert trace.archive_files(step, STEPS_KEY) == []
            assert len(trace.llm_messages(step)) == 1, "the step's model call is kept"
        assert trace.trailing_calls == []
        assert cap.collect_calls == 4
        assert result.metrics.recording_health is not None
        assert result.metrics.recording_health.missing_steps == []
        problem = _problem(
            result.metrics.recording_health,
            step=failing_step,
            error="RuntimeError",
            detail="broken on purpose",
        )
        assert (problem.channel, problem.phase) == (CAPABILITY_ID, "collect")

    async def test_recording_health_is_separate_from_the_successful_execution_result(
        self, tmp_path
    ) -> None:
        cap = FakeArchivingCapability()
        cap.broken = True
        writer = _archiving_writer(tmp_path)
        result = await _runtime(writer, cap=cap, steps=3).execute(
            "t", timeout=10, raise_on_error=False
        )

        assert (result.agent_done, result.answer, result.error) == (
            AgentDoneStatus.DONE,
            "42",
            None,
        )
        assert result.metrics.llm_calls == 3
        assert result.metrics.recording_health is not None
        assert result.metrics.recording_health.status == "incomplete"
        assert result.metrics.recording_health.missing_steps == []
        assert result.metrics.recording_health.committed_steps == 3
        assert result.metrics.recording_health.complete_through == -1
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
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert any(record.actions for record in trace.step_records)
        assert trace.result is not None and trace.result.answer == "42"
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == -1
        assert "broken on purpose" in (trace.manifest.incomplete_reason or "")
        assert trace.manifest.missing_steps == []
        assert trace.step(0) is not None
        assert trace.archive_files(0, STEPS_KEY) == []
        assert trace.trailing_calls == []
        assert cap.collect_calls == 2

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
        assert "broken on purpose" in (trace.manifest.incomplete_reason or "")
        assert trace.manifest.missing_steps == []
        terminal = trace.step(1)
        assert terminal is not None and terminal.origin == "terminal"
        assert trace.archive_files(1, STEPS_KEY) == []

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
        assert "broken on purpose" in (trace.manifest.incomplete_reason or "")
        assert trace.result is not None and trace.result.agent_done == AgentDoneStatus.DONE
        assert trace.step(0) is not None
        assert [r.name for _, r in trace.iter_archive(STEPS_KEY)] == ["cycle_001.json"]

    async def test_a_veto_at_a_step_whose_collection_fails_is_still_a_veto(self, tmp_path):
        cap = FakeArchivingCapability()
        writer = _archiving_writer(tmp_path)
        guard = _RiskGuard(cap, veto_at=1)
        run = make_archive_run(hooks=[writer, guard], capability=cap, steps=3)
        with pytest.raises(RunAbort, match="risk-control page detected") as caught:
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, raise_on_error=False
            )

        assert "broken on purpose" not in str(caught.value)
        _problem(guard.health, step=1, error="RuntimeError", detail="broken on purpose")
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == 0
        assert trace.manifest.missing_steps == []
        assert trace.step(1) is not None, "the vetoed step is recorded without its evidence"
        assert trace.archive_files(1, STEPS_KEY) == []
        assert trace.trailing_calls == []
        assert trace.result is not None and "risk-control page detected" in (
            trace.result.error or ""
        )
        assert "broken on purpose" not in (trace.result.error or "")
        assert trace.result.agent_done == AgentDoneStatus.VETOED
        assert cap.collect_calls == 3

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
        assert result.error.startswith("observe failed at step 1: ObservationFailedError")
        assert "ConnectionError: CDP websocket closed" in result.error
        assert "broken on purpose" not in result.error
        _problem(
            result.metrics.recording_health,
            step=1,
            error="RuntimeError",
            detail="broken on purpose",
        )
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        failed = trace.step(1)
        assert failed is not None and failed.error_phase == "observe"
        assert trace.archive_files(1, STEPS_KEY) == []
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
        # Sealed before its collection was cut off: recorded without its evidence.
        assert trace.step(1) is not None
        assert trace.manifest.complete_through == 0
        assert trace.trailing_calls == []
        # The stalled collection took nothing; the close-out took the vetoed page's evidence.
        assert [
            (step, r.name) for step, r in trace.iter_archive(STEPS_KEY, include_trailing=True)
        ] == [(0, "cycle_001.json"), (None, "cycle_002.json")]
        assert "collection cancelled" in (trace.manifest.incomplete_reason or "")
        assert trace.manifest.missing_steps == []
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
        assert trace.manifest.complete_through == 0
        assert trace.archive_files(1, STEPS_KEY) == []
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

        assert "No space left on device" not in str(caught.value)
        assert "No space left on device" in caplog.text
        health = required(guard.health)
        assert any("No space left on device" in problem.detail for problem in health.problems)
        assert health.status == "incomplete"
        assert 1 in health.missing_steps
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.step(1) is None
        assert trace.trailing_calls == []
        if refuses == "commits":
            assert trace.manifest.status == TraceStatus.INCOMPLETE
            assert "No space left on device" in (trace.manifest.incomplete_reason or "")
            assert trace.result is not None
            assert trace.result.agent_done == AgentDoneStatus.VETOED
            assert "risk-control page detected" in (trace.result.error or "")
            assert "No space left on device" not in (trace.result.error or "")
        else:
            assert trace.manifest.status == TraceStatus.WRITING
            assert trace.result is None
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
            archive_file(STEPS_KEY, "late.json", b'{"late": 1}', trailing=1),
        ]
        assert any(
            record.meta.get("kind") == "recording_timing" for record in trace.trailing_extra_files
        )
        committed = trace.extra_files(0) + _archive_extras(trace)
        assert [(r.path, r.size, r.sha256, r.content_addressed, r.meta) for r in committed] == [
            (f.rel_path, len(f.data), sha256_hex(f.data), f.content_addressed, dict(f.meta))
            for f in expected
        ]
