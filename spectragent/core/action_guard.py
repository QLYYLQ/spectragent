"""Action guard — prompt-capability consistency guarantee.

The ``build_guarded_act()`` factory produces a pair of
(filtered_descriptors, guarded_act_closure). The closure validates that
every action emitted by the agent is in the active action set before
delegating to ``env.act()``. This eliminates prompt/capability mismatch:
what the prompt tells the LLM is available matches what actually executes.

See docs/architecture_design.md §4.5 "guarded_act()" for the design.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


async def _reject_action(action: Action, active_names: frozenset[str]) -> ActionResult:
    return ActionResult(
        success=False,
        error=(
            f"Action '{action.name}' is not in the active action set. "
            f"Available actions: {sorted(active_names)}"
        ),
    )


def build_guarded_act(
    act_fn: Callable[..., Awaitable[ActionResult]],
    descriptors: list[ActionDescriptor],
    action_filter: list[str] | None = None,
) -> tuple[list[ActionDescriptor], Callable[..., Awaitable[ActionResult]]]:
    """Build a guarded action executor with optional filtering.

    Args:
        act_fn: The underlying ``env.act(action, **kwargs)`` callable.
        descriptors: Full list of action descriptors from the environment.
        action_filter: Optional allowlist of action names for ablation
            experiments. When provided, only descriptors whose name is
            in this list are retained.

    Returns:
        A tuple of ``(filtered_descriptors, guarded_act)``:
        - ``filtered_descriptors``: the (possibly narrowed) descriptor
          list — use this for prompt building.
        - ``guarded_act``: an async closure that validates the action
          name is in the active set before delegating to *act_fn*.
    """
    if action_filter is not None:
        allowed = set(action_filter)
        filtered = [d for d in descriptors if d.name in allowed]
    else:
        filtered = list(descriptors)

    active_names = frozenset(d.name for d in filtered)

    async def guarded_act(action: Action, **kwargs: Any) -> ActionResult:
        if action.name not in active_names:
            return await _reject_action(action, active_names)
        return await act_fn(action, **kwargs)

    return filtered, guarded_act
