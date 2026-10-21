"""Writing outputs whole or not at all: each file is written to a temporary name and renamed."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from spectragent.core.trace.fsio import atomic_write


def write_text(path: Path, text: str) -> None:
    """*text* as *path*, atomically (a failure leaves no partial file)."""
    atomic_write(path, text.encode("utf-8"))


def write_json(path: Path, document: Any) -> None:
    write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def write_lines(path: Path, lines: Iterable[str]) -> int:
    """One line per item, atomically; the number of lines.

    The lines are joined in memory first, so a failure while producing them
    leaves no file at all.
    """
    collected = list(lines)
    write_text(path, "".join(line + "\n" for line in collected))
    return len(collected)
