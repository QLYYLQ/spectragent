"""``spectragent eval``: judge recorded runs with a benchmark's offline verifiers (§4.10.1, §4.11).

The offline attachment point's entry. ``--benchmark`` selects the benchmark,
an alias or a path as for ``spectragent bench``; its block lists the
verifiers, each with its own settings (an LLM judge's model is written in its
block), and its eval loader gives the reference rows. Every verifier of the
benchmark that needs no live environment judges every trace given; each
verdict goes into that run's own verdict store (``verdicts/<slug>.json``), so
the command writes into the run directories. ``--out`` is a new directory for
this batch's report — counts, coverage, failures, and where each verdict is —
never a copy of a verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from spectragent.benchmarks import Benchmark
from spectragent.benchmarks.selection import read_benchmark, run_root
from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.core.trace.encoding import verdict_path
from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.reader import RunTrace
from spectragent.verifiers.base import OfflineVerifier
from spectragent.verifiers.report import VerifierTally, tallies
from spectragent.verifiers.runner import VerificationReport, VerificationRunner, discover_traces

#: The format of ``report.json``.
REPORT_FORMAT = "af-independent-evaluation-2"


def selected_traces(inputs: list[Path]) -> list[Path]:
    """Explicit runs or immediate children of the supplied directories, deduplicated."""
    selected: dict[Path, None] = {}
    for path in inputs:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Trace input is not a directory: {path}")
        found = [path] if (path / "manifest.json").is_file() else discover_traces(path)
        if not found:
            raise ValueError(f"No trace manifests in {path}; discovery is not recursive")
        selected.update(dict.fromkeys(found))
    return list(selected)


def offline_verifiers(benchmark: Benchmark, selection: str) -> list[OfflineVerifier]:
    """The verifiers of *benchmark* that judge from a recorded trace.

    Raises:
        ValueError: It has none.
    """
    verifiers = benchmark.offline_verifiers()
    if not verifiers:
        raise ValueError(
            f"--benchmark {selection}: the benchmark configures no verifier that judges a "
            f"recorded run; list them under verifiers: in its block, in a benchmark YAML of "
            f"your own"
        )
    return verifiers


def reference_data(
    benchmark: Benchmark, verifiers: Sequence[OfflineVerifier], selection: str
) -> BaseEvalLoader | None:
    """The benchmark's reference rows, when a verifier needs one; ``None`` when none does.

    Raises:
        ValueError: A verifier needs one and the benchmark has no reference data.
    """
    needing = [v.verifier_id for v in verifiers if "reference" in v.needs]
    if not needing:
        return None
    loader = benchmark.reference_loader()
    if loader is None:
        raise ValueError(
            f"--benchmark {selection}: verifier(s) {', '.join(needing)} judge against the task's "
            f"reference row, and this benchmark has no reference data"
        )
    return loader


def compose_benchmark(selection: str) -> Benchmark:
    """The benchmark *selection* names, composed as a run root of its own block alone."""
    root = run_root({}, read_benchmark(selection))
    return ConfigComposer.for_config(root).compose(root).single("benchmark")


def _recording(path: Path) -> dict[str, JsonValue]:
    try:
        trace = RunTrace(path)
        return {
            "run_id": trace.run_id,
            "recorded_at": str(trace.manifest.created_at),
            "trace_status": str(trace.manifest.status),
            "source": trace.config.branch.model_dump(mode="json") if trace.config.branch else None,
        }
    except Exception:
        return {"run_id": None, "recorded_at": None, "trace_status": "unreadable"}


def _tally(tally: VerifierTally) -> dict[str, JsonValue]:
    return {
        "slug": tally.verifier.slug,
        "verifier": tally.verifier.model_dump(mode="json"),
        "counts": {
            "provided": tally.provided,
            "judged": tally.judged,
            "passed": tally.passed,
            "failed": tally.failed,
            "errors": tally.errors,
        },
        "coverage": tally.coverage,
        "success_rate": tally.success_rate,
        "judge_usage": tally.usage.model_dump(mode="json") if tally.usage is not None else None,
        "calls_without_usage": tally.calls_without_usage,
    }


def render_report(
    benchmark: str, provided: int, counted: Sequence[VerifierTally], result: VerificationReport
) -> str:
    """The batch report in plain Chinese, one section per verifier."""
    lines = ["# 独立任务评测", "", f"基准：{benchmark}；输入 {provided} 条轨迹。"]
    for tally in counted:
        coverage, success = tally.coverage, tally.success_rate
        usage = tally.usage.model_dump(mode="json") if tally.usage is not None else None
        lines += [
            "",
            f"## {tally.verifier.slug}",
            "",
            f"有效判定 {tally.judged}；判对 {tally.passed}；判错 {tally.failed}；"
            f"无法判定 {tally.errors}。",
            f"有效判定覆盖率：{coverage:.1%}。"
            if coverage is not None
            else "有效判定覆盖率：未测。",
            f"有效判定中的任务正确率：{success:.1%}。"
            if success is not None
            else "任务正确率：未测（没有有效判定）。",
            f"已知 judge 用量：{json.dumps(usage, ensure_ascii=False)}；"
            f"{tally.calls_without_usage} 次调用没有报告用量，未知用量不按免费处理。",
        ]
    lines += [
        "",
        "分母只包含本批提供的轨迹。模型失败、证据不足及未得到有效分数均不算任务判错；"
        "部分覆盖下的正确率只代表已判定部分。此报告与回放保真指标独立。",
        "",
        "每份判定写在它那个 run 的 verdicts/ 里，带判分器的身份、实际提示快照与已报告用量；"
        "本报告只按 run 与 slug 指向判定，不存副本。时效任务依据录制时点及录下的页面事实，"
        "不访问当前网站补真值。",
    ]
    for outcome in result.errors:
        lines.extend(["", f"- 无法判定 {outcome.trace_dir}（{outcome.slug}）: {outcome.error}"])
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Evaluation:
    """What one ``spectragent eval`` produced.

    Attributes:
        report: ``report.json``'s content (a dry run's: what would be judged).
        text: ``report.md``'s content, what the command prints.
        errors: Verifier and run pairs that got no verdict.
    """

    report: dict[str, JsonValue]
    text: str
    errors: int = 0


async def evaluate(
    *,
    benchmark: str,
    traces: list[Path],
    output: Path,
    dry_run: bool = False,
) -> Evaluation:
    """Judge the selected runs; an existing output directory is never overwritten."""
    if output.exists():
        raise ValueError(f"Evaluation output already exists: {output}; choose a new directory")
    paths = selected_traces(traces)
    chosen = compose_benchmark(benchmark)
    verifiers = offline_verifiers(chosen, benchmark)
    runner = VerificationRunner(
        verifiers=verifiers,
        reference_loader=reference_data(chosen, verifiers, benchmark),
        # Only this benchmark's runs: a verifier that reads no reference row
        # (WebJudge) must not judge another benchmark's run in the same root.
        benchmark=chosen.task_loader.benchmark_name,
    )
    identities: list[JsonValue] = [
        identity.model_dump(mode="json") for identity in runner.identities
    ]
    if dry_run:
        await runner.aclose()
        return Evaluation(
            report={"dry_run": True, "provided": len(paths), "verifiers": identities},
            text=f"ready: {len(paths)} trace(s), {len(identities)} verifier(s); no model calls",
        )
    output.mkdir(parents=True, exist_ok=False)
    result = await runner.run(paths)
    counted = tallies(result)
    rows: list[JsonValue] = [
        {
            "trace": str(outcome.trace_dir),
            "task_id": outcome.task_id,
            **_recording(outcome.trace_dir),
            "slug": outcome.slug,
            # Where the verdict is: the run's store. Never a copy.
            "verdict": verdict_path(outcome.slug) if outcome.verdict is not None else None,
            "error": outcome.error,
            "error_evidence": outcome.evidence,
            "error_usage": outcome.usage.model_dump(mode="json") if outcome.usage else None,
        }
        for outcome in result.outcomes
    ]
    report: dict[str, JsonValue] = {
        "format": REPORT_FORMAT,
        "benchmark": benchmark,
        "provided": len(paths),
        "verifiers": [_tally(tally) for tally in counted],
        "outcomes": rows,
    }
    text = render_report(benchmark, len(paths), counted, result)
    atomic_write(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    atomic_write(output / "report.md", text.encode())
    return Evaluation(report=report, text=text, errors=len(result.errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectragent eval",
        description=(
            "Judge recorded runs with a benchmark's offline verifiers, without running agents or "
            "browsers. Each verdict is written into its run's verdicts/."
        ),
        epilog=(
            "Exit codes: 0 every verifier judged every run (a failed task included); 1 some "
            "verifier could not judge some run; 2 the benchmark, the inputs or the output "
            "directory were refused."
        ),
    )
    parser.add_argument(
        "--benchmark",
        required=True,
        help=(
            "The benchmark whose verifiers judge the runs, and whose reference rows they judge "
            "against: a shipped alias (configs/benchmarks/<alias>.yaml) or the path of a "
            "benchmark YAML. An LLM judge's model is written in its verifier block."
        ),
    )
    parser.add_argument(
        "--traces",
        required=True,
        nargs="+",
        type=Path,
        help="Run directories or their immediate parents",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="New directory for this batch's report"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the benchmark, its verifiers and the inputs, without model calls or writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv

    load_dotenv()
    try:
        evaluation = asyncio.run(
            evaluate(
                benchmark=args.benchmark,
                traces=args.traces,
                output=args.out,
                dry_run=args.dry_run,
            )
        )
    except (ValueError, OSError, ComposeError, BenchmarkDataError) as exc:
        print(f"evaluation refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(evaluation.text)
    return 1 if evaluation.errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
