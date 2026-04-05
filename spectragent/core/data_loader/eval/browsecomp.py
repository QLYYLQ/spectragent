"""BrowseComp benchmark eval loader."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from spectragent.core.data_loader.eval._registrar import EvalLoaderBase


@dataclass
class BrowseCompEvalLoader(EvalLoaderBase):
    """Loads BrowseComp evaluation reference data."""

    __registry_key__ = "browsecomp"

    subset: Annotated[
        Literal["All"],
        Field("All", description="BrowseComp data subset"),
    ] = "All"
