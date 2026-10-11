"""One run of a Loop: the dependencies it drives and the books every Loop keeps.

``AgentLoopBase.weave`` stores nothing on the Loop — a run's dependencies
arrive per call in ``ctx`` — so everything a run accumulates has to live in
one object per ``_execute`` call. :class:`LoopRun` is that object. Nothing in
it is specific to one execution strategy: which environment and model a run
drives, where its finished steps go, and the books any Loop keeps — when it
started, the steps it built, the actions it executed, how many model calls it
made, the step before this one, the best answer so far, how many steps in a
row could not be parsed, and its token ledger.

A Loop builds one with :meth:`LoopRun.from_ctx` at the top of ``_execute``
and threads it through its steps (``ReactLoop._run_step``); a Loop with more
per-run state subclasses it (``BranchLoop``'s ``BranchRun``). See
``docs/architecture_design.md`` §4.1 "一个 run 与一步".
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from spectragent.schemas.step_record import StepRecord


@dataclass(kw_only=True)
class LoopRun:
    """The dependencies of one run and the books every Loop keeps.

    Attributes:
        agent: The (woven) agent whose slots the Loop calls.
        task: The task text.
        env: The (woven) environment — ``observe`` / ``act`` and friends.
        llm: The (woven) model.
        memory: The run's message memory, already seeded by the runtime.
        act_fn: ``guarded_act`` — the only way an action reaches ``env``.
        negotiated_obs: The observation negotiation result (``None`` only in
            narrow unit tests that weave by hand).
        task_id: Dataset identity of the task.
        recorder: The framework's step sink (``record_step``), or ``None`` in
            narrow unit tests that weave a loop by hand: the run is then
            identical, just unrecorded.
        model_id: The model identifier results are attributed to.
        usage: The Loop's token ledger. Its type is the Loop's own (ReactLoop
            puts its ``_UsageTally`` here); this module never reads it.
        start_time: ``time.monotonic()`` when the run started.
        step_records: Every record the run built, in order — policy,
            restored and terminal alike.
        action_history: ``name(params)`` of every action the run's own agent
            executed.
        llm_calls: Model calls made (billed ones, including refused and
            truncated replies).
        prev_step_record: The record of the step before the one in progress.
        last_answer: The best answer seen so far, carried onto every ending.
        parse_error_streak: Consecutive steps whose reply could not be parsed.
    """

    agent: Any
    task: str
    env: Any
    llm: Any
    memory: Any
    act_fn: Any
    negotiated_obs: Any
    task_id: str
    recorder: Any
    model_id: str
    usage: Any
    start_time: float = field(default_factory=time.monotonic)
    step_records: list[StepRecord] = field(default_factory=list)
    action_history: list[str] = field(default_factory=list)
    llm_calls: int = 0
    prev_step_record: StepRecord | None = None
    last_answer: str = ""
    parse_error_streak: int = 0

    @classmethod
    def from_ctx(
        cls,
        agent: Any,
        task: str,
        ctx: Mapping[str, Any],
        *,
        model_id: str,
        usage: Any,
        **state: Any,
    ) -> Self:
        """Start a run from the ``ctx`` that ``weave`` hands ``_execute``.

        ``env`` / ``llm`` / ``memory`` / ``act_fn`` are required; the rest
        default the way a hand-woven unit test needs them to. *state* is a
        subclass's own fields.
        """
        return cls(
            agent=agent,
            task=task,
            env=ctx["env"],
            llm=ctx["llm"],
            memory=ctx["memory"],
            act_fn=ctx["act_fn"],
            negotiated_obs=ctx.get("negotiated_obs"),
            task_id=ctx.get("task_id", "unknown"),
            recorder=ctx.get("step_recorder"),
            model_id=model_id,
            usage=usage,
            **state,
        )

    @property
    def policy_steps(self) -> int:
        """Steps the run's own agent decided — what ``AgentMetrics.steps`` counts.

        Restored steps (a branch run's re-executed prefix) and the terminal
        observation are records of the run, not decisions of its agent.
        """
        return sum(1 for record in self.step_records if record.origin == "policy")
