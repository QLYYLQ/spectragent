"""VerificationRunner — offline verifiers over a directory of traces, every verdict into its run's store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.verdict import JudgeUsage, Judgment, VerifierIdentity
from spectragent.verifiers.base import OfflineVerifier, Verifier, VerifierNeed
from spectragent.verifiers.exact_match import ExactMatchVerifier
from spectragent.verifiers.report import tallies, total_usage
from spectragent.verifiers.runner import VerificationRunner, discover_traces
from tests.test_verifiers.conftest import write_trace


class _Loader:
    """A minimal eval loader — the runner duck-types this surface."""

    benchmark_name = "offline_bench"
    subset = "All"

    def __init__(self, answers: dict[str, str]):
        self._refs = {
            task_id: EvalReference(
                task_id=task_id, benchmark="offline_bench", expected_answer=answer
            )
            for task_id, answer in answers.items()
        }

    def task_ids(self) -> list[str]:
        return list(self._refs)

    def get(self, task_id: str) -> EvalReference | None:
        return self._refs.get(task_id)

    def __getitem__(self, task_id: str) -> EvalReference:
        return self._refs[task_id]


@pytest.fixture
def three_traces(traces_dir: Path) -> list[Path]:
    return [
        write_trace(traces_dir, run_id="r1", task_id="t-1", answer="42"),
        write_trace(traces_dir, run_id="r2", task_id="t-2", answer="wrong"),
        write_trace(traces_dir, run_id="r3", task_id="t-3", answer="paris"),
    ]


@pytest.fixture
def loader() -> _Loader:
    return _Loader({"t-1": "42", "t-2": "43", "t-3": "Paris"})


def _runner(*verifiers: OfflineVerifier, loader: _Loader | None = None) -> VerificationRunner:
    return VerificationRunner(verifiers=list(verifiers), reference_loader=loader)  # type: ignore[arg-type]


# ── Discovery ───────────────────────────────────────────────────────


def test_discover_traces_finds_committed_runs(traces_dir, three_traces):
    assert discover_traces(traces_dir) == sorted(three_traces)


def test_discover_traces_skips_half_written_directories(traces_dir, three_traces):
    (traces_dir / "not-a-trace").mkdir()
    assert (traces_dir / "not-a-trace") not in discover_traces(traces_dir)


def test_discover_traces_on_a_missing_directory_is_empty(tmp_path):
    assert discover_traces(tmp_path / "nope") == []


# ── Judging a sweep ─────────────────────────────────────────────────


async def test_every_trace_gets_a_verdict(three_traces, loader):
    report = await _runner(ExactMatchVerifier(), loader=loader).run(three_traces)

    labels = {o.task_id: o.verdict.label for o in report.outcomes if o.verdict is not None}
    assert labels == {"t-1": 1, "t-2": 0, "t-3": 1}
    assert report.errors == []


async def test_each_verdict_lands_in_its_runs_store_with_who_judged_it(three_traces, loader):
    verifier = ExactMatchVerifier()

    await _runner(verifier, loader=loader).run(three_traces)

    verdicts = RunTrace(three_traces[0]).verdicts()
    slug = verifier.identity().slug
    assert list(verdicts) == [slug]
    assert (three_traces[0] / "verdicts" / f"{slug}.json").is_file()
    verdict = verdicts[slug]
    assert verdict.verifier == verifier.identity()
    assert verdict.attachment == "offline"
    assert verdict.status == "judged"
    assert verdict.label == 1


async def test_the_runner_times_the_verifier(three_traces, loader):
    report = await _runner(ExactMatchVerifier(), loader=loader).run(three_traces[:1])

    verdict = report.outcomes[0].verdict
    assert verdict is not None
    assert verdict.started_at > 1_000_000_000
    assert verdict.duration_ms >= 0


async def test_several_verifiers_judge_one_trace_side_by_side(three_traces, loader):
    """A rule and a second setting of it: two verdicts in one place, each with its own identity."""
    strict = ExactMatchVerifier(case_sensitive=True)
    lenient = ExactMatchVerifier(allow_substring=True)

    report = await _runner(strict, lenient, loader=loader).run(three_traces)

    assert [o.slug for o in report.outcomes[:2]] == [
        strict.identity().slug,
        lenient.identity().slug,
    ]
    verdicts = RunTrace(three_traces[2]).verdicts()
    assert set(verdicts) == {strict.identity().slug, lenient.identity().slug}
    # "paris" vs "Paris": fails under case sensitivity, passes leniently.
    assert verdicts[strict.identity().slug].label == 0
    assert verdicts[lenient.identity().slug].label == 1


async def test_judging_again_with_the_same_settings_replaces_the_verdict(three_traces, loader):
    verifier = ExactMatchVerifier()
    await _runner(verifier, loader=loader).run(three_traces[:1])
    first = RunTrace(three_traces[0]).verdicts()[verifier.identity().slug]

    await _runner(ExactMatchVerifier(), loader=loader).run(three_traces[:1])

    verdicts = RunTrace(three_traces[0]).verdicts()
    assert list(verdicts) == [verifier.identity().slug]
    assert verdicts[verifier.identity().slug].started_at >= first.started_at


# ── Verdicts are written atomically ─────────────────────────────────
#
# ``os.replace`` failing stands in for the disk filling up or the process
# dying between writing a verdict and publishing it.


def _disk_full(*_args, **_kwargs):
    raise OSError("DISK FULL")


async def test_a_verdict_that_cannot_be_published_leaves_no_partial_file(
    three_traces, loader, monkeypatch
):
    monkeypatch.setattr(os, "replace", _disk_full)

    report = await _runner(ExactMatchVerifier(), loader=loader).run(three_traces[:1])

    failure = report.errors[0].error
    assert failure is not None and "DISK FULL" in failure
    # Neither a partial verdict nor the temp file it was staged in.
    assert list((three_traces[0] / "verdicts").iterdir()) == []


async def test_a_failed_rejudging_keeps_the_previous_verdict_whole(
    three_traces, loader, monkeypatch
):
    verifier = ExactMatchVerifier()
    await _runner(verifier, loader=loader).run(three_traces[:1])
    verdict = three_traces[0] / "verdicts" / f"{verifier.identity().slug}.json"
    before = verdict.read_bytes()

    monkeypatch.setattr(os, "replace", _disk_full)
    report = await _runner(verifier, loader=loader).run(three_traces[:1])

    assert report.errors, "the second write was expected to fail"
    assert verdict.read_bytes() == before
    assert [p.name for p in (three_traces[0] / "verdicts").iterdir()] == [verdict.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_verdicts_are_as_readable_as_a_plain_write(tmp_path, three_traces, loader):
    """A run directory another user reads must not come out owner-only."""
    import stat

    verifier = ExactMatchVerifier()
    previous = os.umask(0o022)
    try:
        (tmp_path / "plain.json").write_text("{}")
        await _runner(verifier, loader=loader).run(three_traces[:1])
    finally:
        os.umask(previous)

    plain = stat.S_IMODE((tmp_path / "plain.json").stat().st_mode)
    written = three_traces[0] / "verdicts" / f"{verifier.identity().slug}.json"
    assert stat.S_IMODE(written.stat().st_mode) == plain


# ── What cannot be judged ───────────────────────────────────────────


async def test_one_bad_trace_does_not_destroy_the_pass(traces_dir, three_traces, loader):
    """A pass that dies on trace 2 of 300 has thrown away 298 verdicts."""
    orphan = write_trace(traces_dir, run_id="r4", task_id="not-in-the-split")

    report = await _runner(ExactMatchVerifier(), loader=loader).run([*three_traces, orphan])

    assert sum(o.verdict is not None for o in report.outcomes) == 3
    assert len(report.errors) == 1
    failure = report.errors[0].error
    assert failure is not None
    assert "not-in-the-split" in failure
    assert not (orphan / "verdicts").exists()


async def test_a_trace_that_does_not_read_back_fails_every_verifier(tmp_path, loader):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json")

    report = await _runner(
        ExactMatchVerifier(), ExactMatchVerifier(allow_substring=True), loader=loader
    ).run([broken])

    assert len(report.errors) == 2
    assert {o.task_id for o in report.errors} == {"broken"}


class _FlakyVerifier:
    """A judge unreachable for some tasks, written to the Protocol only.

    It lets the transport error escape — which is what the ported graders
    deliberately stopped swallowing.
    """

    verifier_id = "flaky"
    needs: frozenset[VerifierNeed] = frozenset({"trace", "reference"})

    def __init__(self, broken: set[str]):
        self._broken = broken
        self._inner = ExactMatchVerifier()

    def identity(self) -> VerifierIdentity:
        broken: list[JsonValue] = [*sorted(self._broken)]
        return VerifierIdentity(verifier_id=self.verifier_id, config_signature={"broken": broken})

    async def verify(self, trace: RunTrace, reference: EvalReference | None) -> Judgment:
        assert reference is not None
        if reference.task_id in self._broken:
            raise ConnectionError("429 Too Many Requests")
        return await self._inner.verify(trace, reference)


def test_a_structural_verifier_states_its_identity():
    verifier = _FlakyVerifier({"t-2", "t-1"})
    assert isinstance(verifier, Verifier)
    assert isinstance(verifier, OfflineVerifier)
    assert verifier.identity().slug == _FlakyVerifier({"t-1", "t-2"}).identity().slug


async def test_a_judge_outage_is_an_error_never_a_wrong_answer(traces_dir):
    """An API outage must not become a benchmark score, nor overwrite anything."""
    traces = [
        write_trace(traces_dir, run_id=f"r{i}", task_id=f"t-{i}", answer="42") for i in (1, 2, 3)
    ]
    loader = _Loader({"t-1": "42", "t-2": "42", "t-3": "42"})

    report = await _runner(_FlakyVerifier(broken={"t-2"}), loader=loader).run(traces)

    [outage] = report.errors
    assert outage.task_id == "t-2"
    assert outage.error is not None and "429" in outage.error
    assert RunTrace(traces[1]).verdicts() == {}
    [tally] = tallies(report)
    assert (tally.provided, tally.judged, tally.passed, tally.failed, tally.errors) == (
        3,
        2,
        2,
        0,
        1,
    )
    assert tally.success_rate == 1.0
    assert tally.coverage == pytest.approx(2 / 3)


class _NotAJudgment:
    verifier_id = "careless"
    needs: frozenset[VerifierNeed] = frozenset({"trace"})

    def identity(self) -> VerifierIdentity:
        return VerifierIdentity(verifier_id=self.verifier_id)

    async def verify(self, trace, reference):
        return {"label": 1}


async def test_a_verifier_that_hands_back_no_judgment_judged_nothing(three_traces):
    report = await _runner(_NotAJudgment()).run(three_traces[:1])  # type: ignore[arg-type]

    [failure] = report.errors
    assert failure.error is not None and "not a Judgment" in failure.error
    assert RunTrace(three_traces[0]).verdicts() == {}


class _WithoutReference:
    verifier_id = "unreferenced"
    needs: frozenset[VerifierNeed] = frozenset({"trace"})

    def __init__(self) -> None:
        self.references: list[EvalReference | None] = []

    def identity(self) -> VerifierIdentity:
        return VerifierIdentity(verifier_id=self.verifier_id)

    async def verify(self, trace, reference):
        self.references.append(reference)
        return Judgment(label=1)


async def test_a_verifier_that_needs_no_reference_is_handed_none(three_traces, loader):
    verifier = _WithoutReference()

    report = await _runner(verifier, ExactMatchVerifier(), loader=loader).run(three_traces[:1])

    assert verifier.references == [None]
    assert report.errors == []


async def test_a_verifier_that_needs_no_reference_judges_without_reference_data(three_traces):
    report = await _runner(_WithoutReference()).run(three_traces)  # type: ignore[arg-type]

    assert report.errors == []


def test_a_reference_need_without_reference_data_is_refused_up_front():
    with pytest.raises(ValueError, match="no reference data"):
        _runner(ExactMatchVerifier())


def test_one_verifier_listed_twice_with_the_same_settings_is_refused(loader):
    """Both would write the one verdict file of their slug, the second over the first."""
    with pytest.raises(ValueError, match="more than once") as refused:
        _runner(ExactMatchVerifier(), ExactMatchVerifier(), loader=loader)
    assert ExactMatchVerifier().identity().slug in str(refused.value)


class _Live:
    verifier_id = "on_the_device"
    needs: frozenset[VerifierNeed] = frozenset({"live_environment", "answer"})

    def identity(self) -> VerifierIdentity:
        return VerifierIdentity(verifier_id=self.verifier_id)

    async def verify(self, trace, reference):  # pragma: no cover - never called
        raise AssertionError


def test_a_verifier_attached_live_never_judges_from_a_trace():
    with pytest.raises(ValueError, match="scoring point"):
        _runner(_Live())  # type: ignore[arg-type]


# ── Each verifier's counts ──────────────────────────────────────────


async def test_tallies_count_each_verifier_over_the_pass(three_traces, loader):
    strict = ExactMatchVerifier(case_sensitive=True)
    lenient = ExactMatchVerifier(allow_substring=True)

    counted = tallies(await _runner(strict, lenient, loader=loader).run(three_traces))

    assert [t.verifier.slug for t in counted] == [
        strict.identity().slug,
        lenient.identity().slug,
    ]
    assert [(t.judged, t.passed, t.failed, t.errors) for t in counted] == [
        (3, 1, 2, 0),
        (3, 2, 1, 0),
    ]
    assert counted[0].usage is None  # exact_match calls no model
    assert "judged 3 of 3" in counted[1].describe()


def test_usage_sums_and_never_reads_unknown_as_free():
    assert total_usage([None, None]) is None
    summed = total_usage(
        [
            JudgeUsage(calls=2, reported_calls=1, prompt_tokens=10, completion_tokens=3),
            None,
            JudgeUsage(calls=1, reported_calls=1, prompt_tokens=5, cached_tokens=2),
        ]
    )
    assert summed == JudgeUsage(
        calls=3, reported_calls=2, prompt_tokens=15, completion_tokens=3, cached_tokens=2
    )
