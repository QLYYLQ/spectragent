"""Environment evidence — the archive axis (M28, framework layer).

The third provides axis after observation and action
(``docs/architecture_design.md`` §4.5): archivers are registered one class
each under ``<capability>.<id>`` keys, a capability declares the ones it
provides in ``__wiring__["archiver"]``, the trace hook's ``archive`` list
says which ones a run records, negotiation binds the two, and the trace
hook pulls the evidence at its own commit points.

This package holds the capability-agnostic half: the registrar and base
(:mod:`._registrar`), the value types a collection hands over
(:mod:`.types`), the two Protocols either side of the pull
(:mod:`.protocols`) and the shared privacy rules (:mod:`.privacy`). It
imports nothing from ``core/trace``, ``core/hooks``, ``core/negotiation``,
``core/environments`` or ``core/capabilities`` — they depend on it, never
the other way round.

No concrete archiver lives here or anywhere yet: the first ones are the
browser's (M29), which is also when ``ArchiverRegistrar`` joins
``_collect_registrars()`` and the trace hook's ``archive`` becomes a YAML
receptor — an empty registry cannot be a Composer layer.
"""

from spectragent.core.archive._registrar import (
    ARCHIVER_KEY_SEPARATOR,
    Archiver,
    ArchiverBase,
    ArchiverRegistrar,
)
from spectragent.core.archive.protocols import ArchiveConsumer, ArchivingCapability
from spectragent.core.archive.types import (
    MAX_SEGMENT_BYTES,
    RUN_RECORD_MEDIA_TYPE,
    ArchiveBatch,
    ArchiveContractError,
    ArchiveFile,
    ArchiveProvenance,
    ArchiveScope,
    segment_problem,
    text_problem,
)

__all__ = [
    "ARCHIVER_KEY_SEPARATOR",
    "MAX_SEGMENT_BYTES",
    "RUN_RECORD_MEDIA_TYPE",
    "ArchiveBatch",
    "ArchiveConsumer",
    "ArchiveContractError",
    "ArchiveFile",
    "ArchiveProvenance",
    "ArchiveScope",
    "Archiver",
    "ArchiverBase",
    "ArchiverRegistrar",
    "ArchivingCapability",
    "segment_problem",
    "text_problem",
]
