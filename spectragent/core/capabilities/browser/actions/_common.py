"""Shared helpers used by multiple browser action classes.

Keeps the per-action modules short while avoiding duplication for the
``index → element`` translation that several actions need.

Both helpers resolve against the capability's **focused tab**
(``BrowserCapability.default_session_id``, moved by ``switch_tab``).
Actions take no session argument: the CDP session id is a browser
concept, and the framework Protocols an action sits behind must not
carry one. The capability exposes the focused scope and its read-only selector map
through public queries; actions never reach into its storage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from agent_cdp import Participation
from pydantic import ValidationError

from spectragent.schemas.action import ActionEffect

if TYPE_CHECKING:
    from spectragent.core.actions._registrar import ActionBase
    from spectragent.core.capabilities.browser.capability import BrowserCapability

logger = logging.getLogger(__name__)


class ElementRef(NamedTuple):
    """How an event addresses an element: CDP's own ``(session, backend id)``.

    ``session_id`` is the session of the target that owns the node — not the
    tab's for an element inside an out-of-process iframe, whose backend node
    id may well equal one in the page.
    """

    backend_node_id: int
    session_id: str | None


def focused_scope(capability: BrowserCapability) -> Any:
    """Return the ``EventScope`` of the capability's focused tab.

    Raises:
        RuntimeError: If no tab is attached (``setup()`` has not run).
        KeyError: If the focused tab has no scope — a torn-down session.
    """
    return capability.action_scope


def resolve_index(
    capability: BrowserCapability,
    index: Any,
) -> ElementRef:
    """Translate an LLM-facing ``index`` param into the element it names.

    The focused tab's selector_map is keyed by the same integer the model
    reads as ``[index]`` (``dom.models.element_handle``); the element found
    there says where CDP can reach it.

    Raises:
        ValueError: If the element is missing from the selector_map.
    """
    selector_map = capability.selector_map
    node = selector_map.get(index)
    if node is None or node.backend_node_id is None:
        raise ValueError(f"Element {index} not found in selector_map")
    return ElementRef(node.backend_node_id, node.session_id)


def handler_field(event: Any, key: str) -> Any:
    """The value under *key* in the dict a watchdog handler answered ``event`` with.

    ``None`` when no handler answered with a dict carrying it — the typing
    handler's ``sensitive`` verdict, for instance, which a run whose handler
    could not judge the field leaves out or sets to ``None``.
    """
    for outcome in event.results.values():
        if (
            outcome.participation is not Participation.HANDLER
            or outcome.receiver_key != "browser.action"
        ):
            continue
        value = outcome.result
        if isinstance(value, dict) and key in value:
            return value[key]
    return None


def effect_from(action: ActionBase, facts: Any) -> ActionEffect | None:
    """The action's ``Effect`` built from the facts its executor measured, or ``None``.

    *facts* is the ``effect_facts`` mapping a watchdog handler answered with
    (:func:`handler_field`) or that the action measured itself: every field of
    ``action.Effect`` but ``effect_id``, which is the action's own key. No
    facts, or an action without an ``Effect``, is ``None``. A failed action
    may carry facts too: typing that found no text field with focus answers
    the focus it measured along with its error. Facts the model refuses are a
    programming error in the executor; they are logged and the action still
    returns its outcome — building the effect never changes it
    (``docs/architecture_design.md`` §4.5, "动作效果").
    """
    effect_cls = action.Effect
    if effect_cls is None or not isinstance(facts, dict):
        return None
    try:
        return effect_cls(effect_id=action.action_id, **facts)
    except (TypeError, ValidationError) as exc:
        logger.error("%s measured an effect its Effect model refuses: %s", action.action_id, exc)
        return None
