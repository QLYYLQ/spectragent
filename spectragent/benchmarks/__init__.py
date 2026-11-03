"""The benchmark layer (``docs/architecture_design.md`` §4.10.1).

Every subpackage is one benchmark, named by its ``benchmark_id``
(``online_mind2web``, ``androidworld``), or the branch sweep's task source
(``trace_branches``), which is not a benchmark of its own: each branch task
belongs to its source run's benchmark. A run imports only the subpackages its
configuration names (§4.2.1), and importing any module of this package runs
this file first, so it imports no subpackage.
"""

from spectragent.benchmarks._registrar import Benchmark, BenchmarkBase, BenchmarkRegistrar

__all__ = ["Benchmark", "BenchmarkBase", "BenchmarkRegistrar"]
