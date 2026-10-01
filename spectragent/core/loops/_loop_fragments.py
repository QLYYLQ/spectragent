"""Loop-source observation fragment population.

After ``Environment.observe()`` returns the env-source half of the
NegotiatedObservation, the Loop fills in the loop-source fragments it
owns (task, step_info, history, action_result, ...). This helper is
the seam between those two halves.

See ``docs/architecture_design.md`` §4.4 (negotiation) and
``tests/test_core/test_loops/test_react_loop_fragments.py`` for the executable
contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from spectragent.schemas.observation._registrar import (
    FragmentBase,
    fragment_field_name,
)
from spectragent.schemas.observation.fragments import ActionResultObs
from spectragent.schemas.step_record import StepRecord


def populate_loop_fragments(
    obs: BaseModel,
    *,
    loop_sourced: frozenset[str],
    builders: Mapping[str, Callable[[], FragmentBase]],
) -> BaseModel:
    """Return *obs* with loop-source fragments injected.

    For each key in *loop_sourced*, calls the matching builder and writes
    the result into the field whose name matches the key's local segment
    (e.g. ``"loop.task"`` → ``obs.task``). Keys not present in
    *loop_sourced* are skipped — the agent didn't request them.

    Raises:
        KeyError: if a key in *loop_sourced* has no registered builder.
    """
    if not loop_sourced:
        return obs
    update: dict[str, Any] = {}
    for key in loop_sourced:
        if key not in builders:
            raise KeyError(
                f"No builder registered for loop-source fragment {key!r}; "
                f"available builders: {sorted(builders)!r}"
            )
        local_name = fragment_field_name(key)
        update[local_name] = builders[key]()
    return obs.model_copy(update=update)


def last_action_result_obs(prev_step: StepRecord | None) -> ActionResultObs:
    """Build an ActionResultObs from the most recent completed StepRecord.

    On step 0 (no prior step) returns a sentinel with ``last_action=""``
    and ``success=True``. When multiple actions ran in the previous
    step, picks the last one — that's the action whose result the LLM
    perceives as "what happened just now".
    """
    if prev_step is None or not prev_step.actions or not prev_step.action_results:
        return ActionResultObs(last_action="", success=True, error=None)
    last_action = prev_step.actions[-1]
    last_result = prev_step.action_results[-1]
    return ActionResultObs(
        last_action=last_action.name,
        success=bool(last_result.success),
        error=last_result.error,
    )
