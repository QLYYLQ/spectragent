"""Screenshot hygiene for trajectory judges.

A browser trajectory is full of frames that carry no signal: an
all-white page mid-navigation, three identical frames while a modal
animates. Feeding them to a vision judge costs money and dilutes the
evidence, so the LexBench grader filtered them before scoring — that
filtering changes which frames the judge sees, so it is scoring logic and
is ported here rather than dropped.

The original used numpy for the statistics and MD5 for the identity.
Both are reproduced with Pillow alone (``ImageStat`` / ``histogram``) and
SHA-256, which keeps the dependency footprint at what the package already
declares.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenshotFilterStats:
    """Why each recorded frame did or did not reach the judge.

    Recorded into ``benchmark_details``, which makes every field here a
    claim about the *recording* that someone auditing an eval result will
    believe. ``blank``, ``duplicate`` and ``unreadable`` are therefore
    measurements: they may only be incremented by code that actually
    inspected the pixels.

    ``elided`` is the escape hatch that keeps them honest. Frames dropped
    because the *strategy* only wants some of them — ``final`` forwards
    one, ``stepwise`` caps at ``max_screenshots`` — were never examined
    and are not defects. Folding them into ``duplicate`` (which is what
    the ``final`` branch used to do) reports a clean 10-frame recording
    as nine duplicates, and makes the same key mean different things
    under different strategies.
    """

    total: int = 0
    blank: int = 0
    duplicate: int = 0
    unreadable: int = 0
    elided: int = 0

    @property
    def kept(self) -> int:
        """Frames that actually reached the judge."""
        return (
            self.total
            - self.blank
            - self.duplicate
            - self.unreadable
            - self.elided
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "blank": self.blank,
            "duplicate": self.duplicate,
            "unreadable": self.unreadable,
            "elided": self.elided,
            "kept": self.kept,
        }


def clean_screenshots(
    paths: list[Path],
    *,
    drop_blank: bool = True,
    drop_duplicates: bool = True,
    blank_stddev: float = 5.0,
    mono_ratio: float = 0.99,
) -> tuple[list[Path], ScreenshotFilterStats]:
    """Drop blank and byte-identical frames, preserving order."""
    if not paths or not (drop_blank or drop_duplicates):
        return list(paths), ScreenshotFilterStats(total=len(paths))

    from PIL import Image, ImageStat

    kept: list[Path] = []
    seen: set[str] = set()
    blank = duplicate = unreadable = 0

    for path in paths:
        try:
            with Image.open(path) as img:
                grey = img.convert("L")
                if drop_blank and _is_blank(
                    ImageStat.Stat(grey).stddev,
                    grey.histogram(),
                    blank_stddev,
                    mono_ratio,
                ):
                    blank += 1
                    continue
                if drop_duplicates:
                    digest = hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()
                    if digest in seen:
                        duplicate += 1
                        continue
                    seen.add(digest)
        except OSError as exc:
            # A truncated PNG is a recording defect, not a verdict. Skip
            # the frame and count it so the eval result says so.
            logger.warning("Screenshot %s is unreadable (%s); skipping", path, exc)
            unreadable += 1
            continue
        kept.append(path)

    return kept, ScreenshotFilterStats(
        total=len(paths), blank=blank, duplicate=duplicate, unreadable=unreadable
    )


def _is_blank(
    stddev: list[float],
    histogram: list[int],
    blank_stddev: float,
    mono_ratio: float,
) -> bool:
    if stddev and stddev[0] < blank_stddev:
        return True
    total = sum(histogram)
    return bool(total) and (max(histogram) / total) > mono_ratio


def sample_evenly(paths: list[Path], limit: int) -> list[Path]:
    """Keep at most *limit* frames, spread across the trajectory.

    Always retains the first and last frame: the first shows the starting
    state, the last is where the answer lives.
    """
    count = len(paths)
    if limit <= 0 or count <= limit:
        return list(paths)
    if limit == 1:
        return [paths[-1]]
    indices = sorted({int(i * (count - 1) / (limit - 1)) for i in range(limit)})
    return [paths[i] for i in indices]
