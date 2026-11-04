"""LexBench-Browser's data, pinned on Hugging Face (``docs/architecture_design.md`` §4.10.1 "数据").

The public dataset ``Lexmount/LexBench-Browser`` (v1.0, 210 tasks on real
sites, no login), pinned at a commit. Its files sit under
``LexBench-Browser/``; a fetch downloads each subset's file as it is, checked
against the dataset's record of it at that commit, since the task and eval
loaders read the dataset's own record shape (``core/data_loader/_lexbench_legacy.py``),
and writes ``data_info.json`` naming the subsets. The subsets are the
dataset's own, those its ``data_info.json`` names with a file at the pinned
commit: ``All`` (``task.jsonl``), ``global`` and ``lexmount`` (its
``website_region`` ``en`` and ``zh`` tasks, which split ``All`` in two) and
``sample50`` (50 of ``All``, each marked ``only_use``). Its ``data_info.json``
also names ``task_all``, which has no file there, so it is no subset here.
"""

from __future__ import annotations

import json
from pathlib import Path

from spectragent.benchmarks.data import Say, download_huggingface_file
from spectragent.schemas.data_version import DataVersion

#: The dataset, at the commit this benchmark reads.
DATA = DataVersion(
    source="huggingface",
    repository="Lexmount/LexBench-Browser",
    revision="bac3f83bfcc87dc0ca298c82ddf91e4aefba5bab",
)
#: The folder of the dataset holding its files.
FOLDER = "LexBench-Browser"
#: Each subset, and the dataset's file of its tasks.
SUBSETS = {
    "All": "task.jsonl",
    "global": "task_global.jsonl",
    "lexmount": "task_lexmount.jsonl",
    "sample50": "task_sample50.jsonl",
}


class LexBenchBrowserData:
    """LexBench-Browser's tasks at :data:`DATA`, fetched from Hugging Face."""

    @property
    def version(self) -> DataVersion:
        return DATA

    def fetch(self, into: Path, say: Say) -> None:
        for name in SUBSETS.values():
            download_huggingface_file(DATA.repository, DATA.revision, f"{FOLDER}/{name}", into, say)
        (into / "data_info.json").write_text(
            json.dumps({"split": SUBSETS}, indent=2) + "\n", encoding="utf-8"
        )
        say(f"wrote the subsets {', '.join(SUBSETS)}")


__all__ = ["DATA", "FOLDER", "SUBSETS", "LexBenchBrowserData"]
