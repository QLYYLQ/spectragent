"""Generated (by ``spectragent-gen``) config schemas — do not hand-edit.

Re-exports the top-level ``ComposedConfig`` Pydantic model so callers
can import a stable surface::

    from spectragent.generated import ComposedConfig

The import is guarded, but **only** against the file being absent:
``spectragent-gen`` wipes before it regenerates, and ``discover()`` must
survive that window. Anything else — a ``NameError`` from a codegen bug,
a ``SyntaxError`` from a malformed emit — propagates.

This used to be a bare ``except Exception``, which silently degraded the
surface to ``None`` and hid a real defect: the committed ``_composed.py``
raised ``NameError: name 'AwsProviderConfig' is not defined`` at import
for months while every caller just saw ``ComposedConfig is None``.
"""

try:  # pragma: no cover — trivial bootstrap
    from spectragent.generated._composed import ComposedConfig  # noqa: F401
except ImportError:  # the wipe-and-regenerate window, nothing else
    ComposedConfig = None  # type: ignore[assignment]
