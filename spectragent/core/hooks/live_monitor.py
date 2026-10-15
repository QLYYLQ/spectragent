"""Observe execution in memory; terminals and other consumers render independently."""

from typing import Any

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.monitoring.protocols import ProgressSink


class LiveMonitor(HookBase):
    """A small blocking observer that never performs rendering, network or file I/O.

    Around advice settles each individual call even on failure or cancellation.
    The state is transient: only RunTrace can say which steps were committed.
    """

    __registry_key__ = "live_monitor"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("record_step", role="loop", advice="around"),
        Intercept("run", role="agent", advice="around"),
    ]

    def __init__(self) -> None:
        self.progress: ProgressSink | None = None
        self._starting: int | None = None
        object.__setattr__(self, "__intercepts__", list(type(self).__intercepts__))

    def bind(self, progress: ProgressSink, *, environment: bool) -> None:
        self.progress = progress
        intercepts = list(type(self).__intercepts__)
        if environment:
            intercepts += [
                Intercept("observe", role="env", advice="around"),
                Intercept("act", role="env", advice="around"),
            ]
        # HookBase's declaration is a ClassVar. This monitor deliberately
        # shadows it per instance before Weaver reads it, without modifying
        # the shared declaration or weakening validation for other hooks.
        object.__setattr__(self, "__intercepts__", intercepts)

    def _capture(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Display observation cannot change a model or environment outcome.
        # This never performs I/O (including logging); the trace remains the
        # source of truth if a transient sink cannot accept a measurement.
        if self.progress is None:
            return None
        try:
            return getattr(self.progress, method)(*args, **kwargs)
        except Exception:
            return None

    async def on_run_start(self, **kwargs: Any) -> None:
        if self.progress is not None:
            self._capture(
                "run_started",
                max_steps=getattr(kwargs.get("loop"), "max_steps", None),
                continue_steps=getattr(kwargs.get("loop"), "continue_steps", None),
                branch=kwargs.get("branch"),
                **{
                    key: kwargs.get(key)
                    for key in ("run_id", "task_id", "agent_id", "loop_id", "llm_model")
                },
            )
            self._starting = self._capture("begin", "starting run")

    async def on_run_end(self, *, result: Any, **kwargs: Any) -> None:
        if self.progress is not None:
            if self._starting is not None:
                self._capture("end", self._starting)
                self._starting = None
            self._capture("run_ended", result)

    async def _observe(self, phase: str, proceed: Any, detail: str = "") -> Any:
        progress = self.progress
        if progress is None:
            return await proceed()
        token = self._capture("begin", phase, detail)
        error = None
        try:
            return await proceed()
        except BaseException as exc:
            error = exc
            raise
        finally:
            if token is not None:
                self._capture("end", token, error=error)

    async def around_run(self, *, proceed: Any, **kwargs: Any) -> Any:
        if self.progress is not None and self._starting is not None:
            self._capture("end", self._starting)
            self._starting = None
        return await self._observe("running agent", proceed)

    async def around_observe(self, *, proceed: Any, **kwargs: Any) -> Any:
        return await self._observe("observing", proceed)

    async def around_act(self, *, proceed: Any, args: tuple, **kwargs: Any) -> Any:
        action = args[0] if args else None
        return await self._observe("acting", proceed, str(getattr(action, "name", "")))

    async def around_ainvoke(self, *, proceed: Any, **kwargs: Any) -> Any:
        try:
            result = await self._observe("waiting for model", proceed)
        except BaseException as exc:
            if self.progress is not None:
                self._capture("called", getattr(exc, "usage", None))
            raise
        if self.progress is not None:
            self._capture("called", getattr(result, "usage", None))
        return result

    async def around_record_step(
        self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any
    ) -> Any:
        result = await self._observe("recording step", proceed)
        record = args[0] if args else kwargs.get("record")
        if self.progress is not None and record is not None:
            self._capture("recorded", record)
        return result
