"""What archiving may cost per step, defined once (``docs/architecture_design.md`` §4.14, §4.16).

Recording environment evidence adds time to every step: a capability's
collection, the evidence's commit. :data:`ARCHIVE_MAX_P50_OVERHEAD_MS` is the
budget for that time at the median step, whichever capability archives. Two
readers judge against it and must agree: the browser's overhead benchmark
(``tests/test_core/test_capabilities/test_browser_archive_live.py``, which a
noisy machine may override with ``SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS``) and
the flywheel report's archive-overhead rule (``spectragent/flywheel/report.py``).

.. note::
   No ``from __future__ import annotations`` — the package rule
   (:mod:`spectragent.core.archive._registrar`).
"""

#: Milliseconds archiving may add to a step at the median (archived minus not archived).
ARCHIVE_MAX_P50_OVERHEAD_MS: float = 300.0

__all__ = ["ARCHIVE_MAX_P50_OVERHEAD_MS"]
