"""Hook registrar — conscribe-driven auto-registration.

Hooks declare their method interceptions via ``__intercepts__`` (a list of
``Intercept`` objects from ``core.aop.advice``). The AOP Weaver reads these
declarations at runtime to wrap target object methods.

See docs/execution_dag.md M04 for the registrar design.
"""

from __future__ import annotations

from typing import Any, ClassVar

from conscribe import create_registrar

from spectragent.core.aop.advice import Intercept
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
