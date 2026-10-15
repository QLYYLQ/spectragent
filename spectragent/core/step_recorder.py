"""StepRecorder — the seam where a Loop surrenders its finished StepRecord.

A Loop already knows everything a trace needs: which actions it parsed,
what they returned, and what the agent saw. The recording layer adds measured usage.
Before this existed that knowledge died in a local variable, and the WAL
reconstructed a two-key skeleton (``{step, llm_output}``) by intercepting
``llm.ainvoke`` — a call that is neither one-per-step nor aware of
anything that happens after it.

The fix is a Protocol method, per the framework's own rule: *if a
semantic event has no corresponding method, model it as a method — do
not add an emitter sidechannel.* ``StepRecorder`` is that method's
owner. ``AgentRuntime`` constructs one per run, the Weaver wraps it under
``role="loop"``, and the Loop calls it once per completed step. Hooks
intercept ``record_step`` like any other method; the Loop stays
completely unaware that anyone is listening.

It doubles as the answer to ``on_run_error(partial_steps=...)``: the
records the Loop surrendered before it died are, by construction,
exactly the partial trajectory.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.step_record import StepRecord


@runtime_checkable
class StepSink(Protocol):
    """Contract for the object a Loop hands each completed step to."""

    @property
    def records(self) -> list[StepRecord]:
        """A snapshot of surrendered execution records in order."""
        ...

    @property
    def policy_step_count(self) -> int:
        """The number of surrendered policy decisions."""
        ...

    @property
    def last_observation(self) -> Any:
        """Most recent surrendered observation, or None."""
        ...

    def clear(self) -> None:
        """Start a fresh execution's in-memory record history."""
        ...

    async def record_step(
        self,
        record: StepRecord,
        *,
        observation: Any = None,
    ) -> None:
        """Accept one completed step.

        Args:
            record: The finished :class:`StepRecord`.
            observation: The observation the agent actually saw this step
                — i.e. *after* loop-source fragments were populated, which
                is a different object from ``env.observe()``'s return
                value. Optional so env-less loops can still record.

        """
        ...


class StepRecorder:
    """Default :class:`StepSink`: keeps the run's records in memory.

    Deliberately does no I/O. Persistence is a hook's job (``TraceWriter``
    intercepts ``record_step``), which keeps a run with no hooks
    configured working exactly the same, just without files.
    """

    def __init__(self) -> None:
        self._records: list[StepRecord] = []
        self._last_observation: Any = None

    @property
    def records(self) -> list[StepRecord]:
        """Steps surrendered so far, in order."""
        return list(self._records)

    @property
    def policy_step_count(self) -> int:
        """Surrendered steps the run's own agent decided — what ``AgentMetrics.steps`` counts.

        A branch run's restored steps and a terminal observation are records
        of the run, not decisions of its agent (``StepRecord.origin``).
        """
        return sum(1 for record in self._records if record.origin == "policy")

    @property
    def last_observation(self) -> Any:
        """The most recent observation handed over with a step."""
        return self._last_observation

    async def record_step(
        self,
        record: StepRecord,
        *,
        observation: Any = None,
    ) -> None:
        self._records.append(record)
        if observation is not None:
            self._last_observation = observation

    def clear(self) -> None:
        self._records.clear()
        self._last_observation = None
