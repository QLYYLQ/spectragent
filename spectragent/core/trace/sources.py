"""Which recorded runs a sources root and its globs name (arch §4.10, §4.15).

The one rule two offline consumers share: the ``trace_branches`` task loader
(branch tasks from recorded runs) and the training export (samples from
recorded runs) both take a sources root and a list of globs relative to it.
A match is a run when it is a directory holding a ``manifest.json``; the
matches come back sorted by their POSIX path relative to the root, so the
order never depends on the filesystem's.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from spectragent.core.trace.encoding import MANIFEST_FILE

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


__all__ = ["match_runs", "source_pattern_problem"]
