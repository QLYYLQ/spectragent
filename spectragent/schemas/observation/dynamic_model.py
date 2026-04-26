"""Dynamic Pydantic model generation from negotiated fragments.

After negotiation, builds a nested Pydantic model where each active fragment
is a sub-model accessible as ``obs.terminal.stdout``, ``obs.dom.element_tree``.

See docs/architecture_design.md §4.4 "动态生成 Observation Model".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, create_model

from spectragent.schemas.observation._registrar import ObservationRegistrar

if TYPE_CHECKING:
    from spectragent.core.negotiation import ObservationNegotiationResult


def build_observation_model(
    active_fragments: frozenset[str],
) -> type[BaseModel]:
    """Build a nested Pydantic model from the set of active fragment keys.

    Each fragment becomes a required sub-model field. The resulting model
    supports nested access like ``obs.terminal.stdout``.

    Args:
        active_fragments: Fragment keys to include (from ObservationNegotiationResult).

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

    # Fragment keys are fully qualified (``source.local_id``). The
    # pydantic attribute name is the local segment so agents keep
    # reading ``obs.dom`` / ``obs.terminal``. Cross-source collisions on
    # the same local name (e.g. ``browser.screenshot`` AND
    # ``desktop.screenshot`` both active) are rejected here.
    for key in sorted(active_fragments):
        frag_cls = ObservationRegistrar.get(key)
        field_name = key.rsplit(".", 1)[-1]
        if field_name in fields:
            raise ValueError(
                f"Duplicate observation field name '{field_name}' from "
                f"active fragments {sorted(active_fragments)!r}. "
                f"Cross-source local-id collisions are not supported."
            )
        fields[field_name] = (frag_cls, ...)

    return create_model("NegotiatedObservation", **fields)


def build_observation_model_from_result(
    result: ObservationNegotiationResult,
) -> type[BaseModel]:
    """Convenience wrapper: build model directly from an ObservationNegotiationResult."""
    return build_observation_model(result.active_fragments)
