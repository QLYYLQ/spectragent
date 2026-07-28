"""LexBench-Browser benchmark task loader."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field

from spectragent.core.data_loader._lexbench_legacy import normalize_legacy_record
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.schemas.task import TaskDefinition


@dataclass
class LexBenchBrowserTaskLoader(TaskLoaderBase):
    """Loads LexBench-Browser benchmark tasks."""

    __registry_key__ = "lexbench_browser"

    subset: Annotated[
        Literal[
            "All", "L1", "L2", "L3-api", "L3-security",
            "omw", "omw-20pct", "omw-80pct", "private",
        ],
        Field("All", description="LexBench data subset"),
    ] = "All"

    def _normalize_raw(self, raw: dict[str, Any]) -> TaskDefinition:
        """Accept both the normalized and the upstream record shape.

        The ``private`` split ships upstream-shaped records (integer
        ``id``, ``query``, ``target_website``, flat login flags).
        """
        return super()._normalize_raw(normalize_legacy_record(raw))
