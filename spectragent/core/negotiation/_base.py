"""Shared primitives for observation and action negotiation.

Contains the single ``extract_wiring`` helper used by both negotiation
flows; before this module existed, this logic was duplicated verbatim in
``schemas/observation/negotiation.py`` and ``core/actions/negotiation.py``.
"""

from __future__ import annotations

from conscribe.wiring import parse_wiring


def extract_wiring(cls: type, registry_name: str) -> tuple[set[str], set[str]]:
    """Extract ``(required, optional)`` key sets from ``cls.__wiring__``.

    Uses ``conscribe.wiring.parse_wiring`` so registries don't need to be
    populated at parse time. Returns empty sets when no matching wiring
    spec is found.
    """
    specs = parse_wiring(cls)
    for spec in specs:
        if spec.registry_name == registry_name:
            required = set(spec.allowed_keys) if spec.allowed_keys else set()
            optional = set(spec.optional_keys) if spec.optional_keys else set()
            return required, optional
    return set(), set()
