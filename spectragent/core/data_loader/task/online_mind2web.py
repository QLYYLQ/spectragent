"""Online-Mind2Web benchmark task loader."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from spectragent.core.data_loader.task._registrar import TaskLoaderBase


@dataclass
class OnlineMind2WebTaskLoader(TaskLoaderBase):
    """Loads Online-Mind2Web benchmark tasks."""

    __registry_key__ = "online_mind2web"

    subset: Annotated[
        Literal["All", "Hard"],
        Field("All", description="Online-Mind2Web data subset"),
    ] = "All"
