"""Present committed experiments without creating a second trace reader.

Directory discovery belongs here; every access *inside* a trace belongs to
RunTrace. A new reader per query is intentional: it is a coherent manifest
snapshot, while a reader held across UI refreshes would retain stale caches.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import task_identity
from spectragent.core.trace.usage import recorded_usage
from spectragent.inspection.restoration import (
    restoration_gap,
    restoration_overview,
    restoration_results,
)
from spectragent.schemas.observation.image import read_model_image


class RepositoryError(ValueError):
    """The selected experiment cannot be inspected, with an actionable reason."""


class RunRepository(Protocol):
    """JSON-ready read surface; keys are paths relative to the selected root."""

    def list_runs(self, search: str = "") -> list[dict[str, Any]]: ...
    def matches(self, key: str) -> bool: ...
    def run(self, key: str) -> dict[str, Any]: ...
    def step(self, key: str, index: int) -> dict[str, Any]: ...
    def screenshot(
        self, key: str, index: int, *, view: Literal["browser", "agent"] = "browser"
    ) -> bytes | None: ...
    def compare(self, keys: list[str]) -> dict[str, Any]: ...
    def compare_groups(self, left: str, right: str, evaluator: str) -> dict[str, Any]: ...
    def reports(self) -> list[dict[str, Any]]: ...
    def report(self, key: str) -> dict[str, Any]: ...


def open_repository(
    root: Path, *, config_source: Path | str | None = None, config_hash: str | None = None
) -> RunRepository:
    """Open a local experiment root, which may itself be a single run."""
    return _FileRepository(root, config_source=config_source, config_hash=config_hash)


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        data = value.model_dump(mode="python")
        for name in type(value).model_fields:
            original = getattr(value, name)
            if isinstance(original, bytes) and name in data:
                data[name] = {"bytes": len(original)}
        return _json(data)
    if isinstance(value, bytes):
        # The screenshot endpoint serves image bytes. Never put megabytes of
        # byte reprs (or a failed UTF-8 decode) into generic fragment details.
        return {"bytes": len(value)}
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _has_screenshot(entry: Any) -> bool:
    return any(
        key.rsplit(".", 1)[-1] == "screenshot" and files.artifact and not files.failed
        for key, files in entry.fragments.items()
    )


def _evaluations(trace: RunTrace) -> tuple[dict[str, Any], str | None]:
    try:
        return trace.evals(), None
    except TraceSchemaError as exc:
        # Evaluations are a separate post-run ledger. A broken verdict must
        # not hide intact observations and model calls, or relabel the WAL.
        return {}, str(exc)


def _verdicts(evaluations: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "evaluator": slug,
            "predicted_label": item.predicted_label,
            "verdict_source": item.verdict_source,
        }
        for slug, item in sorted(evaluations.items())
    ]


def _mappings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mappings(child)


def _replay_archive(config: TraceConfig) -> str | None:
    context = config.task_setup.context if config.task_setup else {}
    value = context.get("replay_archive")
    if not isinstance(value, str):
        browser = context.get("browser", {})
        value = browser.get("replay_archive") if isinstance(browser, dict) else None
    return value if isinstance(value, str) else None


def _network_source(config: TraceConfig) -> str | None:
    sources = {
        block["network_source"].get("source_id")
        for block in _mappings(config.resolved)
        if block.get("capability_id") == "browser" and isinstance(block.get("network_source"), dict)
    }
    sources &= {"live", "archive"}
    if len(sources) == 1:
        return next(iter(sources))
    # replay_archive is an explicit recorded setup fact, not a directory-name guess.
    return "archive" if _replay_archive(config) is not None else None


def _execution_kind(config: TraceConfig) -> str:
    network = _network_source(config)
    if config.branch is not None:
        return "branch_replay" if network == "archive" else "branch"
    return "replay" if network == "archive" else "live" if network == "live" else "unknown"


def _strategy(config: TraceConfig) -> dict[str, Any]:
    loops = [
        block for block in _mappings(config.resolved) if block.get("loop_id") == config.loop_id
    ]
    loop = loops[0] if len(loops) == 1 else {}
    restorer = loop.get("restorer") or {}
    return {
        "history_source": loop.get("history_source"),
        "verify": restorer.get("verify") if isinstance(restorer, dict) else None,
        "restorer": restorer.get("restorer_id") if isinstance(restorer, dict) else None,
        "continue_steps": loop.get("continue_steps"),
        "max_steps": loop.get("max_steps"),
    }


def _replay_log(trace: RunTrace) -> dict[str, Any]:
    from spectragent.core.capabilities.browser.replay.replay_log import iter_decisions

    enabled = bool(
        trace.config.negotiation
        and "browser.replay_log" in trace.config.negotiation.active_archivers
    )
    entries: list[dict[str, Any]] = []
    counts = dict.fromkeys(("hit", "miss", "exhausted", "synthetic"), 0)
    try:
        for step, entry, weight in iter_decisions(trace):
            if entry.kind not in counts:
                raise ValueError("A folded replay log entry does not identify its decision kind")
            entries.append({"step": step, **entry.model_dump(mode="json"), "weight": weight})
            counts[entry.kind] += weight
    except (TraceSchemaError, ValueError, KeyError) as exc:
        return {
            "recorded": enabled or bool(entries),
            "entries": entries,
            "counts": None,
            "error": str(exc),
        }
    recorded = enabled or bool(entries)
    return {
        "recorded": recorded,
        "entries": entries,
        "counts": counts if recorded else None,
        "error": None,
    }


def _summary(trace: RunTrace, key: str, usage: Any = None) -> dict[str, Any]:
    config, result, manifest = trace.config, trace.result, trace.manifest
    records = trace.step_records
    metrics = result.metrics if result else None
    usage = recorded_usage(trace) if usage is None else usage
    evaluations, evaluation_error = _evaluations(trace)
    calls = usage.calls
    return {
        "key": key,
        "run_id": trace.run_id,
        "task_id": config.task_id,
        "task": config.task,
        "benchmark": config.benchmark,
        "subset": config.subset,
        "agent_id": config.agent_id,
        "loop_id": config.loop_id,
        "model": config.llm_model,
        "config_hash": manifest.config_hash,
        "config_source": _json(config.config_source),
        "execution_kind": _execution_kind(config),
        "strategy": _strategy(config),
        "created_at": manifest.created_at.isoformat(),
        "updated_at": manifest.updated_at.isoformat(),
        "agent_done": str(result.agent_done) if result else None,
        "env_status": str(result.env_status) if result else None,
        "trace_status": manifest.status.value,
        "complete_through": manifest.complete_through,
        "recording": _json(config.recording),
        "missing_steps": manifest.missing_steps,
        "recording_problems": _json(manifest.recording_problems),
        "usage_measured": usage.measured,
        "usage_complete": usage.complete,
        "policy_steps": sum(record.origin == "policy" for record in records),
        "total_steps": len(records),
        "llm_calls": calls,
        "tokens": usage.tokens
        if not usage.calls or usage.calls > len(usage.without_usage)
        else None,
        "usage_missing": len(usage.without_usage),
        "execution_ms": metrics.execution_ms if metrics else None,
        "answer": result.answer if result else "",
        "error": result.error if result else None,
        "branch": _json(config.branch),
        "verdicts": _verdicts(evaluations),
        "evaluation_error": evaluation_error,
        "read_error": None,
    }


class _FileRepository:
    def __init__(
        self, root: Path, *, config_source: Path | str | None = None, config_hash: str | None = None
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.config_source = (
            str(Path(config_source).expanduser().resolve()) if config_source is not None else None
        )
        self.config_hash = config_hash
        if not self.root.is_dir():
            raise FileNotFoundError(f"Experiment directory does not exist: {self.root}")

    def _path(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if not key or relative.is_absolute() or ".." in relative.parts or "\\" in key:
            raise RepositoryError("Choose a relative path inside the experiment directory")
        path = self.root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(self.root):
            raise RepositoryError(f"Path leaves the experiment directory: {key}")
        return path

    def _trace(self, key: str) -> RunTrace:
        path = self._path(key)
        if not (path / "manifest.json").exists():
            raise FileNotFoundError(f"No recorded run at {key}")
        try:
            return RunTrace(path)
        except TraceSchemaError as exc:
            raise RepositoryError(str(exc)) from exc

    def _matches(self, trace: RunTrace) -> bool:
        source = trace.config.config_source
        return (
            self.config_source is None or (source is not None and source.path == self.config_source)
        ) and (self.config_hash is None or trace.manifest.config_hash == self.config_hash)

    def matches(self, key: str) -> bool:
        """Initial focus obeys filters; lineage navigation may leave the filter, never the root."""
        try:
            return self._matches(self._trace(key))
        except TraceSchemaError as exc:
            raise RepositoryError(str(exc)) from exc

    def _directories(self) -> Iterator[tuple[Path, list[str]]]:
        for current, directories, files in os.walk(self.root, followlinks=False):
            path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in {".git", ".venv", "node_modules", "__pycache__"}
                and (not name.startswith(".") or name == ".sessions")
                and not (path / name).is_symlink()
            )
            yield path, files
            # All contents of a run/dataset are private to its reader. In
            # particular, do not find fake reports in archived web content.
            if "manifest.json" in files:
                directories.clear()

    def _kind(self, path: Path) -> str | None:
        try:
            data = self._read_json(path)
        except (RepositoryError, FileNotFoundError):
            return None
        if not isinstance(data, dict):
            return None
        fmt = str(data.get("format", ""))
        if fmt.startswith("af-independent-evaluation-"):
            return "evaluation"
        if fmt.startswith("af-flywheel-report-"):
            return "flywheel"
        if path.parent.name == ".sessions" and fmt == "af-session-1":
            return "session"
        if path.name == "diagnostics.json":
            return "diagnostics"
        if path.name == "manifest.json" and "samples" in data and "exporter" in data:
            return "dataset"
        return None

    def _read_json(self, path: Path) -> Any:
        if not path.resolve().is_relative_to(self.root):
            raise RepositoryError("Report path leaves the experiment directory")
        if not path.is_file():
            raise FileNotFoundError(f"Report does not exist: {path.name}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RepositoryError(f"Cannot read {path.relative_to(self.root)}: {exc}") from exc

    def list_runs(self, search: str = "") -> list[dict[str, Any]]:
        rows = []
        for directory, files in self._directories():
            if "manifest.json" not in files or self._kind(directory / "manifest.json") == "dataset":
                continue
            key = directory.relative_to(self.root).as_posix()
            try:
                trace = self._trace(key)
                if self._matches(trace):
                    rows.append(_summary(trace, key))
            except (RepositoryError, TraceSchemaError, OSError) as exc:
                if self.config_source is not None or self.config_hash is not None:
                    # No readable identity means no evidence of membership.
                    continue
                rows.append(
                    {
                        "key": key,
                        "run_id": directory.name,
                        "read_error": str(exc),
                        "trace_status": "unreadable",
                        "verdicts": [],
                    }
                )
        needle = search.casefold()
        if needle:
            rows = [
                row for row in rows if needle in " ".join(str(v) for v in row.values()).casefold()
            ]
        return sorted(rows, key=lambda row: (row.get("created_at", ""), row["key"]), reverse=True)

    def run(self, key: str) -> dict[str, Any]:
        trace = self._trace(key)
        try:
            usage = recorded_usage(trace)
            summary = _summary(trace, key, usage)
            steps = []
            for entry in trace.manifest.steps:
                record = trace.step(entry.step)
                assert record is not None
                steps.append(
                    {
                        **_json(record),
                        "recording_ms": trace.recording_timing(entry.step),
                        "has_screenshot": bool(_has_screenshot(entry)),
                        "fragment_keys": list(entry.fragments),
                        "gap": restoration_gap(_json(record)),
                        "call_count": len(entry.call_metrics)
                        if trace.config.recording.metrics
                        else None,
                    }
                )
            execution = self._execution(trace)
            trailing = trace.trailing_calls if trace.config.recording.trajectory else []
            return {
                "summary": summary,
                "config": _json(trace.config),
                "result": _json(trace.result),
                "manifest": _json(trace.manifest),
                "steps": steps,
                "evaluations": _json(_evaluations(trace)[0]),
                "evaluation_error": summary["evaluation_error"],
                "trailing_calls": _json(trailing),
                "trailing_call_metrics": _json(trace.trailing_call_metrics),
                "trailing_output_schemas": {
                    call.output_format.sha256: trace.output_schema(call.output_format.sha256)
                    for call in trailing
                    if call.output_format is not None
                },
                "usage": _json(usage),
                "source_key": execution["source_key"],
                "execution": execution,
                "restoration_overview": restoration_overview(
                    steps, trace.config.branch.checkpoint_step
                )
                if trace.config.branch
                else None,
                "replay_log": _replay_log(trace),
            }
        except (TraceSchemaError, KeyError) as exc:
            raise RepositoryError(str(exc)) from exc

    def _source_match(self, run_id: str) -> tuple[str | None, str]:
        candidates = []
        for directory, files in self._directories():
            if "manifest.json" not in files:
                continue
            key = directory.relative_to(self.root).as_posix()
            try:
                if self._trace(key).run_id == run_id:
                    candidates.append(key)
            except (RepositoryError, OSError):
                continue
        if len(candidates) == 1:
            return candidates[0], "found"
        return None, "ambiguous" if candidates else "missing"

    def _execution(self, trace: RunTrace) -> dict[str, Any]:
        config = trace.config
        branch = config.branch
        source_key, resolution, source_run_id = None, "none", None
        if branch is not None:
            source_run_id = branch.source_run_id
            source_key, resolution = self._source_match(source_run_id)
        else:
            archive = _replay_archive(config)
            if archive is not None:
                path = Path(archive).expanduser()
                # Runtime records an absolute source. Never reinterpret an
                # old relative path against the viewer's current directory.
                if not path.is_absolute():
                    resolution = "missing"
                elif not path.resolve().is_relative_to(self.root):
                    resolution = "outside_root"
                else:
                    key = path.resolve().relative_to(self.root).as_posix()
                    try:
                        source_run_id = self._trace(key).run_id
                        source_key, resolution = key, "found"
                    except (RepositoryError, FileNotFoundError):
                        resolution = "missing"
        return {
            "network_source": _network_source(config),
            "source_key": source_key,
            "source_run_id": source_run_id,
            "checkpoint": branch.checkpoint_step if branch else None,
            "relationship": "branch" if branch else "replay" if _replay_archive(config) else None,
            "source_resolution": resolution,
            **_strategy(config),
        }

    def _comparison(self, trace: RunTrace, record: Any) -> dict[str, Any] | None:
        if trace.config.branch is None or (
            record.restoration is None and record.origin != "restored"
        ):
            return None
        execution = self._execution(trace)
        source_step = record.source_history.source_step if record.source_history else record.step
        comparison = {
            "history_source": execution["history_source"],
            "source_key": execution["source_key"],
            "source_step": source_step,
            "source_status": execution["source_resolution"],
            "source": None,
            "error": None,
            "results": restoration_results(_json(record), None),
        }
        if (
            record.source_history
            and record.source_history.source_run_id != trace.config.branch.source_run_id
        ):
            comparison.update(
                source_key=None,
                source_status="inconsistent_reference",
                error="The step and run name different source runs.",
            )
            return comparison
        if execution["source_key"] is None:
            return comparison
        try:
            source = self._trace(execution["source_key"])
            original = source.step(source_step)
            if original is None:
                comparison["source_status"] = "missing_step"
                return comparison
            entry = next(item for item in source.manifest.steps if item.step == source_step)
            comparison["source"] = {
                "record": _json(original),
                "fragments": {
                    name: _json(source.observation_fragment(source_step, name))
                    for name in entry.fragments
                },
                "has_screenshot": bool(_has_screenshot(entry)),
                "recording": _json(source.config.recording),
            }
            comparison["results"] = restoration_results(_json(record), _json(original))
        except (RepositoryError, TraceSchemaError, OSError, KeyError) as exc:
            comparison.update(source_status="unreadable", error=str(exc))
        return comparison

    def step(self, key: str, index: int) -> dict[str, Any]:
        trace = self._trace(key)
        try:
            record = trace.step(index)
            if record is None:
                raise FileNotFoundError(f"Run {key} has no committed step {index}")
            entry = next(entry for entry in trace.manifest.steps if entry.step == index)
            calls = trace.llm_messages(index) if trace.config.recording.trajectory else []
            schemas = {
                call.output_format.sha256: trace.output_schema(call.output_format.sha256)
                for call in calls
                if call.output_format is not None
            }
            return {
                "record": {**_json(record), "recording_ms": trace.recording_timing(index)},
                "fragments": {
                    name: _json(trace.observation_fragment(index, name)) for name in entry.fragments
                },
                "calls": _json(calls),
                "call_metrics": _json(trace.call_metrics(index)),
                "recording": _json(trace.config.recording),
                "output_schemas": schemas,
                "archive": _json(
                    [
                        record
                        for archiver in (
                            trace.config.negotiation.active_archivers
                            if trace.config.negotiation
                            else []
                        )
                        for record in trace.archive_files(index, archiver)
                    ]
                ),
                "has_screenshot": bool(_has_screenshot(entry)),
                "gap": restoration_gap(_json(record)),
                "comparison": self._comparison(trace, record),
            }
        except (TraceSchemaError, KeyError) as exc:
            raise RepositoryError(str(exc)) from exc

    def screenshot(
        self, key: str, index: int, *, view: Literal["browser", "agent"] = "browser"
    ) -> bytes | None:
        try:
            trace = self._trace(key)
            if view == "browser":
                return trace.screenshot(index)
            if view != "agent":
                raise RepositoryError(f"Unknown screenshot view: {view}")
            trace.require_recording("observations", consumer="agent observation viewing")
            entry = next((entry for entry in trace.manifest.steps if entry.step == index), None)
            if entry is not None:
                for name, files in entry.fragments.items():
                    if (
                        name.rsplit(".", 1)[-1] == "screenshot"
                        and files.artifact
                        and not files.failed
                    ):
                        return read_model_image(trace.observation_fragment(index, name))
            return None
        except (TraceSchemaError, TypeError, ValueError) as exc:
            raise RepositoryError(str(exc)) from exc

    def compare(self, keys: list[str]) -> dict[str, Any]:
        if len(keys) < 2 or len(keys) > 8 or len(set(keys)) != len(keys):
            raise RepositoryError("Choose between two and eight distinct runs to compare")
        runs = [self.run(key) for key in keys]
        identities = set()
        for key in keys:
            identity = task_identity(self._trace(key).config)
            identities.add(
                (
                    identity.benchmark,
                    identity.subset,
                    identity.task_id or identity.task,
                    identity.start_url,
                )
            )
        configs = [_flatten(r["config"].get("resolved", {})) for r in runs]
        fields = sorted(set().union(*(c.keys() for c in configs)))
        differences = []
        for field in fields:
            values = [c.get(field, {"missing": True}) for c in configs]
            if any(value != values[0] for value in values[1:]):
                differences.append({"path": field, "values": values})
        evaluators = sorted(set().union(*(r["evaluations"].keys() for r in runs)))
        return {
            "runs": [r["summary"] for r in runs],
            "config_differences": differences,
            "same_task": len(identities) == 1,
            "evaluator_comparisons": [
                {"evaluator": name, "verdicts": [r["evaluations"].get(name) for r in runs]}
                for name in evaluators
            ],
        }

    def reports(self) -> list[dict[str, Any]]:
        reports = []
        for directory, files in self._directories():
            if "manifest.json" in files:
                if self._kind(directory / "manifest.json") != "dataset":
                    continue
                files = ["manifest.json"]
            names = [
                name
                for name in files
                if name in {"report.json", "diagnostics.json", "manifest.json"}
                or (directory.name == ".sessions" and name.endswith(".json"))
            ]
            for name in sorted(names):
                path = directory / name
                kind = self._kind(path)
                if kind is not None:
                    key = path.relative_to(self.root).as_posix()
                    reports.append(
                        {"key": key, "kind": kind, "title": directory.name, "error": None}
                    )
                elif name != "manifest.json":
                    try:
                        self._read_json(path)
                        error = "Unsupported experiment report format"
                    except (RepositoryError, FileNotFoundError) as exc:
                        error = str(exc)
                    reports.append(
                        {
                            "key": path.relative_to(self.root).as_posix(),
                            "kind": "unreadable",
                            "title": directory.name,
                            "error": error,
                        }
                    )
        return reports

    def compare_groups(self, left: str, right: str, evaluator: str) -> dict[str, Any]:
        """Match two configuration cohorts without inventing verdicts or picking attempts."""
        if not left or not right or left == right or not evaluator:
            raise RepositoryError("Choose two different configuration hashes and one evaluator")
        grouped: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
        signatures = set()
        found = set()
        for row in self.list_runs():
            group = row.get("config_hash")
            if row.get("read_error") or group not in {left, right}:
                continue
            found.add(group)
            trace = self._trace(row["key"])
            identity = task_identity(trace.config)
            key = (
                identity.benchmark,
                identity.subset,
                identity.task_id or identity.task,
                identity.start_url,
            )
            record = grouped.setdefault(
                key,
                {
                    "task_id": identity.task_id or identity.task,
                    "benchmark": identity.benchmark,
                    "subset": identity.subset,
                    "left": [],
                    "right": [],
                },
            )
            verdict = _evaluations(trace)[0].get(evaluator)
            if verdict is not None and verdict.verdict_source == "graded":
                signatures.add(json.dumps(verdict.evaluator_config, sort_keys=True))
            record["left" if group == left else "right"].append(row)
        if found != {left, right}:
            raise RepositoryError(
                "Both configuration hashes must have readable runs in this directory"
            )
        counts = dict.fromkeys(
            (
                "both_passed",
                "left_only_passed",
                "right_only_passed",
                "both_failed",
                "unpaired",
                "ungraded",
                "ambiguous",
                "incompatible_evaluator",
            ),
            0,
        )
        rows = []
        for _, row in sorted(grouped.items(), key=lambda item: str(item[0])):
            lhs, rhs = row["left"], row["right"]
            if not lhs or not rhs:
                outcome = "unpaired"
            elif len(lhs) != 1 or len(rhs) != 1:
                outcome = "ambiguous"
            else:
                verdicts = [
                    next(
                        (
                            v
                            for v in side[0]["verdicts"]
                            if v["evaluator"] == evaluator and v["verdict_source"] == "graded"
                        ),
                        None,
                    )
                    for side in (lhs, rhs)
                ]
                if any(verdict is None for verdict in verdicts):
                    outcome = "ungraded"
                elif len(signatures) > 1:
                    outcome = "incompatible_evaluator"
                else:
                    a, b = [
                        verdict["predicted_label"] == 1
                        for verdict in verdicts
                        if verdict is not None
                    ]
                    outcome = (
                        "both_passed"
                        if a and b
                        else "left_only_passed"
                        if a
                        else "right_only_passed"
                        if b
                        else "both_failed"
                    )
            row["outcome"] = outcome
            counts[outcome] += 1
            rows.append(row)
        return {
            "left": left,
            "right": right,
            "evaluator": evaluator,
            "counts": counts,
            "rows": rows,
        }

    def report(self, key: str) -> dict[str, Any]:
        path = self._path(key)
        # Reports are a small enumerated surface, not an arbitrary file API.
        record = next((report for report in self.reports() if report["key"] == key), None)
        if record is None:
            raise FileNotFoundError(f"No experiment report at {key}")
        if record["error"]:
            raise RepositoryError(record["error"])
        return {"key": key, "kind": self._kind(path), "data": self._read_json(path)}


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        return {
            key: item
            for name, child in sorted(value.items())
            for key, item in _flatten(child, f"{path}.{name}" if path else name).items()
        }
    if isinstance(value, list) and value:
        return {
            key: item
            for index, child in enumerate(value)
            for key, item in _flatten(child, f"{path}[{index}]").items()
        }
    return {path: value}
