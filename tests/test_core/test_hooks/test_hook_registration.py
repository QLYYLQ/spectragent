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
