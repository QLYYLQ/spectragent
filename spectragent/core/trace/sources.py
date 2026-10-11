"""Which recorded runs a sources root and its globs name, and which task each was (arch §4.10, §4.15, §4.16).

The rules the offline consumers share: the ``trace_branches`` task loader
(branch tasks from recorded runs), the training export (samples from recorded
runs) and the flywheel metrics (a report over a chain's runs) all take a root
and globs relative to it. A match is a run when it is a directory holding a
``manifest.json``; the matches come back sorted by their POSIX path relative
to the root, so the order never depends on the filesystem's.

The export and the metrics also share what "the task" and "the site" of a run
are (:func:`task_identity`: a branch run's are its source task's) and how a
run that does not read back is named (:func:`unreadable_reason`), so the
dataset's ``split.by: site`` / ``exclude_sites`` and the report's per-site
statistics use one key.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from spectragent.core.trace.encoding import MANIFEST_FILE
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig

logger = logging.getLogger(__name__)


def source_pattern_problem(pattern: str) -> str | None:
    """Why *pattern* cannot name runs under a sources root, or ``None`` when it can.

    A pattern is relative to the root and stays inside it: not empty, not
    absolute, no ``~``, no ``..``.
    """
    if not pattern:
        return "is empty"
    path = PurePosixPath(pattern)
    if path.is_absolute() or pattern.startswith("~"):
        return "must be relative to the sources root"
    if ".." in path.parts:
        return "must stay inside the sources root (no '..')"
    return None


def match_runs(root: Path, patterns: Sequence[str]) -> list[tuple[str, Path]]:
    """``(relative POSIX path, directory)`` of every run directory a glob matches, by path.

    A match without a ``manifest.json`` is not a run and is ignored (logged
    at DEBUG); a run two globs both match is listed once.
    """
    found: dict[str, Path] = {}
    for pattern in patterns:
        for match in root.glob(pattern):
            if match.is_dir() and (match / MANIFEST_FILE).is_file():
                found.setdefault(match.relative_to(root).as_posix(), match)
            else:
                logger.debug("%s is not a recorded run; ignored", match)
    return sorted(found.items())


def unreadable_reason(directory: Path, exc: TraceSchemaError) -> str:
    """Why the run in *directory* is left out, worded without the reader's message.

    ``TraceSchemaError`` names files by absolute path (and may quote the bytes
    that did not parse); a dataset manifest or a metrics report holds no
    absolute path and is the same whichever root the runs sit under (arch
    §4.15 "数据集 manifest", §4.16 "报告"). So the reason names the file
    relative to the run directory and the error class — callers log the
    message itself.
    """
    where = ""
    path = exc.path
    if path is not None and path.is_relative_to(directory):
        file = path.relative_to(directory).as_posix()
        if file != ".":
            where = f": {file} does not read back"
    return f"not a readable trace{where} ({type(exc).__name__})"


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


__all__ = [
    "TaskIdentity",
    "match_runs",
    "source_pattern_problem",
    "task_identity",
    "unreadable_reason",
]
