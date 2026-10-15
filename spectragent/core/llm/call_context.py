"""Run-owned attribution for main and auxiliary model invocations."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CallAttribution:
    caller: str = "agent"
    purpose: str = "agent_decision"
    step: int | None = None


class RunCallContext:
    """One run's explicit attribution scope, with task-local nested values.

    Runtime creates and distributes an instance per execution. There is no
    module-level context variable or implicit process-wide current run.
    """

    def __init__(self) -> None:
        self.context: ContextVar[CallAttribution | None] = ContextVar(
            "model_call_attribution", default=None
        )

    @property
    def current(self) -> CallAttribution:
        value = self.context.get()
        return value if value is not None else CallAttribution()

    @contextmanager
    def scope(
        self, caller: str, purpose: str, step: int | None = None
    ) -> Iterator[CallAttribution]:
        attribution = CallAttribution(
            caller=caller,
            purpose=purpose,
            step=self.current.step if step is None else step,
        )
        token = self.context.set(attribution)
        try:
            yield attribution
        finally:
            self.context.reset(token)
