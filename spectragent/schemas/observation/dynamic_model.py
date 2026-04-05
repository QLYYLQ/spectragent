"""Dynamic Pydantic model generation from negotiated fragments.

After negotiation, builds a nested Pydantic model where each active fragment
is a sub-model accessible as ``obs.terminal.stdout``, ``obs.dom.element_tree``.

See docs/architecture_design.md §4.4 "动态生成 Observation Model".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.negotiation import NegotiationResult


def build_observation_model(
    active_fragments: frozenset[str],
) -> type[BaseModel]:
    """Build a nested Pydantic model from the set of active fragment keys.

    Each fragment becomes a required sub-model field. The resulting model
    supports nested access like ``obs.terminal.stdout``.

    Args:
        active_fragments: Fragment keys to include (from NegotiationResult).

    Returns:
        A dynamically created Pydantic model class.

    Example::

        Model = build_observation_model(frozenset({"terminal", "action_result"}))
        obs = Model(
            terminal=TerminalObs(stdout="hello", cwd="/tmp"),
            action_result=ActionResultObs(last_action="bash", success=True),
        )
        assert obs.terminal.stdout == "hello"
    """
    fields: dict[str, Any] = {
        "timestamp": (float, Field(default=0.0)),
    }

    for key in sorted(active_fragments):
        frag_cls = ObservationRegistrar.get(key)
        fields[key] = (frag_cls, ...)

    return create_model("NegotiatedObservation", **fields)


def build_observation_model_from_result(
    result: NegotiationResult,
) -> type[BaseModel]:
    """Convenience wrapper: build model directly from a NegotiationResult."""
    return build_observation_model(result.active_fragments)
