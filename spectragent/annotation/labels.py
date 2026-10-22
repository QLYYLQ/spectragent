"""Pure label rules: the verdict against a suggestion, image bounds, and current labels."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from spectragent.annotation.models import LabelLine, Region, Suggestion, Verdict


def compute_verdict(
    suggestion: Suggestion | None,
    *,
    choice: str | None,
    choices: list[str] | None,
    answers: dict[str, str] | None,
    regions: list[Region] | None,
    skipped: bool,
    reject: bool,
) -> Verdict | None:
    """How a label relates to its item's suggestion (``docs/architecture_design.md`` §4.17).

    ``None`` without a suggestion that gives an answer, or for a skipped
    item. ``rejected`` when the person marked the suggestion wrong without a
    replacement. Otherwise only the answer fields the suggestion sets are
    compared — each suggested question's answer, regions as a multiset of
    (image, kind, box), the note never — and equal is ``accepted``,
    different ``corrected``.
    """
    if suggestion is None or not suggestion.has_answer or skipped:
        return None
    if reject:
        return "rejected"
    same = True
    if suggestion.choice is not None:
        same &= choice == suggestion.choice
    if suggestion.choices is not None:
        same &= set(choices or []) == set(suggestion.choices)
    if suggestion.answers is not None:
        given = answers or {}
        same &= all(given.get(key) == value for key, value in suggestion.answers.items())
    if suggestion.regions is not None:
        same &= Counter(region.key() for region in regions or []) == Counter(
            region.key() for region in suggestion.regions
        )
    return "accepted" if same else "corrected"


def region_bounds_problems(
    regions: Iterable[Region], sizes: Mapping[str, tuple[int, int]]
) -> list[str]:
    """Regions that leave their image; *sizes* maps image role to (width, height) pixels."""
    problems = []
    for region in regions:
        size = sizes.get(region.image)
        if size is None or size[0] <= 0 or size[1] <= 0:
            problems.append(f"不知道图片 {region.image!r} 的尺寸")
            continue
        width, height = size
        x0, y0, x1, y1 = region.box
        if x1 > width or y1 > height:
            problems.append(f"框 {region.box} 超出了图片 {region.image!r}（{width}x{height} 像素）")
    return problems


@dataclass(frozen=True)
class LabelError:
    """A label line that cannot be read, by its 1-based line number."""

    line: int
    error: str


@dataclass(frozen=True)
class LabelLog:
    """Every readable line of one label file, in file order, plus unreadable ones."""

    lines: tuple[LabelLine, ...]
    errors: tuple[LabelError, ...]

    def current(self) -> dict[tuple[str, str], LabelLine]:
        """The latest line per (target, annotator): file order decides, never the clocks.

        The target is the item id for a queue, ``"<run>#<step>"`` for ad-hoc labels.
        """
        current: dict[tuple[str, str], LabelLine] = {}
        for line in self.lines:
            current[(target(line), line.annotator)] = line
        return current

    def history(self, key: str) -> list[LabelLine]:
        return [line for line in self.lines if target(line) == key]


def target(line: LabelLine) -> str:
    if line.item_id is not None:
        return line.item_id
    return step_target(line.run or "", line.step if line.step is not None else -1)


def step_target(run: str, step: int) -> str:
    return f"{run}#{step}"
