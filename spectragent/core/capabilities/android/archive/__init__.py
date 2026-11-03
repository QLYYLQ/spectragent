"""The Android capability's archivers: environment evidence the trace hook pulls (``docs/architecture_design.md`` §11.2).

``base.py`` holds what every Android archiver shares: the configured instance's
base, the run-time session, and the context the capability assembles at setup;
``ui_tree_raw.py`` registers ``android.ui_tree_raw``. This file imports
nothing, like the rest of the package's ``__init__`` files (§4.2.1).
"""
