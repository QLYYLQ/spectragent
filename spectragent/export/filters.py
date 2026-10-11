"""Which recorded runs an export reads, and which it leaves out (arch §4.15 "源级检查与筛选").

Two kinds of leaving out, both reported in the dataset manifest with the
reason, neither an error:

- **skipped** — a run the export cannot read as a sample source: not a
  readable trace (named by the file that does not read back, relative to the
  run: the reader's message carries absolute paths, so it goes to the log and
  never into the manifest), a format-1 trace (no per-step fragment index),
  recorded with ``record_secrets: true`` (raw credentials never enter a
  dataset), no recorded negotiation (an assembly made by hand), or a
  ``run_id`` another source shares (all sharers are skipped, as
  ``trace_branches`` does);
- **filtered** — a readable run the ``filters`` exclude: ``require_status``,
  ``require_eval`` (a branch run is judged by its source run's verdicts),
  ``exclude_restoration_diverged`` (a restoration check that did not pass:
  ``warn`` — the branch went on regardless, so its context would be a prefix
  it did not decide on — or ``diverged``, which ``strict`` ended),
  ``max_document_misses`` (the replay log's main-frame document misses,
  ``replay_log.document_miss_total``) and ``exclude_sites``. A branch run
  whose source run is not among the readable sources is filtered too: its
  context is the source run's prefix.

Reads only through :class:`RunTrace`: what was committed is all there is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from spectragent.core.capabilities.browser.replay.replay_log import document_miss_total
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import match_runs
from spectragent.export.settings import ExportFilters

logger = logging.getLogger(__name__)

#: The trace format with the per-step fragment index the export reads.
_INDEXED_FORMAT = 2


@dataclass(frozen=True)
class Source:
    """One readable recorded run.

    Attributes:
        rel: Its directory relative to the sources root (POSIX) — how the
            manifest names it.
        trace: The run, read through its manifest.
        config: Its configuration snapshot.
    """

    rel: str
    trace: RunTrace
    config: TraceConfig

    @property
    def run_id(self) -> str:
        return self.trace.run_id

    @property
    def is_branch(self) -> bool:
        return self.config.branch is not None


@dataclass(frozen=True)
class TaskIdentity:
    """The task a run was recorded on — a branch run's is its source task's."""

    benchmark: str
    subset: str
    task_id: str
    task: str
    start_url: str | None

    @property
    def key(self) -> str:
        """``<benchmark>/<subset>/<task_id>`` — the ``split.by: task`` key."""
        return f"{self.benchmark}/{self.subset}/{self.task_id}"

    @property
    def site(self) -> str | None:
        """The start URL's host — the ``split.by: site`` key — or ``None`` without one."""
        if not self.start_url:
            return None
        host = urlsplit(self.start_url).hostname
        return host.lower() if host else None


def task_identity(config: TraceConfig) -> TaskIdentity:
    """The task *config*'s run was recorded on; for a branch run, its source task.

    The start URL is the recorded task setup's (M28: value-level redaction
    leaves the host alone); a branch run's own setup context is its source
    task's, re-resolved.
    """
    setup = config.task_setup
    start_url = setup.context.get("start_url") if setup is not None else None
    branch = config.branch
    if branch is not None:
        return TaskIdentity(
            benchmark=branch.source_benchmark,
            subset=branch.source_subset,
            task_id=branch.source_task_id,
            task=config.task,
            start_url=start_url if isinstance(start_url, str) else None,
        )
    return TaskIdentity(
        benchmark=config.benchmark,
        subset=config.subset,
        task_id=config.task_id,
        task=config.task,
        start_url=start_url if isinstance(start_url, str) else None,
    )


