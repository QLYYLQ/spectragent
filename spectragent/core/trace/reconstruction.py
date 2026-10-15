"""Reconstruct recorded observations and message contexts from committed traces.

These readers are shared by branching and training export. They preserve the
trace's recorded types and manifest boundary, while callers decide how to
report missing context for their own domain. This module knows no Loop or
restoration policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import TypeAdapter

from spectragent.core.trace.encoding import snapshot
from spectragent.core.trace.models import RecordedMessage
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.dynamic_model import build_observation_model_from_result
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import StepInfoObs

#: The ``loop.step_info`` key, read to pin a branch's step budget.
STEP_INFO_KEY = "loop.step_info"

_MESSAGE: TypeAdapter[Any] = TypeAdapter(RecordedMessage)


def recorded_form(messages: Sequence[Any]) -> list[Any]:
    """*messages* as a trace records them: the form a live context is compared in.

    A live message takes the recorder's own path — its JSON snapshot, read
    back as the recorded message type — so a live context and a recorded
    prompt compare equal exactly when the recorder would have written the
    same thing for both.
    """
    return [
        _MESSAGE.validate_python(snapshot(message)).model_dump(mode="json") for message in messages
    ]


def first_difference(live: Sequence[Any], recorded: Sequence[Any]) -> str:
    """Where two contexts in :func:`recorded_form` part, for an error message."""
    for index, (mine, theirs) in enumerate(zip(live, recorded, strict=False)):
        if mine != theirs:
            return (
                f"message {index} differs ({mine.get('role')!r}: {_excerpt(mine)} vs "
                f"recorded {theirs.get('role')!r}: {_excerpt(theirs)})"
            )
    return f"{len(live)} messages vs {len(recorded)} recorded"


def _excerpt(message: Any) -> str:
    text = str(message.get("content", ""))
    return repr(text[:80] + ("…" if len(text) > 80 else ""))


def recorded_observation(
    trace: RunTrace, step: int, negotiated: Any, *, keys: Iterable[str] | None = None
) -> tuple[Any | None, list[str]]:
    """*step*'s recorded fragments as *negotiated*'s ``NegotiatedObservation``, or what is missing.

    Reads every fragment of *keys* (default: every fragment the negotiation
    activates) back typed (``FragmentBase.from_trace``); a capture that
    failed comes back as its ``FragmentFailure``, a loop-source fragment the
    step did not record as ``None``. An env-source fragment the step did not
    record — or recorded as a failure where *negotiated* requires it — cannot
    fill its slot: the result is then ``(None, <those keys, sorted>)`` and the
    caller decides whether that is fatal (a branch's checkpoint) and how to
    word it (the training export). Otherwise ``(observation, [])``.

    Raises:
        TraceSchemaError: A file the manifest lists does not read back.
    """
    env_sourced = frozenset(negotiated.env_sourced)
    required = frozenset(negotiated.required_fragments)
    wanted = sorted(negotiated.active_fragments if keys is None else keys)
    values: dict[str, Any] = {}
    unavailable: list[str] = []
    for key in wanted:
        fragment = trace.observation_fragment(step, key)
        if key in env_sourced and (
            fragment is None or (key in required and isinstance(fragment, FragmentFailure))
        ):
            unavailable.append(key)
        values[fragment_field_name(key)] = fragment
    if unavailable:
        return None, unavailable
    return build_observation_model_from_result(negotiated)(**values), []


def recorded_step_info(trace: RunTrace, step: int) -> StepInfoObs | None:
    """The ``loop.step_info`` fragment *step* recorded, or ``None``.

    Raises:
        TraceSchemaError: The fragment's file does not read back.
    """
    fragment = trace.observation_fragment(step, STEP_INFO_KEY)
    return fragment if isinstance(fragment, StepInfoObs) else None
