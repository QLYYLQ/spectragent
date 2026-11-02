"""The Android capability's input strategies: who delivers a touch, and who enters text.

``_registrar.py`` holds the two registrars (``android_tap_injector``,
``android_typing_backend``), their contracts and their bases; ``tap.py`` and
``text.py`` register the implementations. This file imports nothing, so
reading the package's ``declarations.py`` loads no implementation
(``docs/architecture_design.md`` §4.2.1).
"""
