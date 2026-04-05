"""LexBench-Browser benchmark eval loader."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from spectragent.core.data_loader.eval._registrar import EvalLoaderBase


@dataclass
class LexBenchBrowserEvalLoader(EvalLoaderBase):
    """Loads LexBench-Browser evaluation reference data."""

    __registry_key__ = "lexbench_browser"

    subset: Annotated[
        Literal[
            "All", "L1", "L2", "L3-api", "L3-security",
            "omw", "omw-20pct", "omw-80pct",
        ],
        Field("All", description="LexBench data subset"),
    ] = "All"
