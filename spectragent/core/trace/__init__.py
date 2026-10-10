"""RunTrace persistence — a WAL-style run directory and its typed reader.

``WALWriter`` is the pure I/O engine and ``RunTrace`` the lazy,
manifest-driven reader; :mod:`~spectragent.core.trace.encoding` is the one
rule for what lands where, shared by both and by the test fixtures.

See docs/architecture_design.md §5.
"""

from spectragent.core.trace.encoding import (
    EncodedFragment,
    EncodedLLMCall,
    PendingLLMCall,
    TraceFile,
    encode_archive_file,
    encode_llm_call,
    encode_observation,
)
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    ArchiveMeta,
    ArchiveRecord,
    CallFailure,
    ExtraFileIndex,
    ExtraFileRecord,
    FragmentFiles,
    OutputFormatRef,
    RecordedCompletion,
    RecordedLLMCall,
    RunStatus,
    StepManifestEntry,
    TaskSetupRecord,
    TraceConfig,
    TraceManifest,
    TraceNegotiation,
    TrailingEntry,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.redaction import OMITTED, TraceSafe, redact_config
from spectragent.core.trace.wal import WALWriter

__all__ = [
    "OMITTED",
    "TRACE_FORMAT_VERSION",
    "ArchiveMeta",
    "ArchiveRecord",
    "CallFailure",
    "EncodedFragment",
    "EncodedLLMCall",
    "ExtraFileIndex",
    "ExtraFileRecord",
    "FragmentFiles",
    "OutputFormatRef",
    "PendingLLMCall",
    "RecordedCompletion",
    "RecordedLLMCall",
    "RunStatus",
    "RunTrace",
    "StepManifestEntry",
    "TaskSetupRecord",
    "TraceConfig",
    "TraceFile",
    "TraceManifest",
    "TraceNegotiation",
    "TraceSafe",
    "TraceSchemaError",
    "TrailingEntry",
    "WALWriter",
    "atomic_write",
    "encode_archive_file",
    "encode_llm_call",
    "encode_observation",
    "redact_config",
]
