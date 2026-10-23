"""The action space an Agent's ``prepare`` is handed, built the way a run builds it.

``AgentRuntime.seed_messages`` hands an Agent the run's effective
``ActionSpace``: the negotiated, guarded descriptors and the bindings that say
what a short name means in this run. A test that prepares an Agent by hand
builds the same object here: the descriptors it names, each guarded, and the
short-name bindings negotiation computes for them
(``core/negotiation/_base.py``), so a short name two of them share binds to
neither, as in a run.

:func:`browser_space` uses the registered browser actions' real descriptors,
which carry what each action's params mean (``point_params``,
``element_params``).

``coordinate_space`` frames the space the way ``AgentRuntime.prepare`` frames
it for an Agent that declares that grid (``framed_action_space``): pass the
prepared Agent's own ``coordinate_space``, or use :func:`space_for`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import spectragent.core.capabilities.browser.actions  # noqa: F401 — browser actions registered
from spectragent.core.actions._registrar import ACTION_KEY_SEPARATOR, ActionRegistrar
from spectragent.core.loops.coordinate_frames import declared_space, framed_action_space
from spectragent.core.negotiation._base import (
    bind_short_names,
    extract_wiring,
    group_by_short_name,
)
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace

__all__ = ["browser_descriptors", "browser_space", "space_for", "space_of", "wired_space"]


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


def browser_descriptors(*names: str, **context: Any) -> list[ActionDescriptor]:
    """The registered browser actions' descriptors; a short name is ``browser.<name>``."""
    return [
        ActionRegistrar.get(
            name if ACTION_KEY_SEPARATOR in name else f"browser.{name}"
        )().descriptor(**context)
        for name in names
    ]


def browser_space(
    *names: str, coordinate_space: CoordinateSpace | None = None, **context: Any
) -> ActionSpace:
    """:func:`space_of` the registered browser actions *names* (:func:`browser_descriptors`)."""
    return space_of(browser_descriptors(*names, **context), coordinate_space=coordinate_space)


def wired_space(
    agent_cls: type, *, coordinate_space: CoordinateSpace | None = None, **context: Any
) -> ActionSpace:
    """The browser action space a run gives *agent_cls*: every action its wiring names.

    Required and optional, each the registered browser action of that name,
    as a browser environment that offers them all negotiates them.
    """
    required, optional = extract_wiring(agent_cls, "action")
    return browser_space(*sorted(required | optional), coordinate_space=coordinate_space, **context)
