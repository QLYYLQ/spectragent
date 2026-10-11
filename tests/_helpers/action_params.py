"""Synthesize a minimal legal payload from an ``ActionDescriptor``.

The seam §2.5 warns about: *negotiation matches names, not parameter
contracts.* An agent may emit a verb the run holds and still send it
parameters the action cannot read, and nothing between the two would
notice — the guard checks the name, and the action reads a bare dict.

Now that ``params_schema`` is populated for every action, a test can walk
the declared contract and build the smallest payload that satisfies it,
then push it through the agent's parser and the guard and check what
arrives. This helper is that walk, kept in one place so the two tests that
do it (``test_browser_agents_runtime_compat``,
``test_browser_use_modes``) agree on what "minimal" means.
:func:`variant_params` extends the walk to every optional param, for the
training export's generic round-trip test
(``test_format_react_output_round_trip``), which has to show that a
formatter either writes each param or refuses it by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["minimal_params", "variant_params"]

_SAMPLES: dict[str, Any] = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
}


def _sample(node: Mapping[str, Any]) -> Any:
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        for branch in any_of:
            if branch.get("type") != "null":
                return _sample(branch)
        return None
    return _SAMPLES.get(str(node.get("type")), "x")


def minimal_params(descriptor: Any) -> dict[str, Any]:
    """The smallest payload that satisfies *descriptor*'s required params."""
    schema = descriptor.params_schema or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = schema.get("required") or ()
    return {name: _sample(properties[name]) for name in required if name in properties}


def variant_params(descriptor: Any) -> list[dict[str, Any]]:
    """The minimal payload, then one variant per optional param, set to a sample value.

    Optional params are visited in name order, so the list is deterministic.
    """
    schema = descriptor.params_schema or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    minimal = minimal_params(descriptor)
    return [minimal] + [
        {**minimal, name: _sample(properties[name])}
        for name in sorted(properties)
        if name not in required
    ]
