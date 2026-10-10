"""Tests for Hook registrar and base class."""

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase, HookRegistrar


class TestHookRegistration:
    """Hook subclasses auto-register via conscribe metaclass."""

    def test_register_concrete_hook(self) -> None:
        class DummyHook(HookBase):
            __registry_key__ = "test_dummy_hook"
            __intercepts__ = [
                Intercept("observe", role="env", advice="after"),
            ]

            async def after_observe(self, result, *, args, kwargs, **ctx):
                pass

        assert HookRegistrar.get("test_dummy_hook") is DummyHook
        HookRegistrar.unregister("test_dummy_hook")

    def test_abstract_not_registered(self) -> None:
        class AbstractHook(HookBase):
            __abstract__ = True

        assert HookRegistrar.get_or_none("abstract_hook") is None

    def test_hook_id_property(self) -> None:
        class IdHook(HookBase):
            __registry_key__ = "test_id_hook"

        instance = IdHook()
        assert instance.hook_id == "test_id_hook"
        HookRegistrar.unregister("test_id_hook")

    def test_blocking_default_true(self) -> None:
        class BlockHook(HookBase):
            __registry_key__ = "test_block_hook"

        instance = BlockHook()
        assert instance.blocking is True
        HookRegistrar.unregister("test_block_hook")

    def test_blocking_override(self) -> None:
        class NonBlockHook(HookBase):
            __registry_key__ = "test_nonblock_hook"

            @property
            def blocking(self) -> bool:
                return False

        instance = NonBlockHook()
        assert instance.blocking is False
        HookRegistrar.unregister("test_nonblock_hook")

    def test_intercepts_default_empty(self) -> None:
        class EmptyHook(HookBase):
            __registry_key__ = "test_empty_hook"

        assert EmptyHook.__intercepts__ == []
        HookRegistrar.unregister("test_empty_hook")

    def test_intercepts_declared(self) -> None:
        class IntHook(HookBase):
            __registry_key__ = "test_int_hook"
            __intercepts__ = [
                Intercept("ainvoke", role="llm", advice="after"),
                Intercept("observe", role="env", advice="before"),
            ]

            async def after_ainvoke(self, result, *, args, kwargs, **ctx):
                pass

            async def before_observe(self, *, args, kwargs, **ctx):
                pass

        assert len(IntHook.__intercepts__) == 2
        HookRegistrar.unregister("test_int_hook")


class TestHookConfigIsExtractable:
    """A hook's ``Annotated`` knobs must survive into the generated config.

    ``HookBase`` was the framework's one non-dataclass registrar base, so
    hooks hand-rolled ``__init__(self, **kwargs)``. conscribe reads
    ``inspect.signature(cls.__init__)``, found no named parameter and
    emitted a discriminator-only model with ``extra="forbid"`` — putting
    the knob in YAML was a hard ``extra_forbidden`` validation error, and
    passing it to the constructor was a ``TypeError`` from
    ``object.__init__``.

    What conscribe needs is a *named* parameter per knob, and a hook gets
    one in either of two shapes (``HookBase``'s docstring): a hook without
    a ``__wiring__`` receptor is a ``@dataclass`` whose knobs are fields; a
    hook that carries a receptor hand-writes ``__init__`` — a receptor on a
    dataclass-synthesised ``__init__`` loses its default to conscribe's
    ``WiredField`` (``test_receptor_hygiene.py`` Rule 3) — and its knobs are
    named ``Annotated`` parameters there. Either way, a parameter that is
    not ``Annotated`` stays out of the config (``__config_annotated_only__``).
    """

    def test_annotated_field_becomes_a_config_field(self) -> None:
        """The dataclass shape — for a hook that carries no receptor."""
        from dataclasses import dataclass, field
        from typing import Annotated

        from conscribe.config.extractor import extract_config_schema
        from pydantic import Field

        @dataclass
        class KnobHook(HookBase):
            __registry_key__ = "test_knob_hook"

            where: Annotated[str, Field(description="Where to write")] = "here"
            _state: list = field(default_factory=list, init=False, repr=False)

        try:
            schema = extract_config_schema(KnobHook)
            assert schema is not None
            assert "where" in schema.model_fields
            # ...and the constructor actually accepts it.
            assert KnobHook(where="/tmp/x").where == "/tmp/x"
            assert KnobHook()._state == []
        finally:
            HookRegistrar.unregister("test_knob_hook")

    def test_a_hand_written_init_hook_exposes_only_its_annotated_params(self) -> None:
        """The hand-written shape: named ``Annotated`` parameters in, a plain one out."""
        from typing import Annotated

        from conscribe.config.extractor import extract_config_schema
        from pydantic import Field

        from spectragent.core.hooks.trace_writer import TraceWriter

        class HandWrittenHook(HookBase):
            __registry_key__ = "test_hand_written_hook"

            def __init__(
                self,
                where: Annotated[str, Field(description="Where to write")] = "here",
                sinks: list | None = None,
            ) -> None:
                super().__init__()
                self.where = where
                self.sinks = list(sinks or [])

        try:
            schema = extract_config_schema(HandWrittenHook)
            assert schema is not None
            assert set(schema.model_fields) == {"where"}
            hook = HandWrittenHook(where="/tmp/x", sinks=[1])
            assert (hook.where, hook.sinks) == ("/tmp/x", [1])
        finally:
            HookRegistrar.unregister("test_hand_written_hook")

        # The worked case: TraceWriter's knobs in, its plain ``archive`` out.
        trace_schema = extract_config_schema(TraceWriter)
        assert trace_schema is not None
        assert set(trace_schema.model_fields) == {"trace_dir", "record_secrets"}

    def test_trace_writer_exposes_trace_dir(self) -> None:
        from spectragent.generated._composed import TraceWriterHookConfig

        assert "trace_dir" in TraceWriterHookConfig.model_fields
        assert TraceWriterHookConfig(trace_dir="/tmp/t").trace_dir == "/tmp/t"
