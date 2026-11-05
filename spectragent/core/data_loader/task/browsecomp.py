"""BrowseComp benchmark task loader.

**No step budget.** BrowseComp's public protocol states none, so a task hands
the Loop no ``max_steps`` and the configured ``max_steps`` applies
(``docs/architecture_design.md`` §4.10.1 "基准的步数预算"). Looked at:
simple-evals asks the model under test once per question, with no agent loop
(``browsecomp_eval.py:95-105`` at commit 652c89d0ca9df547706735883097e9537d40dc47),
and the paper reports accuracy against browsing effort rather than at a
fixed budget (arXiv:2504.12516v1, §4.3).
"""

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
