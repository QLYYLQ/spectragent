"""The Android capability's UI tree: who captures it, which nodes become elements, how they are printed.

``_registrar.py`` holds the three registrars (``android_ui_tree_source``,
``android_ui_tree_cleaner``, ``android_ui_tree_serializer``), their contracts and
their bases; ``models.py`` the cleaned tree they pass along; ``source.py``,
``cleaning.py`` and ``serializers.py`` register the implementations. This file
imports nothing, so reading the package's ``declarations.py`` loads no
implementation (``docs/architecture_design.md`` §4.2.1).
"""
