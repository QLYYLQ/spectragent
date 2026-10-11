"""Restorers: how a branch run puts the environment back at its checkpoint.

``RestorerRegistrar`` (``restorer_id``) and its v1 strategy ``prefix_replay``;
the narrow context a restorer is handed; typed access to the source run; and
the verdict rules every restorer applies. See ``docs/architecture_design.md``
§4.1 "`BranchLoop`" and §5.3 ``RestorationCheck``.
"""

from spectragent.core.loops.restore._registrar import (
    Restorer,
    RestorerBase,
    RestorerRegistrar,
    default_restorer,
)
from spectragent.core.loops.restore.checks import judge
from spectragent.core.loops.restore.context import (
    RestorationContext,
    RestorationDivergedError,
    RestoredStep,
)
from spectragent.core.loops.restore.prefix_replay import PrefixReplayRestorer
from spectragent.core.loops.restore.source import SourceRun

__all__ = [
    "PrefixReplayRestorer",
    "RestorationContext",
    "RestorationDivergedError",
    "RestoredStep",
    "Restorer",
    "RestorerBase",
    "RestorerRegistrar",
    "SourceRun",
    "default_restorer",
    "judge",
]
