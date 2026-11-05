"""A benchmark owns its task loader and its reference data (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.benchmarks import BenchmarkRegistrar
from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
from spectragent.schemas.verdict import Judgment, Verdict, VerifierIdentity


def _data(root: Path, benchmark: str, subset: str, *task_ids: str) -> Path:
    """``<root>/browser/<benchmark>``: one split of tasks, each its own reference."""
    directory = root / "browser" / benchmark
    directory.mkdir(parents=True, exist_ok=True)
    info = directory / "data_info.json"
    split = json.loads(info.read_text())["split"] if info.is_file() else {}
    info.write_text(json.dumps({"split": {**split, subset: f"{subset}.json"}}))
    records = [
        {"task_id": task_id, "task_text": task_id, "expected_answer": f"answer {task_id}"}
        for task_id in task_ids
    ]
    (directory / f"{subset}.json").write_text(json.dumps(records))
    return directory


def _benchmark(**block: object) -> object:
    raw = {"benchmark": [block]}
    return ConfigComposer.for_config(raw).compose(raw).single("benchmark")


def test_each_benchmark_takes_its_own_loaders() -> None:
    """A benchmark writes its complete wiring: its own task loader, and eval loader when it has one."""
    ConfigComposer.for_every_package()
    owned = {
        key: (cls.__wiring__["task_loader"][1], cls.__wiring__.get("eval_loader", (None, []))[1])
        for key, cls in BenchmarkRegistrar.get_all().items()
    }
    assert owned == {
        "androidworld": (["androidworld"], []),
        "browsecomp": (["browsecomp"], ["browsecomp"]),
        "lexbench_browser": (["lexbench_browser"], ["lexbench_browser"]),
        "online_mind2web": (["online_mind2web"], ["online_mind2web"]),
        "trace_branches": (["trace_branches"], []),
        "venusbench": (["venusbench"], []),
    }
    for key, (tasks, references) in owned.items():
        assert TaskLoaderRegistrar.get(tasks[0]) is not None, key
        for reference in references:
            assert EvalLoaderRegistrar.get(reference) is not None, key


def test_a_benchmark_block_needs_its_task_loader() -> None:
    raw = {"benchmark": [{"benchmark_id": "online_mind2web"}]}
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert (error["type"], error["loc"][-1]) == ("missing", "task_loader")


def test_another_benchmarks_loader_is_refused() -> None:
    raw = {
        "benchmark": [
            {"benchmark_id": "online_mind2web", "task_loader": {"benchmark_name": "browsecomp"}}
        ]
    }
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert error["type"] == "literal_error" and "online_mind2web" in error["msg"]


def test_an_unknown_benchmark_is_refused_before_anything_is_imported() -> None:
    raw = {"benchmark": [{"benchmark_id": "online_mindweb", "task_loader": {}}]}
    with pytest.raises(ComposeError) as refused:
        ConfigComposer.for_config(raw)
    assert refused.value.issue is not None
    assert refused.value.issue.code == "unknown_benchmark_package"
    assert "benchmark[0].benchmark_id" in str(refused.value)


def test_the_references_are_the_eval_loader_block(tmp_path: Path) -> None:
    directory = _data(tmp_path, "online_mind2web", "Hard", "t1")
    loader = {"benchmark_name": "online_mind2web", "subset": "Hard", "data_dir": str(directory)}
    benchmark = _benchmark(benchmark_id="online_mind2web", task_loader=loader, eval_loader=loader)
    references = benchmark.reference_loader()  # type: ignore[attr-defined]
    assert references is benchmark.eval_loader  # type: ignore[attr-defined]
    assert references.get("t1").expected_answer == "answer t1"


def test_without_a_block_the_references_are_the_registered_loader_for_the_task_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))
    _data(tmp_path, "online_mind2web", "All", "a1")
    _data(tmp_path, "online_mind2web", "Hard", "h1")
    benchmark = _benchmark(
        benchmark_id="online_mind2web",
        task_loader={"benchmark_name": "online_mind2web", "subset": "Hard"},
    )
    assert benchmark.eval_loader is None  # type: ignore[attr-defined]
    references = benchmark.reference_loader()  # type: ignore[attr-defined]
    assert (references.subset, references.task_ids()) == ("Hard", ["h1"])


def test_a_branch_sweep_has_no_references(tmp_path: Path) -> None:
    benchmark = _benchmark(
        benchmark_id="trace_branches",
        task_loader={
            "benchmark_name": "trace_branches",
            "data_dir": str(tmp_path),
            "step_selector": {"selector_id": "all"},
        },
    )
    assert benchmark.reference_loader() is None  # type: ignore[attr-defined]
    assert benchmark.task_loader.task_ids() == []  # type: ignore[attr-defined]


def test_each_benchmark_names_the_verifier_kinds_it_takes() -> None:
    """A branch sweep's task source takes none: a branch task is judged by its source's benchmark."""
    ConfigComposer.for_every_package()
    taken = {
        key: list(cls.__wiring__.get("verifiers", (None, []))[1])
        for key, cls in BenchmarkRegistrar.get_all().items()
    }
    assert taken == {
        "androidworld": ["androidworld"],
        "browsecomp": ["browsecomp", "exact_match"],
        "lexbench_browser": ["lexbench_browser"],
        "online_mind2web": ["online_mind2web"],
        "trace_branches": [],
        "venusbench": ["venusbench"],
    }


def test_a_block_that_lists_no_verifier_has_none(tmp_path: Path) -> None:
    directory = _data(tmp_path, "browsecomp", "All", "t1")
    loader = {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(directory)}
    benchmark = _benchmark(benchmark_id="browsecomp", task_loader=loader)
    assert benchmark.verifiers == []  # type: ignore[attr-defined]
    assert benchmark.offline_verifiers() == []  # type: ignore[attr-defined]


def test_the_offline_verifiers_are_the_listed_ones_in_order(tmp_path: Path) -> None:
    directory = _data(tmp_path, "browsecomp", "All", "t1")
    loader = {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(directory)}
    benchmark = _benchmark(
        benchmark_id="browsecomp",
        task_loader=loader,
        verifiers=[
            {"verifier_id": "exact_match", "allow_substring": True},
            {"verifier_id": "exact_match"},
        ],
    )
    listed = benchmark.offline_verifiers()  # type: ignore[attr-defined]
    assert [verifier.allow_substring for verifier in listed] == [True, False]


def test_another_benchmarks_verifier_is_refused() -> None:
    raw = {
        "benchmark": [
            {
                "benchmark_id": "online_mind2web",
                "task_loader": {"benchmark_name": "online_mind2web"},
                "verifiers": [{"verifier_id": "exact_match"}],
            }
        ]
    }
    with pytest.raises(ValidationError, match="exact_match"):
        ConfigComposer.for_config(raw).validate(raw)


def test_verifiers_are_no_part_of_the_runs_configuration_or_its_hash(tmp_path: Path) -> None:
    """Judging a sweep with another judge leaves its runs one experiment (§5.3 "实验身份")."""
    from spectragent.core.trace.models import TraceConfig
    from spectragent.core.trace.redaction import redact_config

    directory = _data(tmp_path, "browsecomp", "All", "t1")
    loader = {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(directory)}

    def recorded(verifiers: list[dict[str, object]] | None) -> dict[str, object]:
        block: dict[str, object] = {"benchmark_id": "browsecomp", "task_loader": loader}
        if verifiers is not None:
            block["verifiers"] = verifiers
        raw = {"benchmark": [block]}
        run = ConfigComposer.for_config(raw).compose(raw)
        return redact_config(run.run_configuration(), record_secrets=True)

    none = recorded(None)
    judged = recorded([{"verifier_id": "exact_match", "allow_substring": True}])
    assert none == judged
    assert judged["benchmark"][0]["verifiers"] is None  # type: ignore[index]
    assert judged["benchmark"][0]["task_loader"]["subset"] == "All"  # type: ignore[index]
    digest = TraceConfig().configuration_digest
    assert digest(resolved=none) == digest(resolved=judged)


# ── the primary verdict and the success rule (§4.10.1 "主判定") ──────────


def _judged(
    verifier_id: str,
    *,
    attachment: Literal["live", "offline"] = "offline",
    config: dict[str, JsonValue] | None = None,
    label: Literal[0, 1] | None = None,
    score: float | None = None,
    evidence: dict[str, JsonValue] | None = None,
) -> Verdict:
    return Verdict(
        verifier=VerifierIdentity(verifier_id=verifier_id, config_signature=config or {}),
        attachment=attachment,
        status="judged",
        label=label,
        score=score,
        evidence=evidence or {},
        started_at=1.0,
        duration_ms=1,
    )


def test_each_browser_benchmark_declares_its_own_judge_primary() -> None:
    ConfigComposer.for_every_package()
    primary = {key: cls.__primary_verifier__ for key, cls in BenchmarkRegistrar.get_all().items()}
    assert primary == {
        "androidworld": "androidworld",
        "browsecomp": "browsecomp",
        "lexbench_browser": "lexbench_browser",
        "online_mind2web": "online_mind2web",
        "trace_branches": None,
        "venusbench": "venusbench",
    }


def test_the_primary_verdict_is_the_first_listed_verifier_of_the_primary_kind() -> None:
    from spectragent.benchmarks.browsecomp import BrowseCompBenchmark, BrowseCompVerifier
    from spectragent.verifiers.exact_match import ExactMatchVerifier

    first, second = BrowseCompVerifier(), BrowseCompVerifier()
    first.llm = type("Judge", (), {"model": "judge-a"})()  # type: ignore[assignment]
    second.llm = type("Judge", (), {"model": "judge-b"})()  # type: ignore[assignment]
    benchmark = BrowseCompBenchmark()
    benchmark.verifiers = [ExactMatchVerifier(), first, second]
    passed, failed = first.identity(), second.identity()
    verdicts = {
        passed.slug: _judged("browsecomp", label=1, config=passed.config_signature),
        failed.slug: _judged("browsecomp", label=0, config=failed.config_signature),
    }
    verdicts[ExactMatchVerifier().identity().slug] = _judged("exact_match", label=0)

    primary = benchmark.primary_verdict(verdicts)
    assert primary is not None and primary.slug == first.identity().slug
    assert benchmark.success(primary, None) == 1.0
    # The listed one judged nothing: there is no primary verdict, never the other judge's.
    del verdicts[first.identity().slug]
    assert benchmark.primary_verdict(verdicts) is None


def test_androidworlds_listed_verifier_gives_its_primary_verdict_from_the_scoring_point() -> None:
    """Listed in its block, the live verifier's verdict is found by its slug, as any listed one's."""
    from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
    from spectragent.benchmarks.androidworld.verifier import AndroidWorldVerifier

    benchmark = AndroidWorldBenchmark()
    benchmark.verifiers = [AndroidWorldVerifier()]
    live = Verdict.judged(
        VerifierIdentity(verifier_id="androidworld", version=PINNED_COMMIT),
        "live",
        Judgment(score=1.0, evidence={"family": "miniwob"}),
        started_at=1.0,
        duration_ms=1,
    )
    assert benchmark.live_verifiers() == benchmark.verifiers
    assert benchmark.offline_verifiers() == []
    assert benchmark.primary_verdict({live.slug: live}) == live
    assert benchmark.success(live, None) == 1.0


def test_a_live_verifier_that_names_no_host_is_refused() -> None:
    from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark

    class _Hostless:
        verifier_id = "hostless"
        needs = frozenset({"live_environment"})

        def identity(self) -> VerifierIdentity:
            return VerifierIdentity(verifier_id=self.verifier_id)

    benchmark = AndroidWorldBenchmark()
    benchmark.verifiers = [_Hostless()]  # type: ignore[list-item]
    with pytest.raises(TypeError, match="'hostless' needs the live environment and names no"):
        benchmark.live_verifiers()


def test_the_default_success_rule_reads_the_label() -> None:
    from spectragent.benchmarks.online_mind2web import OnlineMind2WebBenchmark

    benchmark = OnlineMind2WebBenchmark()
    assert benchmark.success(_judged("online_mind2web", label=1), None) == 1.0
    assert benchmark.success(_judged("online_mind2web", label=0), None) == 0.0
    assert benchmark.success(_judged("online_mind2web", score=3.0), None) is None
    unjudged = Verdict(
        verifier=VerifierIdentity(verifier_id="online_mind2web"),
        attachment="live",
        status="not_judged",
        reason="timeout",
        started_at=1.0,
        duration_ms=1,
    )
    assert benchmark.success(unjudged, None) is None
    assert benchmark.primary_verifier == "online_mind2web"


@pytest.mark.parametrize(
    ("family", "agent_done", "score", "success"),
    [
        ("android", "done", 1.0, 1.0),
        ("android", "done", 0.5, 0.5),
        ("android", "max_steps", 1.0, 0.0),
        ("information_retrieval", "error", 1.0, 0.0),
        ("miniwob", "episode_over", 1.0, 1.0),
        ("miniwob", "max_steps", 0.0, 0.0),
    ],
)
def test_androidworld_counts_the_score_only_when_the_agent_declared_done(
    family: str, agent_done: str, score: float, success: float
) -> None:
    """Its runner's ``task_successful if done else 0``; a MiniWoB++ page ends its own episode."""
    from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
    from tests._helpers.trace_builder import agent_result

    verdict = _judged("androidworld", attachment="live", score=score, evidence={"family": family})
    result = agent_result(agent_done=agent_done, agent_reported_success=None)
    assert AndroidWorldBenchmark().success(verdict, result) == success
    # No result: the run's ending is unknown, so no success either way; a
    # MiniWoB++ task's page ended it, so its score still stands.
    unknown = score if family == "miniwob" else None
    assert AndroidWorldBenchmark().success(verdict, None) == unknown


@pytest.mark.parametrize(
    ("agent_done", "score", "success"),
    [("done", 1.0, 1.0), ("done", 0.0, 0.0), ("max_steps", 1.0, 0.0), ("episode_over", 1.0, 0.0)],
)
def test_venusbench_counts_the_score_only_when_the_agent_declared_done(
    agent_done: str, score: float, success: float
) -> None:
    """Its runner is AndroidWorld's (``task_successful if done else 0``), and it has no MiniWoB++."""
    from spectragent.benchmarks.venusbench.benchmark import VenusBenchBenchmark
    from tests._helpers.trace_builder import agent_result

    verdict = _judged("venusbench", attachment="live", score=score, evidence={"family": "venus"})
    result = agent_result(agent_done=agent_done, agent_reported_success=None)
    assert VenusBenchBenchmark().success(verdict, result) == success
    assert VenusBenchBenchmark().success(verdict, None) is None, "an unknown ending claims nothing"
