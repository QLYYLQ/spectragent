"""Action guard — prompt-capability consistency guarantee.

The ``build_guarded_act()`` factory produces a pair of
(filtered_descriptors, guarded_act_closure). The closure validates that
every action emitted by the agent is in the active action set before
delegating to ``env.act()``. This eliminates prompt/capability mismatch:
what the prompt tells the LLM is available matches what actually executes.

See docs/architecture_design.md §4.5 "guarded_act()" for the design.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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
    *,
    bindings: Mapping[str, str] | None = None,
) -> tuple[list[ActionDescriptor], Callable[..., Awaitable[ActionResult]]]:
    """Build a guarded action executor with optional filtering.

    This is the **only** place a capability-relative action name becomes a
    fully-qualified one. Everything past this closure — ``env.act``
    prefix routing, ``ActionDescriptor.name``, the trace — sees the full
    key and nothing else, so the collision-free routing the prefix exists
    for is untouched by agents that speak short names.

    Args:
        act_fn: The underlying ``env.act(action, **kwargs)`` callable.
        descriptors: Full list of action descriptors from the environment.
        action_filter: Optional allowlist of action names for ablation
            experiments. When provided, only descriptors whose name is
            in this list are retained.
        bindings: ``ActionNegotiationResult.action_bindings`` — short name
            → fully-qualified key for this run. An agent that emits
            ``click`` gets it rewritten to the ``click`` this run actually
            has. Omitted (``None``) means the caller negotiated nothing,
            so only fully-qualified names are accepted.

    Returns:
        A tuple of ``(filtered_descriptors, guarded_act)``:
        - ``filtered_descriptors``: the (possibly narrowed) descriptor
          list — use this for prompt building.
        - ``guarded_act``: an async closure that resolves the action name
          against *bindings* and validates it is in the active set before
          delegating to *act_fn*.
    """
    if action_filter is not None:
        allowed = set(action_filter)
        filtered = [d for d in descriptors if d.name in allowed]
    else:
        filtered = list(descriptors)

    active_names = frozenset(d.name for d in filtered)
    # An ablation can narrow the descriptors after negotiation, so a
    # binding whose target did not survive it is dropped here rather than
    # resolving to an action the run no longer executes.
    resolvable = {
        short: full
        for short, full in (bindings or {}).items()
        if full in active_names and short not in active_names
    }

    async def guarded_act(action: Action, **kwargs: Any) -> ActionResult:
        name = action.name
        if name not in active_names:
            full = resolvable.get(name)
            if full is None:
                return await _reject_action(action, active_names)
            # Resolve in place, not onto a copy: the caller keeps this
            # object and is the one that writes it into ``StepRecord``
            # and ``AgentResult.action_history``. A copy would leave the
            # trace naming a provider-less verb for an action some
            # specific capability executed — the provenance loss the
            # observation axis already refuses.
            action.name = full
        return await act_fn(action, **kwargs)

    return filtered, guarded_act
