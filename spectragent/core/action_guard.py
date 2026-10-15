"""Action guard — prompt-capability consistency guarantee.

The ``build_guarded_act()`` factory produces a pair of
(action_space, guarded_act_closure). The closure validates that
every action emitted by the agent is in the active action set before
delegating to ``env.act()``. This eliminates prompt/capability mismatch:
what the prompt tells the LLM is available matches what actually executes.

See docs/architecture_design.md §4.5 "guarded_act()" for the design.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from collections.abc import Set as AbstractSet
from typing import Any

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace


def resolve_action_name(
    name: str,
    active_names: AbstractSet[str],
    bindings: Mapping[str, str] | None = None,
) -> str | None:
    """The fully-qualified name ``guarded_act`` executes an action called *name* as.

    The one copy of the rule (``guarded_act`` applies it to every action it
    is handed): an active name is itself; a short name resolves through
    *bindings* only when its target is active — an ablation
    (``action_filter``) that dropped the target revokes the alias too;
    anything else is refused (``None``), and ``guarded_act`` leaves the
    refused action's name as the agent wrote it. A caller that has to know
    what the guard did to an action — ``BranchLoop``'s replay comparing a
    re-parsed step with the recorded one — asks this function rather than
    working the rule out again.

    Args:
        name: The action name as the agent emitted it.
        active_names: The names the guard executes — its filtered
            descriptors' names.
        bindings: ``ActionNegotiationResult.action_bindings`` (short name →
            fully-qualified key); ``None`` accepts fully-qualified names only.
    """
    if name in active_names:
        return name
    full = (bindings or {}).get(name)
    return full if full is not None and full in active_names else None


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
) -> tuple[ActionSpace, Callable[..., Awaitable[ActionResult]]]:
    """Build a guarded action executor with optional filtering.

    This is the **only** place a capability-relative action name becomes a
    fully-qualified one (by :func:`resolve_action_name`, the rule's one
    copy). Everything past this closure — ``env.act``
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
        A tuple of ``(action_space, guarded_act)``:
        - ``action_space``: the original filter, final prompt descriptors,
          allowed names and bindings — one typed experiment contract.
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
    bound = dict(bindings or {})
    action_space = ActionSpace(
        action_filter=list(action_filter) if action_filter is not None else None,
        descriptors=[descriptor.model_copy(deep=True) for descriptor in filtered],
        allowed_actions=active_names,
        action_bindings=dict(bound),
    )

    async def guarded_act(action: Action, **kwargs: Any) -> ActionResult:
        # An ablation can narrow the descriptors after negotiation, so a
        # binding whose target did not survive it does not resolve to an
        # action the run no longer executes (``resolve_action_name``).
        full = resolve_action_name(action.name, active_names, bound)
        if full is None:
            return await _reject_action(action, active_names)
        if full != action.name:
            # Resolve in place, not onto a copy: the caller keeps this
            # object and is the one that writes it into ``StepRecord``
            # and ``AgentResult.action_history``. A copy would leave the
            # trace naming a provider-less verb for an action some
            # specific capability executed — the provenance loss the
            # observation axis already refuses.
            action.name = full
        return await act_fn(action, **kwargs)

    return action_space, guarded_act
