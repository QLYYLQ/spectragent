"""Action guard — prompt-capability consistency guarantee.

The ``build_guarded_act()`` factory produces a pair of
(action_space, guarded_act_closure). The closure validates that
every action emitted by the agent is in the active action set before
delegating to ``env.act()``. This eliminates prompt/capability mismatch:
what the prompt tells the LLM is available matches what actually executes.

Beside it, the refusals a Loop words before anything runs, all in one voice:
an action name the guard would refuse (:func:`name_refusal`), params that do
not fit the action's ``Params`` (:func:`params_refusal`), and a structured
reply that does not fit the run's output model (:func:`reply_refusal`).

See docs/architecture_design.md §4.5 "guarded_act()" for the design.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from spectragent.core.actions._registrar import registered_params
from spectragent.core.actions.action_union import (
    ACTION_PARAMS,
    ACTION_SHAPE,
    ACTION_UNKNOWN,
    ActionParamsContext,
    ActionShapeContext,
    ActionUnknownContext,
    ParamProblem,
    param_problems,
)
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


def executed_name(space: ActionSpace, name: str) -> str | None:
    """The fully-qualified name *space*'s guard executes an action called *name* as, or ``None``.

    :func:`resolve_action_name` over the space's guarded names and its
    bindings: the one naming helper for an Agent. An Agent that has to know
    which of its run's actions one of its own names means (UI-TARS's
    ``click`` verb is ``click_coordinate``), or which run action a recorded
    name is, asks this, never the name's suffix: the bindings are computed
    once, at negotiation, and a short name two capabilities offer binds to
    neither (``docs/architecture_design.md`` §4.5).
    """
    return resolve_action_name(name, space.allowed_actions, space.action_bindings)


def executed_descriptor(space: ActionSpace, name: str) -> ActionDescriptor | None:
    """The descriptor of the action called *name* executes as in *space* (:func:`executed_name`).

    ``None`` when the guard would refuse *name*. The descriptor says what the
    action's params mean in this run (``point_params``, ``element_params``).
    """
    full = executed_name(space, name)
    if full is None:
        return None
    return next(descriptor for descriptor in space.descriptors if descriptor.name == full)


def _not_an_action(space: ActionSpace, name: str) -> str:
    """The refusal of *name*, a name the run does not execute, listing the ones it does."""
    offered = ", ".join(sorted(descriptor.name for descriptor in space.descriptors))
    return f"{name} is not an action this run executes (its actions: {offered})"


def _misfit(
    name: str, descriptor: ActionDescriptor | None, problems: Sequence[ParamProblem]
) -> str:
    """The refusal of an action's params: each problem by param, then what the action takes."""
    refused = "; ".join(f"{problem['param']}: {problem['msg']}" for problem in problems)
    if descriptor is None:
        return f"{name}: {refused}"
    takes = (
        ", ".join(f"{param}: {text}" for param, text in descriptor.params.items())
        or "no parameters"
    )
    return f"{name}: {refused} ({descriptor.name} takes {takes})"


def name_refusal(space: ActionSpace, name: str) -> str | None:
    """Why *space*'s run does not execute an action called *name*, or ``None``.

    The name check every parsed action passes before it runs
    (``ReactLoop.check_action_params``): a name the guard would refuse
    (:func:`executed_name` is ``None``), refused with the run's actions
    listed. ``None`` when the guard executes the name, and in a run that
    advertises no actions (no environment): it told the model no names to
    keep to, and its guard refuses whatever it is handed.
    """
    if not space.descriptors or executed_name(space, name) is not None:
        return None
    return _not_an_action(space, name)


def params_refusal(space: ActionSpace, action: Action) -> str | None:
    """Why *action*'s params break the ``Params`` of the action it executes as, or ``None``.

    The check every parsed action passes before it runs
    (``ReactLoop.check_action_params``): its params validated against the
    ``Params`` model of the action *space*'s guard executes it as. The answer
    is written for the model that wrote the action: each problem by param
    (``index: Field required``, ``reasoning: Extra inputs are not
    permitted``), then what the action takes in this run, from its
    descriptor. ``None`` when the params fit, when the guard would refuse the
    name itself (:func:`name_refusal` says so), and when the action has no
    registered ``Params`` to check against.
    """
    descriptor = executed_descriptor(space, action.name)
    if descriptor is None:
        return None
    params = registered_params(descriptor.name)
    if params is None:
        return None
    try:
        params.model_validate(action.params)
    except ValidationError as exc:
        return _misfit(action.name, descriptor, param_problems(exc))
    return None


