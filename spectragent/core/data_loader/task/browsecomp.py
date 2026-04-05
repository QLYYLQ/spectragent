"""BrowseComp benchmark task loader."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from spectragent.core.data_loader.task._registrar import TaskLoaderBase


@dataclass
class BrowseCompTaskLoader(TaskLoaderBase):
    """Loads BrowseComp benchmark tasks (encrypted questions/answers)."""

    __registry_key__ = "browsecomp"

    subset: Annotated[
        Literal["All"],
        Field("All", description="BrowseComp data subset"),
    ] = "All"
