"""Result model + table/JSON reporting for the harness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_OUT_DIR = Path(__file__).parent / "out"

# ANSI (degrade to empty if not a tty is handled by the caller if desired)
_C = {
    "PASS": "\033[32m",
    "FAIL": "\033[31m",
    "SKIP": "\033[33m",
    "reset": "\033[0m",
    "dim": "\033[2m",
}


@dataclass
class Result:
    provider: str
    registry_key: str
    model: str
    phase: str  # "text" | "structured:<label>"
    status: str  # PASS | FAIL | SKIP
    latency_ms: float | None = None
    usage: tuple[int | None, int | None, int | None] | None = None  # (prompt, completion, total)
    stop_reason: str | None = None
    error: str | None = None
    preview: str | None = None

    def usage_str(self) -> str:
        if not self.usage:
            return "-"
        p, c, t = self.usage
        return f"{p if p is not None else '?'}/{c if c is not None else '?'}/{t if t is not None else '?'}"


def _c(status: str, text: str) -> str:
    return f"{_C.get(status, '')}{text}{_C['reset']}"


def _trunc(text: str | None, n: int) -> str:
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def print_table(results: list[Result]) -> None:
    if not results:
        print("  (no results)")
        return
    rows = []
    for r in results:
        detail = r.error if r.status == "FAIL" else r.preview
        rows.append(
            (
                r.provider,
                r.model,
                r.phase,
                r.status,
                f"{r.latency_ms:.0f}" if r.latency_ms is not None else "-",
                r.usage_str(),
                _trunc(detail, 58),
            )
        )
    headers = ("PROVIDER", "MODEL", "PHASE", "STATUS", "ms", "tok p/c/t", "DETAIL")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    # cap widths for the last few columns
    widths[6] = min(widths[6], 58)

    def fmt(row: tuple[str, ...], color_status: bool) -> str:
        cells = []
        for i, cell in enumerate(row):
            text = cell.ljust(widths[i])
            if color_status and i == 3:
                text = _c(row[3], cell.ljust(widths[i]))
            cells.append(text)
        return "  ".join(cells)

    print("  " + fmt(headers, False))
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("  " + fmt(row, True))


def summarize(results: list[Result]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def print_summary(results: list[Result]) -> None:
    c = summarize(results)
    print()
    print(
        "  Summary: "
        f"{_c('PASS', str(c['PASS']) + ' PASS')}  "
        f"{_c('FAIL', str(c['FAIL']) + ' FAIL')}  "
        f"{_c('SKIP', str(c['SKIP']) + ' SKIP')}  "
        f"({len(results)} total)"
    )


def dump_json(results: list[Result], meta: dict[str, Any]) -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _OUT_DIR / f"{ts}.json"
    payload = {
        "meta": meta,
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path