def open_sources(root: Path, patterns: Sequence[str]) -> tuple[list[Source], dict[str, str]]:
    """Every matched run the export can read, by path, and why each other one cannot.

    Returns:
        ``(sources, skipped)`` — *skipped* maps a run's relative path to the
        reason it is not a sample source.
    """
    readable: list[Source] = []
    skipped: dict[str, str] = {}
    for rel, directory in match_runs(root, patterns):
        try:
            trace = RunTrace(directory)
            if trace.manifest.format_version < _INDEXED_FORMAT:
                skipped[rel] = (
                    f"format {trace.manifest.format_version} trace: the export reads the "
                    f"format-{_INDEXED_FORMAT} per-step fragment index — re-record the run"
                )
                continue
            config = trace.config
        except TraceSchemaError as exc:
            logger.warning("export: skipping %s, it does not read back: %s", rel, exc)
            skipped[rel] = unreadable_reason(directory, exc)
            continue
        if config.secrets_recorded:
            skipped[rel] = (
                "recorded with record_secrets: true — a trace holding raw credentials never "
                "enters a dataset"
            )
            continue
        if config.negotiation is None:
            skipped[rel] = "no recorded negotiation (an assembly made by hand)"
            continue
        readable.append(Source(rel=rel, trace=trace, config=config))

    owners: dict[str, list[str]] = {}
    for source in readable:
        owners.setdefault(source.run_id, []).append(source.rel)
    sources: list[Source] = []
    for source in readable:
        shared = [other for other in owners[source.run_id] if other != source.rel]
        if shared:
            skipped[source.rel] = (
                f"its run id {source.run_id!r} is also the run id of {', '.join(shared)}: "
                f"their sample ids would collide"
            )
            continue
        sources.append(source)
    return sources, skipped


def unreadable_reason(directory: Path, exc: TraceSchemaError) -> str:
    """Why the run in *directory* is skipped, worded without the reader's message.

    ``TraceSchemaError`` names files by absolute path (and may quote the bytes
    that did not parse); the dataset manifest holds no absolute path and is
    the same whichever root the sources sit under (arch §4.15 "数据集
    manifest"). So the reason names the file relative to the run directory and
    the error class — callers log the message itself.
    """
    where = ""
    path = exc.path
    if path is not None and path.is_relative_to(directory):
        file = path.relative_to(directory).as_posix()
        if file != ".":
            where = f": {file} does not read back"
    return f"not a readable trace{where} ({type(exc).__name__})"


def filtered_out(
    source: Source, filters: ExportFilters, by_run_id: Mapping[str, Source]
) -> str | None:
    """Why *filters* leave *source* out, or ``None`` when it is kept.

    *by_run_id* is every readable source, by run id: where a branch run's
    source run is found.

    Raises:
        TraceSchemaError: Something the filters must read does not read back
            (a damaged trace is an error, not a filtered source).
    """
    branch = source.config.branch
    origin = source
    if branch is not None:
        found = by_run_id.get(branch.source_run_id)
        if found is None:
            return f"branch source {branch.source_run_id!r} not among the sources"
        origin = found
    status = source.trace.manifest.status
    if filters.require_status != "any" and status != RunStatus(filters.require_status):
        return f"status {status.value} (require_status: {filters.require_status})"
    need = filters.require_eval
    if need is not None and not need.satisfied_by(origin.trace.evals()):
        whose = f"its source run {origin.rel}" if origin is not source else "it"
        pinned = f", slug {need.slug!r}" if need.slug is not None else ""
        return (
            f"{whose} has no graded verdict from {need.evaluator_id!r} with predicted_label "
            f"{need.label}{pinned} (require_eval)"
        )
    if filters.exclude_restoration_diverged:
        # Any check that did not pass: ``warn`` (the branch went on from a
        # state that is not the recorded one) as much as ``diverged`` (strict).
        diverged = [
            f"{record.step} ({record.restoration.verdict}: {', '.join(record.restoration.reasons)})"
            for record in source.trace.step_records
            if record.restoration is not None and record.restoration.verdict != "ok"
        ]
        if diverged:
            return (
                f"restoration diverged at step(s) {', '.join(diverged)} "
                f"(exclude_restoration_diverged)"
            )
    limit = filters.max_document_misses
    if limit is not None:
        misses = document_miss_total(source.trace)
        if misses > limit:
            return f"{misses} main-frame document miss(es) in replay (max_document_misses: {limit})"
    site = task_identity(source.config).site
    if site is not None:
        for entry in filters.exclude_sites:
            excluded = entry.lower()
            if site == excluded or site.endswith("." + excluded):
                return f"site {site} is excluded ({entry!r} in exclude_sites)"
    return None


__all__ = [
    "Source",
    "TaskIdentity",
    "filtered_out",
    "open_sources",
    "task_identity",
    "unreadable_reason",
]
