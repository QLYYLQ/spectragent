"""``AgentRuntime`` and the archive axis: validate early, bind last, or not at all.

What the runtime owes the archive axis (``docs/architecture_design.md``
§4.2, "归档：先校验，最后绑定"):

- archive negotiation runs right after memory negotiation, before anything is
  woven — so a binding error surfaces during assembly;
- ``env.bind_archivers`` is the **last** step of ``prepare()``: after the
  intercept audit (no later check can fail once a capability's state changed),
  and still before ``on_run_start`` and environment setup (a capability
  decides at setup whether to build its capture channel);
- with no hook asking to archive, the environment receives **no** archive call
  at all — an environment without the archive surface keeps working;
- the archive facts reach ``on_run_start`` by name, resolved here.

The runtime finds the consumer through ``ArchiveConsumer`` only, so every test
uses :class:`ArchiveConsumerHook`, not the trace hook.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core import runtime as runtime_mod
from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import DeadInterceptError
from spectragent.core.archive.privacy import MARKER_REDACTED, MARKER_SESSION_STATE
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.negotiation import ArchiveNegotiationResult, IncompatibleArchiveError
from spectragent.core.runtime import AgentRuntime
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    STEPS_KEY,
    ArchiveConsumerHook,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeOrphanArchiver,
    FakeStepArchiver,
    SurfacelessEnv,
    make_archive_env,
    make_archive_run,
)
from tests._helpers.assembly import AGENT_ID


class _DeadIntercept(HookBase):
    """Intercepts a method no environment has — the audit refuses it."""

    __registry_key__ = "contract_archive_dead_intercept"
    __intercepts__ = [Intercept("obsserve", role="env", advice="after")]

    async def after_obsserve(self, result: Any, **ctx: Any) -> None:
        pass


def _runtime(
    *hooks: Any, cap: FakeArchivingCapability | None = None, env: Any = None, steps: int = 1
) -> AgentRuntime:
    run = make_archive_run(hooks=list(hooks), capability=cap, env=env, steps=steps)
    return AgentRuntime.for_agent(run, AGENT_ID)


class TestNoConsumerMeansNoArchiveCall:
    async def test_no_hook_at_all(self) -> None:
        env = SurfacelessEnv(make_archive_env())
        runtime = _runtime(env=env)
        result = await runtime.execute("t", timeout=10)
        assert result.error is None
        assert runtime.negotiated_archive is None

    async def test_a_consumer_with_an_empty_archive(self) -> None:
        env = SurfacelessEnv(make_archive_env())
        hook = ArchiveConsumerHook([])
        runtime = _runtime(hook, env=env)
        result = await runtime.execute("t", timeout=10)
        assert result.error is None
        assert runtime.negotiated_archive is None
        assert hook.run_start is not None
        assert hook.run_start["archive_negotiation"] is None
        assert hook.run_start["env_evidence_recorded"] is False
        assert hook.run_start["env_evidence_privacy"] == []

    async def test_the_archiving_capability_is_never_touched(self) -> None:
        cap = FakeArchivingCapability()
        await _runtime(ArchiveConsumerHook([]), cap=cap).execute("t", timeout=10)
        assert cap.bind_calls == 0
        assert cap.collect_calls == 0


class TestTheBinding:
    def test_prepare_binds_the_consumers_archivers(self) -> None:
        cap = FakeArchivingCapability()
        steps, blobs = FakeStepArchiver(), FakeBlobArchiver()
        runtime = _runtime(ArchiveConsumerHook([steps, blobs]), cap=cap)

        runtime.prepare()

        assert cap.bind_calls == 1
        assert cap.fake.bound == (steps, blobs)
        result = runtime.negotiated_archive
        assert isinstance(result, ArchiveNegotiationResult)
        assert result.active_archivers == frozenset({STEPS_KEY, BLOBS_KEY})
        assert dict(result.archive_bindings) == {STEPS_KEY: CAPABILITY_ID, BLOBS_KEY: CAPABILITY_ID}

    def test_order_memory_then_archive_then_weaving_then_audit_then_bind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log: list[str] = []
        cap = FakeArchivingCapability()
        cap.fake.log = log

        real_memory = runtime_mod.negotiate_memory
        real_archive = runtime_mod.negotiate_archive
        real_weaver = runtime_mod.Weaver

        def logged_memory(*args: Any, **kwargs: Any) -> Any:
            log.append("memory")
            return real_memory(*args, **kwargs)

        def logged_archive(*args: Any, **kwargs: Any) -> Any:
            log.append("archive")
            return real_archive(*args, **kwargs)

        class LoggedWeaver(real_weaver):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                log.append("weaver")
                super().__init__(*args, **kwargs)

            def audit_intercepts(self) -> None:
                super().audit_intercepts()
                log.append("audit")

        monkeypatch.setattr(runtime_mod, "negotiate_memory", logged_memory)
        monkeypatch.setattr(runtime_mod, "negotiate_archive", logged_archive)
        monkeypatch.setattr(runtime_mod, "Weaver", LoggedWeaver)

        _runtime(ArchiveConsumerHook([FakeStepArchiver()]), cap=cap).prepare()

        assert log == ["memory", "archive", "weaver", "audit", "bind"]

    async def test_bind_precedes_on_run_start_and_setup(self) -> None:
        log: list[str] = []
        cap = FakeArchivingCapability()
        cap.fake.log = log
        hook = ArchiveConsumerHook([FakeStepArchiver()], log=log)

        await _runtime(hook, cap=cap).execute("t", timeout=10)

        assert log[:3] == ["bind", "on_run_start", "setup"]
        assert cap.fake.bound_at_setup == (STEPS_KEY,)

    def test_a_failing_check_after_negotiation_binds_nothing(self) -> None:
        """Bind is last: the audit refusing a dead intercept leaves the capability untouched."""
        cap = FakeArchivingCapability()
        runtime = _runtime(ArchiveConsumerHook([FakeStepArchiver()]), _DeadIntercept(), cap=cap)
        with pytest.raises(DeadInterceptError):
            runtime.prepare()
        assert cap.bind_calls == 0

    def test_a_binding_error_raises_before_anything_is_woven(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        woven: list[str] = []
        real_weaver = runtime_mod.Weaver

        class LoggedWeaver(real_weaver):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                woven.append("weaver")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(runtime_mod, "Weaver", LoggedWeaver)
        cap = FakeArchivingCapability()
        runtime = _runtime(ArchiveConsumerHook([FakeOrphanArchiver()]), cap=cap)

        with pytest.raises(IncompatibleArchiveError, match="contract_archive_consumer requests"):
            runtime.prepare()
        assert woven == []
        assert cap.bind_calls == 0


class TestOnRunStartReceivesTheArchiveFacts:
    async def test_by_name(self) -> None:
        hook = ArchiveConsumerHook([FakeBlobArchiver(), FakeStepArchiver()])
        runtime = _runtime(hook)
        context = {"start_url": "https://shop.example/landing"}

        await runtime.execute("t", timeout=10, env_context=context)

        started = hook.run_start
        assert started is not None
        assert started["archive_negotiation"] is runtime.negotiated_archive
        assert started["env_evidence_recorded"] is True
        assert started["env_evidence_privacy"] == [MARKER_REDACTED]
        assert started["task_setup"] is context

    async def test_the_privacy_markers_follow_the_archivers_knobs(self) -> None:
        hook = ArchiveConsumerHook([FakeStepArchiver(redact=False), FakeBlobArchiver()])
        await _runtime(hook).execute("t", timeout=10)
        assert hook.run_start is not None
        assert hook.run_start["env_evidence_privacy"] == []
        assert hook.run_start["env_evidence_recorded"] is True

    async def test_the_privacy_markers_are_the_sorted_union_of_every_archivers(self) -> None:
        """Two archivers sharing a marker: each marker once, in sorted order."""

        class _SessionBodies(FakeBlobArchiver):
            __abstract__ = True  # unregistered; still contract_archive.blobs

            @property
            def privacy_markers(self) -> frozenset[str]:
                return frozenset({MARKER_SESSION_STATE, MARKER_REDACTED})

        hook = ArchiveConsumerHook([FakeStepArchiver(), _SessionBodies()])
        await _runtime(hook).execute("t", timeout=10)
        assert hook.run_start is not None
        assert hook.run_start["env_evidence_privacy"] == sorted(
            {MARKER_SESSION_STATE, MARKER_REDACTED}
        )

    async def test_no_context_means_no_task_setup(self) -> None:
        hook = ArchiveConsumerHook([])
        await _runtime(hook).execute("t", timeout=10)
        assert hook.run_start is not None
        assert hook.run_start["task_setup"] is None

    async def test_the_context_still_reaches_setup(self) -> None:
        cap = FakeArchivingCapability()
        await _runtime(ArchiveConsumerHook([]), cap=cap).execute(
            "t", timeout=10, env_context={"start_url": "https://x.example/"}
        )
        assert cap.fake.start_url == "https://x.example/"
