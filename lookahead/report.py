"""Small Markdown helpers shared by the reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(_cell(header) for header in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines)


def crosstab(
    pairs: Iterable[tuple[str, str]],
    *,
    row_title: str,
    columns: Sequence[str] | None = None,
) -> str:
    """Counts of ``(row, column)`` pairs as a table, with row and column totals."""
    counts = Counter(pairs)
    rows = sorted({row for row, _ in counts})
    found = {column for _, column in counts}
    ordered = list(columns or [])
    ordered += sorted(found - set(ordered))
    body = []
    for row in rows:
        values = [counts[(row, column)] for column in ordered]
        body.append([row, *values, sum(values)])
    totals = [sum(counts[(row, column)] for row in rows) for column in ordered]
    body.append(["total", *totals, sum(totals)])
    return table([row_title, *ordered, "total"], body)


def share(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else f"{part}/0"


def excluded_section(excluded: dict[str, str]) -> list[str]:
    """The report lines naming the runs an exclusions file left out; none when it left out none."""
    if not excluded:
        return []
    lines = [f"## Runs left out by the exclusions file ({len(excluded)})", ""]
    lines += [f"- `{key}`: {why}" for key, why in sorted(excluded.items())]
    return [*lines, ""]
