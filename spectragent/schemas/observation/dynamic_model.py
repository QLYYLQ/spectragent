"""Dynamic Pydantic model generation from negotiated fragments.

After negotiation, builds a nested Pydantic model where each active fragment
is a sub-model accessible as ``obs.terminal.stdout``, ``obs.dom.element_tree``.

See docs/architecture_design.md §4.4 "动态生成 Observation Model".
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, create_model

from spectragent.schemas.observation._registrar import (
    ObservationRegistrar,
    fragment_field_name,
)
from spectragent.schemas.observation.failure import FragmentFailure

if TYPE_CHECKING:
    from spectragent.core.negotiation import ObservationNegotiationResult


def build_observation_model(
    active_fragments: frozenset[str],
    *,
    failable: frozenset[str] = frozenset(),
) -> type[BaseModel]:
    """Build a nested Pydantic model from the set of active fragment keys.

    Each fragment becomes a required sub-model field. The resulting model
    supports nested access like ``obs.terminal.stdout``.

    ``failable`` names the active fragments **nobody declared required** —
    ``env_sourced - required_fragments`` from the negotiation result.
    Those slots widen to ``Fragment | FragmentFailure``, which is what
    lets one capability's flaky resolve be recorded instead of discarding
    the fragments its peers already captured. Required slots stay narrow
    on purpose: a required fragment that failed must not be representable
    here, because the environment's answer to that is to raise.

    The result is cached: the sole production caller is
    ``EnvironmentBase.observe``, which runs **once per step**, and the set
    is frozen by negotiation before the run starts — so rebuilding the
    class every step was pure waste.

    Registry lookup happens *here*, outside the cache, so the cache key is
    the resolved fragment **classes**, not just their keys. Keying on the
    key set alone meant re-registering a fragment under an existing key
    handed back a model still annotated with the old class, and the next
    ``observe()`` failed with ``Input should be ... an instance of NetA``
    pointing at a class the registry no longer holds.

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
    return _build_observation_model(
        tuple(
            (key, ObservationRegistrar.get(key), key in failable)
            for key in sorted(active_fragments)
        )
    )


@lru_cache(maxsize=64)
def _build_observation_model(
    resolved: tuple[tuple[str, type, bool], ...],
) -> type[BaseModel]:
    """Cached constructor keyed on ``(key, class, may_fail)`` triples.

    ``may_fail`` is part of the cache key, not a detail applied after:
    the same fragment set is a *different model* when one of its slots is
    allowed to carry a :class:`FragmentFailure`, and two runs of one
    process can legitimately negotiate both.
    """
    # No synthetic ``timestamp`` field: nothing ever assigned it, so every
    # step wrote a junk ``NNN_timestamp.json`` containing ``0.0``. Step
    # timing lives on StepRecord, which is actually written by the loop.
    fields: dict[str, Any] = {}

    # Fragment keys are fully qualified (``source.local_id``). The
    # pydantic attribute name is the local segment so agents keep
    # reading ``obs.dom`` / ``obs.terminal``. Cross-source collisions on
    # that local name are rejected by ``negotiate_observation``
    # (``DuplicateFragmentNameError``) before a run starts — this
    # function is reached only with a set that already survived it, and a
    # second copy of the rule here could only ever disagree with the
    # first.
    for key, frag_cls, may_fail in resolved:
        field_name = fragment_field_name(key)
        # Loop-source fragments are filled by the Loop *after* env.observe()
        # via populate_loop_fragments. Make them Optional with default=None
        # so env.observe() can construct the model without these values;
        # the Loop overwrites them before the agent ever sees the
        # observation.
        source = getattr(frag_cls, "__source__", "env")
        if source == "loop":
            fields[field_name] = (frag_cls | None, Field(default=None))
        elif may_fail:
            # Still required — the environment always fills the slot. What
            # widens is *what may be in it*: the capture, or the record of
            # why there is none. Never ``None``, which would read as "this
            # run did not ask for it".
            fields[field_name] = (frag_cls | FragmentFailure, ...)
        else:
            fields[field_name] = (frag_cls, ...)

    return create_model("NegotiatedObservation", **fields)


def build_observation_model_from_result(
    result: ObservationNegotiationResult,
) -> type[BaseModel]:
    """Build the model for one negotiation result.

    The single place that turns "who required what" into "whose slot may
    carry a failure": ``env_sourced - required_fragments``. Loop-sourced
    fragments are excluded because the Loop fills them itself and a Loop
    failure is a Loop-level error, not a missing capture.
    """
    # Read directly: a missing field must be an AttributeError here, not a
    # silent "nothing was required" that widens every slot and invites a
    # reader to treat a FragmentFailure as the fragment it asked for.
    required = frozenset(result.required_fragments)
    return build_observation_model(
        result.active_fragments,
        failable=frozenset(result.env_sourced) - required,
    )


def iter_fragments(obs: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(field_name, fragment)`` for every populated fragment on *obs*.

    Iterates ``type(obs).model_fields`` — accessing ``model_fields`` on an
    *instance* is deprecated in pydantic 2.11 and removed in 3.0.
    """
    model_fields = getattr(type(obs), "model_fields", None)
    if not model_fields:
        return
    for field_name in model_fields:
        value = getattr(obs, field_name, None)
        if value is not None:
            yield field_name, value


def summarize_observation(obs: Any) -> str:
    """One-line digest of an observation, for ``StepRecord.observation_summary``.

    Each fragment contributes its own :meth:`FragmentBase.summary`; the
    ones that return empty are skipped. This is what makes a trace
    skimmable without loading megabytes of DOM per step.
    """
    parts: list[str] = []
    for field_name, fragment in iter_fragments(obs):
        summary_fn = getattr(fragment, "summary", None)
        if not callable(summary_fn):
            continue
        try:
            text = summary_fn()
        except Exception:  # a broken summary must never break a run
            continue
        if text:
            parts.append(f"{field_name}={text}")
    return "; ".join(parts)
