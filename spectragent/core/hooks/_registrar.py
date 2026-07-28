"""Hook registrar — conscribe-driven auto-registration.

Hooks declare their method interceptions via ``__intercepts__`` (a list of
``Intercept`` objects from ``core.aop.advice``). The AOP Weaver reads these
declarations at runtime to wrap target object methods.

See docs/execution_dag.md M04 for the registrar design.
"""

from __future__ import annotations

from typing import Any, ClassVar

from conscribe import create_registrar

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.errors import MissingHandlerError
from spectragent.core.hooks.base import RuntimeHook

HookRegistrar = create_registrar(
    "hook",
    RuntimeHook,
    discriminator_field="hook_id",
)


class HookBase(metaclass=HookRegistrar.Meta):
    """Concrete base for all hook implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"trace_writer"``)
    - ``__intercepts__``: list of ``Intercept`` declarations

    Subclasses may override:
    - ``blocking`` property (default: True)
    - ``on_run_start/end/error`` lifecycle callbacks
    - Handler methods named ``{advice}_{method}`` matching ``__intercepts__``

    Example::

        class MetricsHook(HookBase):
            __registry_key__ = "metrics"
            __intercepts__ = [
                Intercept("ainvoke", role="llm", advice="after"),
            ]

            @property
            def blocking(self) -> bool:
                return True  # metrics accumulation is cheap

            async def after_ainvoke(self, result, *, args, kwargs, **ctx):
                self.usage.add(result.usage)
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __intercepts__: ClassVar[list[Intercept]] = []

    # Lifecycle dispatch order, ascending. Deliberately a plain ClassVar
    # and not a conscribe config field: ordering is a property of what the
    # hook *does* (enrich vs. serialize), not a knob a run should have to
    # get right in YAML. Enrichers sit below DEFAULT, writers above.
    PRIORITY_ENRICH: ClassVar[int] = 10
    PRIORITY_DEFAULT: ClassVar[int] = 100
    PRIORITY_PERSIST: ClassVar[int] = 900
    priority: ClassVar[int] = 100

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Fail fast at class creation if an Intercept's handler is missing.

        ``__intercepts__`` declares ``(role, method, advice)`` tuples; the
        Weaver later looks up ``{advice}_{method}`` on the hook instance.
        Catching the typo at class creation (instead of at weave time)
        keeps the error close to the source of the mistake.
        """
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__") is True:
            return
        for intercept in cls.__intercepts__:
            handler = getattr(cls, intercept.handler_name, None)
            if not callable(handler):
                advice_val = (
                    intercept.advice.value
                    if isinstance(intercept.advice, AdviceType)
                    else intercept.advice
                )
                raise MissingHandlerError(
                    hook_cls=cls.__name__,
                    method=intercept.method,
                    advice=advice_val,
                    handler_name=intercept.handler_name,
                )

    @property
    def hook_id(self) -> str:
        return self.__registry_key__

    @property
    def blocking(self) -> bool:
        return True

    # Default no-op lifecycle callbacks
    async def on_run_start(self, **kwargs: Any) -> None:
        pass

    async def on_run_end(self, **kwargs: Any) -> None:
        pass

    async def on_run_error(self, **kwargs: Any) -> None:
        pass
