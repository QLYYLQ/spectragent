"""The two Protocols the archive axis is built on — one per side of the pull.

- :class:`ArchivingCapability` — what a capability that provides archivers
  owes the environment: what it provides, a binding, and a collection.
  Optional, and deliberately **not** part of ``BaseCapability``: a
  capability that records nothing should implement nothing, and adding
  members to a ``@runtime_checkable`` Protocol changes ``isinstance`` for
  every structural implementer. ``CapabilityBase`` satisfies it with inert
  defaults.
- :class:`ArchiveConsumer` — the one thing the runtime knows about whoever
  persists the evidence: an ``archive`` list. The runtime never learns
  that the consumer is the trace hook.

See ``docs/architecture_design.md`` §4.5 (the third provides axis), §4.6
(a recorder pulling evidence) and §4.9 (the archiving contract).

.. note::
   No ``from __future__ import annotations`` — the package rule
   (:mod:`spectragent.core.archive._registrar`).
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from spectragent.core.archive._registrar import ArchiverBase
from spectragent.core.archive.types import ArchiveBatch


@runtime_checkable
class ArchivingCapability(Protocol):
    """A capability that hands over environment evidence when pulled.

    **The archiving contract.** ``tests/_helpers/archive_contract.py`` checks,
    capability by capability, every clause that can be checked from outside;
    the three that depend on the capability's own timing — never blocking,
    the atomic hand-over, an in-flight item handed over exactly once — are
    for each capability's own tests (the browser's under fake CDP):

    - :meth:`bind_archivers` **replaces** the whole binding (an empty list
      unbinds) and acquires nothing. The runtime binds at the end of
      ``prepare()`` — possibly on a dry run that never sets up — and before
      ``setup()``, which is where the capability reads the binding to
      decide whether to build its capture channel at all.
    - :meth:`collect_archive` takes no argument — no step, no cycle — and
      hands over everything captured since the previous collection, then
      forgets it: the batch belongs to the caller. It never blocks
      indefinitely, is safe on a capability that never set up or set up
      half way (nothing captured: an empty batch), and **raises** when it
      cannot collect rather than returning a batch that looks complete.
    - The hand-over is atomic with respect to the capability's own
      producers (swap the buffer out in one synchronous statement): in a
      shared environment another agent may be acting while it runs.
    - An item still in flight at a collection boundary (a response body
      being read) goes into a later batch exactly once; the last
      collection either finishes it within bounded time or hands it over
      marked incomplete — how it is marked is the capability's business.
    - Every file carries its provenance (``ArchiveFile.meta``), its
      ``capability_id`` is this capability's own, and every step file
      belongs to a bound archiver; a batch holds at most one run record
      per capability.
    """

    @property
    def provided_archivers(self) -> frozenset[str]:
        """Fully-qualified archiver keys this capability can provide."""
        ...

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Replace the binding with *archivers* (an empty sequence unbinds).

        The instances are the trace hook's, knobs and all. Must not acquire
        any resource.
        """
        ...

    async def collect_archive(self) -> ArchiveBatch:
        """Hand over everything captured since the last collection, and forget it."""
        ...


@runtime_checkable
class ArchiveConsumer(Protocol):
    """Whoever persists environment evidence — found by the runtime through this.

    One attribute, named like the YAML receptor it is (``trace_writer.archive``). A hook
    whose ``archive`` is non-empty is the run's archive consumer: the
    runtime negotiates its archivers against the environment, binds them,
    and the consumer pulls ``Environment.collect_archive()`` on the raw
    environment at its own commit points. v1 binds at most one consumer
    per run (``collect_archive`` hands evidence to exactly one caller).
    """

    archive: list[ArchiverBase]
