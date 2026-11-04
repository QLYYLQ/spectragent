"""AndroidWorld: tasks on an emulated phone, prepared and judged by AndroidWorld's own service (§11.2).

The package holds the benchmark (``benchmark.py``), its setup component
(``setup.py``), its live verifier (``verifier.py``), AndroidWorld's service and
installation (``service.py``), the judgment read from it (``scoring.py``), its
task list's model (``tasks.py``) and
the programs that run under AndroidWorld's own interpreter
(``androidworld_service/``, which is not a Python package: Spectragent never
imports them). This file imports none of them, so the task loader reads a list
through ``tasks.py`` without the setup component, its service or the Android
capability; discovery imports the rest when a run names the benchmark.
"""
