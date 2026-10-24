"""Loop-source observation fragment population.

After ``Environment.observe()`` returns the env-source half of the
NegotiatedObservation, the Loop fills in the loop-source fragments it
owns (task, step_info, history, action_result, ...). This helper is
the seam between those two halves.

The builders themselves are :func:`loop_fragment_builders`, a pure function
of explicit inputs with three callers (arch §4.1 "一个 run 与一步"):
``ReactLoop`` hands it the live run's values, ``BranchLoop`` the step budget
its source run recorded at the checkpoint (:func:`pinned_step_info`), and the
training export's student renderer the values a recorded step was shown.

See ``docs/architecture_design.md`` §4.4 (negotiation) and
``tests/test_core/test_loops/test_react_loop_fragments.py`` for the executable
contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from spectragent.schemas.observation._registrar import (
    FragmentBase,
    fragment_field_name,
)
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    HistoryObs,
    StepInfoObs,
    TaskObs,
)
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
    step, picks the last one that returned — that's the action whose result
    the LLM perceives as "what happened just now". It is paired with its own
    action, not the step's last: the actions after it (skipped after a
    failure, after ``done``, or after one that raised) have no result.
    """
    if prev_step is None or not prev_step.actions or not prev_step.action_results:
        return ActionResultObs(last_action="", success=True, error=None)
    last_result = prev_step.action_results[-1]
    last_action = prev_step.actions[len(prev_step.action_results) - 1]
    return ActionResultObs(
        last_action=last_action.name,
        success=bool(last_result.success),
        error=last_result.error,
    )


def loop_fragment_builders(
    *,
    task: str,
    step: int,
    max_steps: int,
    today: str,
    history: Sequence[StepRecord],
    prev_step: StepRecord | None,
) -> dict[str, Callable[[], FragmentBase]]:
    """Builders for every loop-source fragment a ReAct step is shown.

    ``populate_loop_fragments`` only invokes the builders whose keys are in
    the agent's negotiated ``loop_sourced`` set, so an unused one costs
    nothing.

    Args:
        task: The task text (``loop.task``).
        step: The step's index; ``loop.step_info`` tells the agent ``step + 1``.
        max_steps: The step budget the agent is told.
        today: The ISO date the agent is told.
        history: The earlier steps (``loop.history``), read when the builder
            runs — a Loop passes its live list.
        prev_step: The step before this one (``loop.action_result``), or
            ``None`` on a first step.
    """
    return {
        "loop.task": lambda: TaskObs(user_request=task),
        "loop.step_info": lambda: StepInfoObs(step=step + 1, max_steps=max_steps, today=today),
        "loop.history": lambda: HistoryObs(items=list(history)),
        "loop.action_result": lambda: last_action_result_obs(prev_step),
    }


#: The loop-source fragment keys :func:`loop_fragment_builders` builds — read
#: off the function itself, so the two cannot drift. ``AgentLoopBase`` declares
#: them as every Loop's default ``__loop_fragments__``: what negotiation may
#: activate on the Loop's side (``docs/architecture_design.md`` §4.4).
LOOP_FRAGMENT_KEYS: frozenset[str] = frozenset(
    loop_fragment_builders(task="", step=0, max_steps=0, today="", history=(), prev_step=None)
)


def pinned_step_info(recorded: StepInfoObs, *, today_override: str | None) -> tuple[int, str]:
    """``(max_steps, today)`` a re-told step is shown: what it recorded, ``today_override`` winning.

    A branch's policy step and an exported step are told the budget and the
    date their source step was told, so the decision is made — and learned —
    on the prompt the source made it on.
    """
    return recorded.max_steps, today_override or recorded.today
