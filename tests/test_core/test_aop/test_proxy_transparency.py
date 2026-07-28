"""AOPProxy must be transparent, and the Weaver must not fail silently.

Four bugs are pinned here:

* ``proxy.attr = x`` landing in the proxy's own ``__dict__``, permanently
  shadowing the wrapped method — which is how ``Loop.weave()``'s
  ``agent.run = run`` made ``Intercept("run", role="agent")`` unreachable.
* Method resolution snapshotted at wrap time, so anything installed
  afterwards was never intercepted.
* A hook declaring a role nobody wraps registering cleanly, appearing in
  ``intercepted_methods``, and never firing — with no log and no error.
* The inverse: a wrap-time "no such attribute" warning declaring a
  working ``agent.run`` hook dead, because it judged a lazily resolved
  method before ``Loop.weave()`` had installed it.
"""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import UnclaimedRoleError
from spectragent.core.aop.proxy import AOPProxy
from spectragent.core.aop.weaver import Weaver


class _Target:
    def __init__(self):
        self.attr = "original"

    async def observe(self, *a, **kw):
        return "observed"


class _Hook:
    hook_id = "spy"
    blocking = True
    __intercepts__ = [Intercept("observe", role="env", advice="after")]

    def __init__(self, seen: list):
        self.seen = seen

    async def after_observe(self, result, *, args, kwargs, **ctx):
        self.seen.append((result, ctx.get("role"), ctx.get("method")))


class _RunHook:
    hook_id = "run_spy"
    blocking = True
    __intercepts__ = [Intercept("run", role="agent", advice="after")]

    def __init__(self, seen: list):
        self.seen = seen

    async def after_run(self, result, *, args, kwargs, **ctx):
        self.seen.append(result)


class _CapabilityRoleHook:
    hook_id = "cap_spy"
    blocking = True
    __intercepts__ = [Intercept("resolve_fragment", role="capability", advice="after")]

    async def after_resolve_fragment(self, result, *, args, kwargs, **ctx):
        pass


class TestProxyWritesPassThrough:
    def test_setattr_lands_on_target(self):
        target = _Target()
        proxy = AOPProxy(target, {}, role="env")
        proxy.attr = "written"
        assert target.attr == "written"
        assert "attr" not in proxy.__dict__

    def test_delattr_lands_on_target(self):
        target = _Target()
        proxy = AOPProxy(target, {}, role="env")
        del proxy.attr
        assert not hasattr(target, "attr")

    async def test_method_installed_after_wrap_is_intercepted(self):
        """This is the ``agent.run = run`` case, from Loop.weave()."""
        seen: list = []
        target = _Target()
        weaver = Weaver([_RunHook(seen)])
        proxy = weaver.wrap(target, role="agent")

        async def run(task):
            return f"ran:{task}"

        # Exactly what Loop.weave does — through the proxy.
        proxy.run = run

        assert await proxy.run("t") == "ran:t"
        assert seen == ["ran:t"]

    async def test_rebinding_a_method_rebuilds_the_wrapper(self):
        seen: list = []
        target = _Target()
        proxy = Weaver([_RunHook(seen)]).wrap(target, role="agent")

        async def first(task):
            return "first"

        async def second(task):
            return "second"

        proxy.run = first
        await proxy.run("t")
        proxy.run = second
        await proxy.run("t")
        assert seen == ["first", "second"]


class TestAdviceContext:
    async def test_handler_receives_target_role_method(self):
        seen: list = []
        target = _Target()
        proxy = Weaver([_Hook(seen)]).wrap(target, role="env")
        await proxy.observe()
        assert seen == [("observed", "env", "observe")]


class TestWeaverRoleValidation:
    def test_unclaimed_role_raises(self):
        weaver = Weaver([_CapabilityRoleHook()])
        weaver.wrap(_Target(), role="env")
        with pytest.raises(UnclaimedRoleError) as exc:
            weaver.raise_for_unclaimed_roles()
        assert "capability" in str(exc.value)
        assert "cap_spy" in str(exc.value)

    def test_claimed_roles_pass(self):
        seen: list = []
        weaver = Weaver([_Hook(seen)])
        weaver.wrap(_Target(), role="env")
        weaver.raise_for_unclaimed_roles()  # must not raise

    def test_none_target_still_claims_the_role(self):
        """An env-less run must not trip the unclaimed-role check."""
        seen: list = []
        weaver = Weaver([_Hook(seen)])
        assert weaver.wrap(None, role="env") is None
        weaver.raise_for_unclaimed_roles()

    def test_wrapping_does_not_prejudge_a_method_weave_installs(self, caplog):
        """No agent has ``run`` until ``Loop.weave()`` puts it there.

        Warning at wrap time calls the framework's flagship intercept
        dead on every single run — and it is wrong, as the tests above
        prove by firing the handler.
        """
        weaver = Weaver([_RunHook([])])
        with caplog.at_level("WARNING"):
            weaver.wrap(_Target(), role="agent")
        assert [rec.message for rec in caplog.records] == []

    async def test_missing_target_method_is_logged_when_asked_for(self, caplog):
        class _NoObserve:
            pass

        proxy = Weaver([_Hook([])]).wrap(_NoObserve(), role="env")
        assert not caplog.records  # nothing decided at wrap time

        with caplog.at_level("WARNING"), pytest.raises(AttributeError):
            await proxy.observe()

        messages = [rec.message for rec in caplog.records]
        assert any("observe" in m for m in messages)
        assert any("spy" in m for m in messages)

    def test_missing_target_method_is_reported_once(self, caplog):
        class _NoObserve:
            pass

        proxy = Weaver([_Hook([])]).wrap(_NoObserve(), role="env")
        with caplog.at_level("WARNING"):
            # The silent shape: a default swallows the AttributeError, so
            # the log is the only trace of a dead hook.
            for _ in range(3):
                assert getattr(proxy, "observe", None) is None
        assert len([r for r in caplog.records if "observe" in r.message]) == 1
