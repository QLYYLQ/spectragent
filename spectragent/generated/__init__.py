"""Generated (by ``spectragent-gen``) config schemas — do not hand-edit.

Re-exports the top-level ``ComposedConfig`` Pydantic model so callers
can import a stable surface::

    from spectragent.generated import ComposedConfig

The import is guarded: during ``spectragent-gen`` itself the file is
momentarily absent while the script wipes-and-regenerates, so the
surface degrades to ``None`` rather than blowing up ``discover()``.
"""

try:  # pragma: no cover — trivial bootstrap
    from spectragent.generated._composed import ComposedConfig  # noqa: F401
except Exception:  # noqa: BLE001 — codegen may be mid-update or have latent issues
    ComposedConfig = None  # type: ignore[assignment]