def reply_refusal(space: ActionSpace, output_model: type[BaseModel], text: str) -> str | None:
    """Why *text*, a structured reply, does not fit *output_model*, or ``None`` when it does.

    The refusal of a reply that does not fit the run's output model, in the
    voice of :func:`name_refusal` and :func:`params_refusal`, so the model is
    told the same thing for the same mistake whether its reply is text or
    structured. A function of the reply's text, the output model and the
    run's action *space* alone: a live step words a refused reply with it
    (``ReactLoop.read_reply``), and a branch's replay of the recorded reply
    (``BranchLoop``) words it again, identically.

    *text* is validated as JSON, as the provider validates it
    (``ChatModelBase.validate_structured``). Text that is not JSON is one
    line with the decoder's position. Otherwise every problem is located:
    an action item by its number in the reply's action list, the way
    :func:`params_refusal` names it (``action 2, browser.type: text: Field
    required (browser.type takes index: ..., text: ...)``), an unknown verb the way
    :func:`name_refusal` does, and any other field by its path
    (``next_goal: Field required``). The action items' mistakes are read from
    the typed errors of the closed action union
    (``core/actions/action_union.py``), never from a message.
    """
    try:
        output_model.model_validate_json(text)
    except ValidationError as exc:
        errors = exc.errors(include_url=False)
        for error in errors:
            if error["type"] == "json_invalid":
                # The decoder's words, with its position.
                decoder = error.get("ctx", {}).get("error")
                said = decoder if isinstance(decoder, str) else error["msg"]
                return f"the reply is not valid JSON: {said}"
        problems = "; ".join(_reply_problem(space, error) for error in errors)
        return f"the reply does not fit the output schema: {problems}"
    return None


def _reply_problem(space: ActionSpace, error: ErrorDetails) -> str:
    """One validation error of a structured reply, located and worded for the model."""
    loc = error["loc"]
    kind = error["type"]
    if kind not in (ACTION_SHAPE, ACTION_UNKNOWN, ACTION_PARAMS):
        # A problem with the reply as a whole (``Input should be an object``)
        # has no path to name.
        return f"{_field_path(loc)}: {error['msg']}" if loc else error["msg"]
    # The action's number is its index in the list that holds it — the last
    # index on its path, however deep an agent nests the union
    # (``actions[1].action``).
    index = next((part for part in reversed(loc) if isinstance(part, int)), None)
    where = f"action {index + 1}" if index is not None else _field_path(loc)
    if kind == ACTION_PARAMS:
        params = cast(ActionParamsContext, error.get("ctx"))
        verb = params["verb"]
        return f"{where}, {_misfit(verb, executed_descriptor(space, verb), params['problems'])}"
    if kind == ACTION_UNKNOWN:
        unknown = cast(ActionUnknownContext, error.get("ctx"))
        return f"{where}, {_not_an_action(space, unknown['verb'])}"
    shape = cast(ActionShapeContext, error.get("ctx"))
    offered = ", ".join(sorted(descriptor.name for descriptor in space.descriptors))
    return (
        f"{where}, {shape['written']}: each action is an object with exactly one key, the "
        f"action's name, whose value is the object of its parameters (its actions: {offered})"
    )


def _field_path(loc: tuple[int | str, ...]) -> str:
    """A field's path in a reply, as JSON reads it: ``actions[1].reasoning``."""
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path


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
            # object and is the one that writes it into ``StepRecord``.
            # A copy would leave the
            # trace naming a provider-less verb for an action some
            # specific capability executed — the provenance loss the
            # observation axis already refuses.
            action.name = full
        return await act_fn(action, **kwargs)

    return action_space, guarded_act
