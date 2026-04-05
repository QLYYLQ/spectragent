"""LexBench-Browser benchmark task loader."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from spectragent.core.data_loader.task._registrar import TaskLoaderBase


@dataclass
class LexBenchBrowserTaskLoader(TaskLoaderBase):
    """Loads LexBench-Browser benchmark tasks."""

    __registry_key__ = "lexbench_browser"

    subset: Annotated[
        Literal[
            "All", "L1", "L2", "L3-api", "L3-security",
            "omw", "omw-20pct", "omw-80pct",
        ],
        Field("All", description="LexBench data subset"),
    ] = "All"
