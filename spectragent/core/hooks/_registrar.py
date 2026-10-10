"""Hook registrar — conscribe-driven auto-registration.

Hooks declare their method interceptions via ``__intercepts__`` (a list of
``Intercept`` objects from ``core.aop.advice``). The AOP Weaver reads these
declarations at runtime to wrap target object methods.

See docs/execution_dag.md M04 for the registrar design.

.. note::
   This module deliberately does **not** use ``from __future__ import
   annotations``, for the same reason ``capabilities/_registrar.py``
   doesn't: every ``Annotated[...]`` a hook declares must stay a real
   object so the ``__init__`` of a subclass — dataclass-generated (whose
   ``__globals__`` belong to *its* module) or hand-written — carries a
   resolvable annotation for conscribe's config extractor.
"""

from dataclasses import dataclass
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


# ``eq=False``: a hook is a stateful object, compared by identity. A
# subclass with a hand-written ``__init__`` (``TraceWriter``) would
# otherwise inherit a field-less ``__eq__`` under which every two
# instances are "equal" — and be unhashable.
@dataclass(eq=False)
class HookBase(metaclass=HookRegistrar.Meta):
    """Concrete base for all hook implementations.

    A ``@dataclass(eq=False)``; every other registrar base is a dataclass
    too (actions, capabilities, agents, loaders, evaluators). Before
    this, hooks were the one layer that hand-rolled
    ``__init__(self, **kwargs)``: conscribe reads
    ``inspect.signature(cls.__init__)``, found no named parameter, and
    emitted a discriminator-only model with ``extra="forbid"`` — so
    ``TraceWriter.trace_dir`` was not merely ignored from YAML, it was a
    hard validation error. What conscribe needs is a *named* ``__init__``
    parameter per knob; the two shapes below both give it one.

    ``eq=False`` because a hook is a stateful object, compared by
    identity: code that must tell hooks apart (archive negotiation
    comparing consumers) uses ``is``. Without it, a subclass that is not
    itself a dataclass would inherit a field-less ``__eq__`` under which
    every two instances are equal.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"trace_writer"``)
    - ``__intercepts__``: list of ``Intercept`` declarations

    Subclasses may override:
    - ``blocking`` property (default: True)
    - ``on_run_start/end/error`` lifecycle callbacks
    - Handler methods named ``{advice}_{method}`` matching ``__intercepts__``

    **A hook with knobs takes one of two shapes**, chosen by one question:
    does it carry a ``__wiring__`` receptor?

    - **No receptor** (``metrics``): decorate the subclass with
      ``@dataclass`` too, declare its knobs as ``Annotated`` fields and its
      internals as ``field(init=False, repr=False)``. The synthesised
      ``__init__`` makes each knob a named parameter. Example below.
    - **A receptor** (``trace_writer``, whose ``archive`` becomes one in
      M29): **not** a ``@dataclass``. conscribe's metaclass decides whether
      a ``__wiring__`` entry is already a constructor parameter by reading
      ``inspect.signature(cls.__init__)`` at class creation — before
      ``@dataclass`` has synthesised anything — so a receptor that is a
      dataclass field looks unreceived, gets a ``WiredField`` installed
      over its default, and loses its optionality. Hand-write ``__init__``:
      every knob a named ``Annotated`` parameter, and the receptor's
      class-level annotation on a plain mixin (``list[...]`` for a list
      receptor — the Composer reads the class annotation to choose list,
      dict or single injection). ``TraceWriter`` is the worked case.
      ``tests/test_core/test_capabilities/test_receptor_hygiene.py`` (Rule
      3) fails a receptor that is a parameter of a synthesised
      ``__init__``.

    Example (no receptor)::

        @dataclass
        class MetricsHook(HookBase):
            __registry_key__ = "metrics"
            __intercepts__ = [
                Intercept("ainvoke", role="llm", advice="after"),
            ]

            window: Annotated[int, Field(description="Rolling window")] = 10

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
        """Wait for this hook's handlers — see :class:`RuntimeHook`.

        True by default because a hook that produces run output must
        finish before the output is read, and a ``blocking=False``
        handler runs after the WAL commit point and therefore reaches no
        artifact. Override to False only for a side channel nothing
        downstream reads.
        """
        return True

    # Default no-op lifecycle callbacks
    async def on_run_start(self, **kwargs: Any) -> None:
        pass

    async def on_run_end(self, **kwargs: Any) -> None:
        pass

    async def on_run_error(self, **kwargs: Any) -> None:
        pass
