"""Generic negotiation engine.

Unifies observation (three-party) and action (two-party) negotiation
under a single signature. Downstream callers stay thin: they pick the
``target_class`` and ``rules`` function, pass wiring-bearing classes as
kwargs, and the engine does the wiring extraction + dispatch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from spectragent.core.negotiation._base import extract_wiring

TResult = TypeVar("TResult")


def negotiate(
    target_class: type[TResult],
    wiring_name: str | Mapping[str, str],
    rules: Callable[..., TResult],
    **kwargs: Any,
) -> TResult:
    """Run a negotiation and return a ``target_class`` instance.

    For each kwarg:
      - If the value is a class, extract ``(required, optional)`` via
        :func:`extract_wiring` using the registry name from ``wiring_name``
        (uniform str, or per-kwarg lookup when ``wiring_name`` is a Mapping).
      - Otherwise pass the value through unchanged (e.g. raw ``env_provides``).

    When ``wiring_name`` is a Mapping and a key is missing, the kwarg is
    forwarded as-is; this lets callers mix wiring-bearing classes with raw
    values in a single call.

    ``rules`` receives the resolved kwargs and must return an instance of
    ``target_class``. The TypeVar lets static checkers infer the return
    type from ``target_class`` at call sites.
    """
    resolved: dict[str, Any] = {}
    for key, value in kwargs.items():
        if not isinstance(value, type):
            resolved[key] = value
            continue
        reg = wiring_name if isinstance(wiring_name, str) else wiring_name.get(key)
        if reg is None:
            resolved[key] = value
            continue
        resolved[key] = extract_wiring(value, reg)
    result = rules(**resolved)
    if not isinstance(result, target_class):
        raise TypeError(
            f"Negotiation rules returned {type(result).__name__}, "
            f"expected {target_class.__name__}"
        )
    return result
