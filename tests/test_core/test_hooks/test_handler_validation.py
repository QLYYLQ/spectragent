"""Tests for ``HookBase.__init_subclass__`` handler validation."""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import MissingHandlerError
from spectragent.core.hooks._registrar import HookBase, HookRegistrar


class TestHandlerValidation:
    """Declaring ``__intercepts__`` with a missing handler must fail fast."""

    def test_missing_handler_raises_at_class_creation(self) -> None:
        with pytest.raises(MissingHandlerError) as exc:

            class BrokenHook(HookBase):
                __registry_key__ = "test_broken_hook"
                __intercepts__ = [Intercept("observe", role="env", advice="after")]

        assert exc.value.hook_cls == "BrokenHook"
        assert exc.value.method == "observe"
        assert exc.value.advice == "after"
        assert exc.value.handler_name == "after_observe"
        assert "after_observe" in str(exc.value)

    def test_typo_in_handler_name_detected(self) -> None:
        """A typo like ``after_obseve`` (missing 'r') must raise."""
        with pytest.raises(MissingHandlerError):

            class TypoHook(HookBase):
                __registry_key__ = "test_typo_hook"
                __intercepts__ = [Intercept("observe", role="env", advice="after")]

                async def after_obseve(self, result, *, args, kwargs, **ctx):  # noqa
                    pass

    def test_abstract_subclass_skips_validation(self) -> None:
        """``__abstract__ = True`` intermediate classes bypass the check."""

        class AbstractHook(HookBase):
            __abstract__ = True
            __intercepts__ = [Intercept("observe", role="env", advice="after")]

        # No error — handler may come from the concrete subclass.
        assert AbstractHook.__intercepts__

    def test_handler_inherited_from_parent_satisfies_check(self) -> None:
        class ParentHook(HookBase):
            __abstract__ = True

            async def after_observe(self, result, *, args, kwargs, **ctx):
                pass

        class ChildHook(ParentHook):
            __registry_key__ = "test_child_inherit_hook"
            __intercepts__ = [Intercept("observe", role="env", advice="after")]

        assert HookRegistrar.get("test_child_inherit_hook") is ChildHook
        HookRegistrar.unregister("test_child_inherit_hook")

    def test_multiple_intercepts_all_validated(self) -> None:
        """If any one handler is missing, validation fails."""
        with pytest.raises(MissingHandlerError) as exc:

            class PartialHook(HookBase):
                __registry_key__ = "test_partial_hook"
                __intercepts__ = [
                    Intercept("ainvoke", role="llm", advice="after"),
                    Intercept("observe", role="env", advice="before"),
                ]

                async def after_ainvoke(self, result, *, args, kwargs, **ctx):
                    pass

                # before_observe intentionally missing

        assert exc.value.handler_name == "before_observe"
