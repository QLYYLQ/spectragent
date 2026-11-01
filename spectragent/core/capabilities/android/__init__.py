"""Android capability package.

An Android device, reached over ADB, as one capability: its screen, its input,
its foreground app. ``docs/architecture_design.md`` §11.2 designs it and
``docs/execution_dag.md`` plans its stages. The layers below the capability are
the package's own code (§11.2): the host transport (``adb.py``, ``adb_server.py``),
the device session (``session.py``) and the bridge to the device
(``bridge.py``), whose first implementation uses adb's built-in commands.

This file imports nothing. Every subpackage of ``spectragent/core/capabilities``
is a capability, and the framework reads only its ``declarations.py`` before a
run names it; importing that module runs this file first (§4.2.1).
"""
