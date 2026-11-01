"""Android capability backends: what brings up a device and hands over its ADB endpoint.

``_registrar.py`` holds ``AndroidProviderRegistrar`` and ``base.py`` the
provider contract; each provider registers in a module of its own
(``local_emulator.py``). This file imports nothing, so reading the package's
``declarations.py`` loads no provider (``docs/architecture_design.md`` §4.2.1).
"""
