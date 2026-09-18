"""RunTrace WAL persistence — M10.

WAL-style per-step persistence with atomic manifest commits.
WALWriter is the pure I/O engine; RunTrace is the lazy-loading reader.

See docs/architecture_design.md §4.6 and docs/execution_dag.md M10.
"""

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import (
    RunStatus,
    StepManifestEntry,
    TraceConfig,
    TraceManifest,
    TraceNegotiation,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import OMITTED, TraceSafe, redact_config
from spectragent.core.trace.wal import WALWriter

__all__ = [
    "OMITTED",
    "RunStatus",
    "RunTrace",
    "StepManifestEntry",
    "TraceConfig",
    "TraceManifest",
    "TraceNegotiation",
    "TraceSafe",
    "TraceSchemaError",
    "WALWriter",
    "redact_config",
]
