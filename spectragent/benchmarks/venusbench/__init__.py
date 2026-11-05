"""VenusBench-Mobile: AndroidWorld's fork with harder tasks, prepared and judged by AndroidWorld's own service (§11.2).

``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案". The package
holds the benchmark (``benchmark.py``), its setup component (``setup.py``), its
live verifier (``verifier.py``), its task loader (``loader.py``), its pinned
code and the tasks its first version runs (``tasks.py``), its installation
(``service.py``, ``install.py``), and the programs that run under its own
interpreter (``venusbench_service/``, which is not a Python package:
Spectragent never imports them). What it shares with AndroidWorld stays in
AndroidWorld's package, in modules that register nothing (its service, task
lists, setup and verifier bases, scoring and ``open_app`` table), so a run
that names only VenusBench registers none of AndroidWorld's components
(§4.2.1). This file imports none of them.
"""
