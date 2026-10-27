"""AOPProxy transparency — where it holds, and where it must say so.

The bugs pinned here:

* ``proxy.attr = x`` landing in the proxy's own ``__dict__``, permanently
  shadowing the wrapped method — which is how ``Loop.weave()``'s
  ``agent.run = run`` made ``Intercept("run", role="agent")`` unreachable.
* The proxy's own bookkeeping names (``_target``, ``_role``,
  ``_background`` ...) hiding target attributes of the same names from
  reads, while writes of those names went to the target.
* Method resolution snapshotted at wrap time, so anything installed
  afterwards was never intercepted.
* A hook declaring a role nobody wraps registering cleanly and never
  firing — with no log and no error.
* The inverse: a wrap-time "no such attribute" warning declaring a
  working ``agent.run`` hook dead, because it judged a lazily resolved
  method before ``Loop.weave()`` had installed it.
* ``bool(proxy)`` / ``proxy == target`` answering for the *proxy*, which
  is a wrong answer given silently.
* ``async with proxy`` dying on a bare "'AOPProxy' object does not
  support the asynchronous context manager protocol", which names
  neither the target nor the way out.
"""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import UnclaimedRoleError, UnsupportedProxyProtocolError
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


#: The names the proxy used to keep its own bookkeeping under.
_FORMER_PROXY_ATTRIBUTES = (
    "_target",
    "_role",
    "_method_interceptors",
    "_wrapper_cache",
    "_reported_missing",
    "_background",
)
_FORMER_PROXY_METHODS = ("_protocol_method", "_reproxy")


class _SameNamesTarget:
    """A target that uses, for itself, every name the proxy used to keep."""

    def __init__(self):
        for name in _FORMER_PROXY_ATTRIBUTES:
            setattr(self, name, f"the target's {name}")

    def _protocol_method(self):
        return "the target's _protocol_method"

    def _reproxy(self):
        return "the target's _reproxy"


class TestProxyReadsReachTheTarget:
    """A read through the proxy answers what the target holds, whatever the name.

    The proxy kept its bookkeeping as ordinary instance attributes and
    methods (``_target``, ``_role``, ``_background`` ...). Normal lookup
    finds those before ``__getattr__`` runs, so a target attribute of the
    same name could not be read through the proxy, while a write of that
    name went to the target: write ``proxy._role``, read back the proxy's.
    """

    @pytest.mark.parametrize("name", _FORMER_PROXY_ATTRIBUTES)
    def test_an_attribute_is_read_from_the_target(self, name):
        proxy = AOPProxy(_SameNamesTarget(), {}, role="env")
        assert getattr(proxy, name) == f"the target's {name}"

    @pytest.mark.parametrize("name", _FORMER_PROXY_METHODS)
    def test_a_method_is_read_from_the_target(self, name):
        proxy = AOPProxy(_SameNamesTarget(), {}, role="env")
        assert getattr(proxy, name)() == f"the target's {name}"

    def test_a_write_reads_back(self):
        target = _SameNamesTarget()
        proxy = AOPProxy(target, {}, role="env")
        proxy._role = "rewritten"
        assert vars(target)["_role"] == "rewritten"
        assert proxy._role == "rewritten"

    def test_the_proxy_keeps_only_names_private_to_its_class(self):
        """Any other name it resolved for itself would hide a target's."""
        proxy = AOPProxy(_Target(), {}, role="env")
        own = set(object.__getattribute__(proxy, "__dict__")) | set(vars(AOPProxy))
        hidden = {
            name
            for name in own
            if not name.startswith("_AOPProxy__")
            and not (name.startswith("__") and name.endswith("__"))
        }
        assert hidden == set()


class _Opinionated:
    """A target with opinions about truthiness and equality."""

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other) -> bool:
        return isinstance(other, _Opinionated | AOPProxy)

    def __hash__(self) -> int:
        return 42


class _CtxTarget:
    """The ``CDPClient`` shape: a resource held through a ``with`` block."""

    def __init__(self):
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def __iter__(self):
        return iter([1, 2])

    async def observe(self, *a, **kw):
        return "observed"


class TestForwardedProtocols:
    """The dunders that used to answer wrongly, and say nothing about it."""

    def test_bool_asks_the_target(self):
        proxy = AOPProxy(_Opinionated(), {}, role="env")
        assert bool(proxy) is False

    def test_equality_asks_the_target(self):
        target = _Opinionated()
        proxy = AOPProxy(target, {}, role="env")
        assert proxy == target
        assert target == proxy

    def test_two_proxies_of_one_target_compare_and_hash_alike(self):
        target = _Target()
        a = AOPProxy(target, {}, role="env")
        b = AOPProxy(target, {}, role="env")
        assert a == b
        assert hash(a) == hash(b) == hash(target)
        assert len({a, b, target}) == 1


class TestCuratedProtocols:
    """Declared on the class, because an implicit lookup never sees ``__getattr__``."""

    async def test_async_context_manager_reaches_the_target(self):
        target = _CtxTarget()
        proxy = AOPProxy(target, {}, role="env")
        async with proxy as entered:
            # ``as x`` stays woven: the target returned *itself*, so
            # handing back the raw object would silently unweave it.
            assert entered is proxy
        assert target.exited is True

    def test_context_manager_reaches_the_target(self):
        target = _CtxTarget()
        proxy = AOPProxy(target, {}, role="env")
        with proxy as entered:
            assert entered is proxy
        assert target.exited is True

    def test_iteration_reaches_the_target(self):
        proxy = AOPProxy(_CtxTarget(), {}, role="env")
        assert list(proxy) == [1, 2]

    async def test_an_intercepted_method_still_fires_inside_the_block(self):
        seen: list = []
        proxy = Weaver([_Hook(seen)]).wrap(_CtxTarget(), role="env")
        async with proxy as entered:
            await entered.observe()
        assert seen == [("observed", "env", "observe")]

    def test_a_target_without_the_protocol_names_itself(self):
        proxy = AOPProxy(_Target(), {}, role="env")
        with pytest.raises(UnsupportedProxyProtocolError) as exc, proxy:
            pass
        message = str(exc.value)
        assert "_Target" in message
        assert "__enter__" in message
        assert "role='env'" in message

    def test_the_error_is_still_a_typeerror(self):
        """Duck-typed ``try: iter(x) except TypeError`` must keep working."""
        proxy = AOPProxy(_Target(), {}, role="env")
        with pytest.raises(TypeError):
            iter(proxy)


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
