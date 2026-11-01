"""The Android task oracle: who prepares, scores and ends a benchmark's task on the device.

``_registrar.py`` holds ``AndroidTaskOracleRegistrar``, the oracle contract and
the default; each oracle registers in a module of its own (``none.py``,
``androidworld.py``). This file imports nothing, so reading the package's
``declarations.py`` loads no oracle (``docs/architecture_design.md`` §4.2.1).
"""
