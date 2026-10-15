"""``Weaver.audit_intercepts()`` — a declared intercept must be able to fire.

``raise_for_unclaimed_roles`` catches a role nothing wraps. This catches
the rest of the silent no-ops: a typo'd method name, an ``env``-role
intercept in an env-less run (``wrap(None, role="env")`` *claims* the
role, so the unclaimed check waves it through), and advice whose shape
the target cannot serve.

The audit's timing is the whole trick. The wrap-time version of this
check was deleted because no agent has ``run`` until ``Loop.weave()``
installs it, so it called the framework's flagship intercept dead on
every run. Run after weaving, the same question has a right answer.
"""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import DeadInterceptError
from spectragent.core.aop.weaver import Weaver
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env, make_run


class _Env:
    async def observe(self, *a, **kw):
        return "observed"


class _Agent:
    pass


class _ObserveHook:
    hook_id = "obs_spy"
    blocking = True
    __intercepts__ = [Intercept("observe", role="env", advice="after")]

    async def after_observe(self, result, *, args, kwargs, **ctx):
        pass


class _TypoHook:
    hook_id = "typo"
    blocking = True
    __intercepts__ = [Intercept("obsserve", role="env", advice="after")]

    async def after_obsserve(self, result, *, args, kwargs, **ctx):
        pass


class _RunHook:
    hook_id = "run_spy"
    blocking = True
    __intercepts__ = [Intercept("run", role="agent", advice="after")]

    async def after_run(self, result, *, args, kwargs, **ctx):
        pass


class _SyncHandlerHook:
    hook_id = "sync_handler"
    blocking = True
    __intercepts__ = [Intercept("observe", role="env", advice="after")]

    def after_observe(self, result, *, args, kwargs, **ctx):
        pass


class TestAuditCatchesDeadIntercepts:
    def test_typo_in_the_method_name_raises(self) -> None:
        weaver = Weaver([_TypoHook()])
        weaver.wrap(_Env(), role="env")
        with pytest.raises(DeadInterceptError) as exc:
            weaver.audit_intercepts()
        assert "env.obsserve" in str(exc.value)
        assert "typo" in str(exc.value)

    def test_env_role_intercept_in_an_envless_run_raises(self) -> None:
        """``wrap(None, role="env")`` claims the role — nothing else notices."""
        weaver = Weaver([_ObserveHook()])
        weaver.wrap(None, role="env")
        weaver.raise_for_unclaimed_roles()  # passes: the role *was* claimed
        with pytest.raises(DeadInterceptError, match="no 'env' object"):
            weaver.audit_intercepts()

    def test_handler_shape_mismatch_raises(self) -> None:
        weaver = Weaver([_SyncHandlerHook()])
        weaver.wrap(_Env(), role="env")
        with pytest.raises(DeadInterceptError, match="plain ``def``"):
            weaver.audit_intercepts()

    def test_every_dead_intercept_is_reported_at_once(self) -> None:
        weaver = Weaver([_TypoHook(), _RunHook()])
        weaver.wrap(_Env(), role="env")
        weaver.wrap(_Agent(), role="agent")  # ``run`` never installed
        with pytest.raises(DeadInterceptError) as exc:
            weaver.audit_intercepts()
        assert "env.obsserve" in str(exc.value)
        assert "agent.run" in str(exc.value)


class TestAuditDoesNotFalseAlarm:
    def test_agent_run_after_weave_is_alive(self) -> None:
        """The exact case that got the wrap-time check deleted."""
        weaver = Weaver([_RunHook()])
        proxy = weaver.wrap(_Agent(), role="agent")

        async def run(task):  # what AgentLoopBase.weave installs
            return f"ran:{task}"

        proxy.run = run
        weaver.audit_intercepts()  # must not raise

    def test_unclaimed_roles_are_left_to_the_other_check(self) -> None:
        class _CapHook:
            hook_id = "cap_spy"
            blocking = True
            __intercepts__ = [Intercept("resolve", role="capability", advice="after")]

            async def after_resolve(self, result, *, args, kwargs, **ctx):
                pass

        weaver = Weaver([_CapHook()])
        weaver.wrap(_Env(), role="env")
        weaver.audit_intercepts()  # silent — raise_for_unclaimed_roles owns it

    def test_a_dynamically_served_method_is_not_dead(self) -> None:
        """``getattr_static`` skips ``__getattr__``; a router is not a typo."""

        class _Router:
            def __getattr__(self, name):
                async def _served(*a, **kw):
                    return name

                return _served

        weaver = Weaver([_ObserveHook()])
        weaver.wrap(_Router(), role="env")
        weaver.audit_intercepts()  # must not raise

    def test_a_run_with_no_hooks_audits_clean(self) -> None:
        weaver = Weaver([])
        weaver.wrap(_Env(), role="env")
        weaver.audit_intercepts()


class _AuditCap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "audit_cap"
    __wiring__ = {"action": ("action", ["audit_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="audit_cap.done", description="Done")]

    async def execute_action(self, action, **kw):
        return ActionResult(success=True, is_done=True, extracted_content="ok")


class _AuditEnv(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "audit_env"


class _AuditAgent:
    __wiring__ = {"action": ("action", ["audit_cap.done"])}

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs, *, context):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, llm_output):
        return [Action(name="audit_cap.done")]

    def get_react_output_model(self):
        return None


class _AuditLLM:
    model = "test"

    async def ainvoke(self, messages, **kwargs):
        raise AssertionError("prepare() must not call the LLM")


class _AuditMemory:
    supports_protocols = frozenset({"user_assistant"})

    def add_messages(self, msgs):
        pass

    def get_context_messages(self):
        return []

    def clear(self):
        pass


def _runtime_with(hooks: list, *, env: bool = True) -> AgentRuntime:
    run = make_run(
        agent=_AuditAgent(),
        loop=ReactLoop(),
        env=make_env(_AuditEnv, {"audit_cap": _AuditCap()}) if env else None,
        llm=_AuditLLM(),
        memory=_AuditMemory(),
        hooks=hooks,
    )
    return AgentRuntime.for_agent(run, AGENT_ID)


class TestAuditRunsInPrepare:
    """The audit is wired into ``AgentRuntime.prepare()``, after weave.

    Before this, a typo'd intercept cost a warning in a log at the first
    call that happened to touch the method — or nothing at all, since
    ``getattr(obj, name, default)`` swallows the AttributeError.
    """

    def test_prepare_raises_on_a_typod_intercept(self) -> None:
        runtime = _runtime_with([_TypoHook()])
        with pytest.raises(DeadInterceptError, match="env.obsserve"):
            runtime.prepare()

    def test_prepare_passes_with_a_live_agent_run_intercept(self) -> None:
        runtime = _runtime_with([_RunHook()])
        runtime.prepare()  # must not raise — weave installed ``run``

    def test_prepare_passes_with_a_live_env_intercept(self) -> None:
        runtime = _runtime_with([_ObserveHook()])
        runtime.prepare()
