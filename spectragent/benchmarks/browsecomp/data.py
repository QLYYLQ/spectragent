"""BrowseComp's data, pinned by its file's SHA-256 (``docs/architecture_design.md`` §4.10.1 "数据").

OpenAI publishes BrowseComp as one CSV file of 1266 questions at a URL, with
no version of its own, so the pin is the file's SHA-256; a fetch refuses a file
that differs. Each row holds the question and its answer encrypted with the
row's canary (``problem``, ``answer``, ``canary``) and a ``problem_topic``. A
fetch writes them as the task loader reads them, in ``tasks.jsonl``, one task
``browsecomp_<row>`` (the row number from 0, at least three digits) per line:
the encrypted pair under ``reference`` (``encrypted_question``,
``encrypted_answer``) and the canary under ``metadata``, still encrypted as
OpenAI asks; ``data_info.json`` names the subset ``All``.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from spectragent.benchmarks.data import Say, download_file
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.schemas.data_version import DataVersion

#: OpenAI's file, pinned by its SHA-256 as downloaded on 2026-11-04 (last modified 2025-04-10).
DATA = DataVersion(
    source="url",
    repository="https://openaipublic.blob.core.windows.net/simple-evals/browse_comp_test_set.csv",
    revision="7b24471cd5b3eb2a46830a14802b5c029ea62f488ff75a0f88af7923d1454abf",
)
#: The file the task loader reads the tasks from.
TASKS_FILE = "tasks.jsonl"
_COLUMNS = ("problem", "answer", "canary")


class BrowseCompData:
    """BrowseComp's questions at :data:`DATA`, fetched from OpenAI's file."""

    @property
    def version(self) -> DataVersion:
        return DATA

    def fetch(self, into: Path, say: Say) -> None:
        downloaded = download_file(DATA.repository, DATA.revision, into / "download.csv", say)
        lines = tasks_jsonl(downloaded.read_text(encoding="utf-8"))
        downloaded.unlink()
        (into / TASKS_FILE).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        (into / "data_info.json").write_text(
            json.dumps({"split": {"All": TASKS_FILE}}, indent=2) + "\n", encoding="utf-8"
        )
        say(f"wrote {len(lines)} tasks")


def tasks_jsonl(text: str) -> list[str]:
    """The task loader's lines for the CSV *text*, one per row.

    Raises:
        BenchmarkDataError: A column the tasks need is missing, or there is no row.
    """
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        raise BenchmarkDataError(f"{DATA.repository} holds no question")
    missing = [column for column in _COLUMNS if column not in rows[0]]
    if missing:
        raise BenchmarkDataError(
            f"{DATA.repository} has no column {', '.join(missing)} (columns: {sorted(rows[0])})"
        )
    return [
        json.dumps(
            {
                "task_id": f"browsecomp_{index:03d}",
                "benchmark": "browsecomp",
                "env_type": "browser",
                "reference": {
                    "encrypted_question": row["problem"],
                    "encrypted_answer": row["answer"],
                },
                "metadata": {"canary": row["canary"]},
            }
        )
        for index, row in enumerate(rows)
    ]


__all__ = ["DATA", "BrowseCompData", "tasks_jsonl"]
