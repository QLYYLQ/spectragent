"""Evaluate existing traces as an independent, YAML-configured stage (§4.11)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import redact_config
from spectragent.evaluator.runner import EvaluationRunner, discover_traces
from spectragent.evaluator.summary import aggregate_judge_tokens
from spectragent.scripts.generate_all import _collect_registrars


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


def compose_evaluation(config: Path):
    """Use the ordinary Composer without constructing a browser or an agent."""
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Evaluation config must be a YAML mapping")
    unexpected = set(raw) - {"evaluator", "eval_loader", "llm"}
    if unexpected:
        raise ValueError(f"Independent evaluation does not accept layers {sorted(unexpected)}")
    run = ConfigComposer(registrars=_collect_registrars()).compose(raw)
    return run, run.single("evaluator"), run.single("eval_loader")


def _recording(path: Path) -> dict[str, Any]:
    try:
        trace = RunTrace(path)
        return {
            "run_id": trace.run_id,
            "recorded_at": str(trace.manifest.created_at),
            "trace_status": str(trace.manifest.status),
            "source": trace.config.branch.model_dump() if trace.config.branch else None,
        }
    except Exception:
        return {"run_id": None, "recorded_at": None, "trace_status": "unreadable"}


def render_report(report: dict[str, Any]) -> str:
    counts = report["counts"]
    coverage = report["coverage"]
    success = report["success_rate"]
    lines = [
        "# 独立任务评测",
        "",
        f"输入 {counts['provided']} 条轨迹；有效判定 {counts['graded']}；"
        f"判对 {counts['passed']}；判错 {counts['failed']}；无法判定 {counts['errors']}。",
        f"有效判定覆盖率：{coverage:.1%}。" if coverage is not None else "有效判定覆盖率：未测。",
        f"有效判定中的任务正确率：{success:.1%}。"
        if success is not None
        else "任务正确率：未测（没有有效判定）。",
        "",
        "分母只包含本批提供的轨迹。模型失败、证据不足及未得到有效分数均不算任务判错；"
        "部分覆盖下的正确率只代表已判定部分。此报告与回放保真指标独立。",
        "",
        "judge 配置与路由身份在 report.json；每份判定保存实际提示快照、录制时间与已报告用量。"
        "时效任务依据录制时点及录下的页面事实，不访问当前网站补真值。",
    ]
    for row in report["outcomes"]:
        if row["error"]:
            lines.extend(["", f"- 无法判定 {row['trace']}: {row['error']}"])
    usage = report["judge_usage"]
    lines.extend(["", f"已知 judge 用量：{json.dumps(usage, ensure_ascii=False)}。"])
    lines.append(f"{report['usage_missing']} 条输入没有可读的已报告用量；未知用量不按免费处理。")
    return "\n".join(lines) + "\n"


async def evaluate(
    *,
    config: Path,
    traces: list[Path],
    output: Path,
    write_into_trace: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Score only selected runs; an existing output directory is never overwritten."""
    if output.exists():
        raise ValueError(f"Evaluation output already exists: {output}; choose a new directory")
    paths = selected_traces(traces)
    run, evaluator, loader = compose_evaluation(config)
    runner = EvaluationRunner(
        evaluator=evaluator,
        eval_loader=loader,
        evals_dir=output,
        write_into_trace=write_into_trace,
        verify_reference_identity=True,
    )
    identity = {
        "evaluator_id": runner.evaluator_id,
        "verdict_slug": runner.verdict_slug,
        "evaluator_config": evaluator.config_signature(),
        "resolved": redact_config(run.top_level_cfg),
    }
    if dry_run:
        return {"dry_run": True, "provided": len(paths), **identity}
    output.mkdir(parents=True, exist_ok=False)
    # Deliberately no expected_task_ids: this stage evaluates provided evidence,
    # not an unstated whole benchmark split.
    result = await runner.run(
        paths,
        benchmark=loader.benchmark_name,
        subset=loader.subset,
    )
    rows = []
    for outcome in result.outcomes:
        if outcome.trace_dir is None:
            continue  # Legacy coverage-policy placeholders are not task verdicts.
        rows.append(
            {
                "trace": str(outcome.trace_dir),
                "task_id": outcome.task_id,
                **_recording(outcome.trace_dir),
                "result": outcome.result.model_dump(mode="json") if outcome.result else None,
                "error": outcome.error,
                "error_details": outcome.details.model_dump(mode="json")
                if outcome.details
                else None,
            }
        )
    graded = result.graded
    passed = sum(item.predicted_label == 1 for item in graded)
    known_usage = [
        row["result"]["evaluation_details"].get("eval_usage")
        if row["result"]
        else (row["error_details"] or {}).get("eval_usage")
        for row in rows
    ]
    usage = aggregate_judge_tokens(result.results)
    report = {
        "format": "af-independent-evaluation-1",
        **identity,
        "counts": {
            "provided": len(paths),
            "graded": len(graded),
            "passed": passed,
            "failed": len(graded) - passed,
            "errors": len(paths) - len(graded),
        },
        "coverage": len(graded) / len(paths) if paths else None,
        "success_rate": passed / len(graded) if graded else None,
        "judge_usage": usage.model_dump() if usage else None,
        "usage_missing": (
            sum(item is None for item in known_usage)
            if getattr(evaluator, "llm", None) is not None
            else 0
        ),
        "outcomes": rows,
    }
    atomic_write(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    atomic_write(output / "report.md", render_report(report).encode())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate recorded tasks without running agents or browsers."
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="YAML with one evaluator and eval_loader"
    )
    parser.add_argument(
        "--traces",
        required=True,
        nargs="+",
        type=Path,
        help="Run directories or their immediate parents",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="New directory for this evaluation batch"
    )
    parser.add_argument(
        "--read-only-traces",
        action="store_true",
        help="Write only the independent batch, not trace evals/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and selection without model calls or writes",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv

    load_dotenv()
    try:
        report = asyncio.run(
            evaluate(
                config=args.config,
                traces=args.traces,
                output=args.out,
                write_into_trace=not args.read_only_traces,
                dry_run=args.dry_run,
            )
        )
    except (ValueError, OSError, ComposeError, BenchmarkDataError) as exc:
        print(f"evaluation refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"ready: {report['provided']} trace(s), {report['evaluator_id']}; no model calls")
        return 0
    print(render_report(report))
    return 1 if report["counts"]["errors"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
