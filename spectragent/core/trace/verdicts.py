"""The verdict store: every verdict on a run, one file each, beside its records.

``<run>/verdicts/<slug>.json`` holds one :class:`~spectragent.schemas.verdict.Verdict`
per verifier configuration, whichever attachment produced it: the scoring
point's, written as the run closes out, and the offline ones, written any time
after (``docs/architecture_design.md`` §5.3 "判定统一存放"). It is outside the
manifest, so writing a verdict never changes a committed record. Each write is
atomic (``fsio.atomic_write``): a reader sees the previous verdict of that slug
or the new one, never part of one, and judging a run again with the same
settings replaces its verdict. ``RunTrace.verdicts`` reads them back.
"""

from __future__ import annotations

from pathlib import Path

from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import verdict_path
from spectragent.schemas.verdict import Verdict


def write_verdict(run_dir: Path, verdict: Verdict) -> Path:
    """Write *verdict* into *run_dir*'s store, replacing its slug's previous one; return its path.

    Raises:
        ValueError: The verdict's slug is not one plain path segment.
        OSError: Writing or ``fsync``-ing failed; the previous verdict, if
            any, is still there.
    """
    path = run_dir / verdict_path(verdict.slug)
    fsio.atomic_write(path, verdict.model_dump_json(indent=2).encode("utf-8"))
    return path


__all__ = ["write_verdict"]
