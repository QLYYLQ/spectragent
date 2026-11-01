"""The action space an Agent's ``prepare`` is handed, built the way a run builds it.

``AgentRuntime.seed_messages`` hands an Agent the run's effective
``ActionSpace``: the negotiated, guarded descriptors and the bindings that say
what a short name means in this run. A test that prepares an Agent by hand
builds the same object here: the descriptors it names, each guarded, and the
short-name bindings negotiation computes for them
(``core/negotiation/_base.py``), so a short name two of them share binds to
neither, as in a run.

:func:`browser_space` uses the real descriptors of a browser run's actions,
which carry what each action's params mean (``point_params``,
``element_params``): the browser capability's, and the Loop's own
(``loop.done``), which a run's negotiation binds a short ``done`` to.

``coordinate_space`` frames the space the way ``AgentRuntime.prepare`` frames
it for an Agent that declares that grid (``framed_action_space``): pass the
prepared Agent's own ``coordinate_space``, or use :func:`space_for`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import spectragent.core.capabilities.browser.actions  # noqa: F401 — browser actions registered
from spectragent.core.actions._registrar import (
    ACTION_KEY_SEPARATOR,
    LOOP_ACTION_SOURCE,
    ActionRegistrar,
)
from spectragent.core.loops import AgentLoopBase, ReactLoop
from spectragent.core.loops.actions import LOOP_ACTION_KEYS
from spectragent.core.loops.coordinate_frames import declared_space, framed_action_space
from spectragent.core.negotiation._base import (
    bind_short_names,
    extract_wiring,
    group_by_short_name,
)
from spectragent.core.negotiation.action import ActionNegotiationResult, negotiate_action
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace

__all__ = [
    "browser_descriptors",
    "browser_run_descriptors",
    "browser_space",
    "negotiated_actions",
    "run_action_key",
    "space_for",
    "space_of",
    "wired_space",
]


def space_of(
    descriptors: Iterable[ActionDescriptor], *, coordinate_space: CoordinateSpace | None = None
) -> ActionSpace:
    """The action space of a run that negotiated exactly *descriptors*, with no filter.

    Framed on *coordinate_space* (``framed_action_space``) when it holds a point action.
    """
    listed = list(descriptors)
    names = frozenset(descriptor.name for descriptor in listed)
    space = ActionSpace(
        descriptors=listed,
        allowed_actions=names,
        action_bindings=bind_short_names(group_by_short_name(names, ACTION_KEY_SEPARATOR)),
    )
    return framed_action_space(space, coordinate_space)


def space_for(agent: Any, descriptors: Iterable[ActionDescriptor]) -> ActionSpace:
    """:func:`space_of` *descriptors*, framed on *agent*'s declared grid as a run frames it."""
    return space_of(descriptors, coordinate_space=declared_space(agent))


def run_action_key(name: str) -> str:
    """The action a browser run binds *name* to: the Loop's (``done``), else the browser's.

    A fully-qualified *name* is itself.
    """
    if ACTION_KEY_SEPARATOR in name:
        return name
    loop_key = f"{LOOP_ACTION_SOURCE}{ACTION_KEY_SEPARATOR}{name}"
    return loop_key if loop_key in LOOP_ACTION_KEYS else f"browser{ACTION_KEY_SEPARATOR}{name}"


def browser_descriptors(*names: str, **context: Any) -> list[ActionDescriptor]:
    """The registered descriptors of a browser run's actions *names* (:func:`run_action_key`)."""
    return [ActionRegistrar.get(run_action_key(name))().descriptor(**context) for name in names]


def browser_run_descriptors(active: Iterable[str], **context: Any) -> list[ActionDescriptor]:
    """The descriptors of a browser run whose active actions are *active*, listed as a run lists them.

    The browser capability's in its wiring's order, then the Loop's own
    (``loop.done``), as ``AgentRuntime.prepare`` lists them. *active* names
    actions fully qualified (:func:`run_action_key`).
    """
    from spectragent.core.capabilities.browser.capability import BrowserCapability

    keys = frozenset(active)
    return [
        *(a.descriptor(**context) for a in BrowserCapability().actions if a.action_id in keys),
        *ReactLoop().get_action_space(active_actions=keys, **context),
    ]


def browser_space(
    *names: str, coordinate_space: CoordinateSpace | None = None, **context: Any
) -> ActionSpace:
    """:func:`space_of` the registered browser actions *names* (:func:`browser_descriptors`)."""
    return space_of(browser_descriptors(*names, **context), coordinate_space=coordinate_space)


def wired_space(
    agent_cls: type, *, coordinate_space: CoordinateSpace | None = None, **context: Any
) -> ActionSpace:
    """The browser action space a run gives *agent_cls*: every action its wiring names that a browser has.

    Required and optional, each the registered action of that name a browser
    run binds it to (:func:`run_action_key`), as a browser environment that
    offers them all, under a Loop that provides ``loop.done``, negotiates them.
    An optional action no browser registers (UI-TARS's ``long_press``) is left
    out, as negotiation leaves it out.
    """
    required, optional = extract_wiring(agent_cls, "action")
    offered = {name for name in optional if ActionRegistrar.get_or_none(run_action_key(name))}
    return browser_space(*sorted(required | offered), coordinate_space=coordinate_space, **context)


class _ProvidesActions:
    """An environment reduced to the one fact action negotiation reads off it."""

    def __init__(self, capability: Any) -> None:
        self.provided_actions = capability.provided_actions


def negotiated_actions(
    agent: Any, capability: Any, *, loop: AgentLoopBase | None = None, **context: Any
) -> tuple[ActionNegotiationResult, list[ActionDescriptor]]:
    """What a run of *agent* over *capability* negotiates, and the descriptors it lists.

    Listed as ``AgentRuntime.prepare`` lists them: the capability's active
    actions, then the Loop's own (*loop*, a ``ReactLoop`` by default, which
    provides ``loop.done``). *context* reaches every descriptor. A guard built
    from them needs the Loop to execute ``loop.done``:
    ``build_guarded_act(..., loop_act=loop.execute_action)``.
    """
    run_loop = loop if loop is not None else ReactLoop()
    negotiated = negotiate_action(agent, run_loop, _ProvidesActions(capability))
    active = negotiated.active_actions
    descriptors = [
        action.descriptor(**context) for action in capability.actions if action.action_id in active
    ]
    return negotiated, [*descriptors, *run_loop.get_action_space(active_actions=active, **context)]
