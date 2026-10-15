"""The data flywheel's metrics gate: a report over a chain's committed runs (arch §4.16).

An offline consumer of committed data, with the standing of ``evaluator/``
and ``export/``: it reads runs only through ``RunTrace``'s accessors and
datasets as the files an export published; it sets up no environment, calls
no LLM and touches no running state, so a report can be recomputed on any
machine without a browser. Not a registry — v1 has one report, and the
browser's evidence formats are read by the browser package's own functions
(``core/capabilities/browser/archive_stats.py``,
``replay/archive_index.py::iter_index_lines``,
``replay/replay_log.py::iter_decisions``); this package organises the calls.

- ``metrics.py`` — the chain root's layout, the roles' runs, every metric
  and every section of the report;
- ``report.py`` — the report model, the recommendation rules (named
  thresholds, the minimum number of sites behind a supported one),
  ``report.json`` and ``report.md``;
- ``chain.py`` — the chain: the existing CLIs' ``main(argv)`` in order, path
  fields only;
- ``cli.py`` — ``spectragent flywheel run | report``.

Nothing outside this package imports it; it imports no runtime, no live
capability, and only ``chain.py`` imports ``spectragent.export``
(``tests/test_flywheel/test_flywheel_boundaries.py``).
"""
