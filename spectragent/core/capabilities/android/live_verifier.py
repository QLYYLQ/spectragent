"""What the Android capability asks of a benchmark's live verifier it hosts.

``docs/architecture_design.md`` §4.9 "基准组件由 capability 承载", §11.2. A
benchmark whose tasks can only be judged on the device itself (AndroidWorld
reads databases, files and settings there) lists a verifier that needs the live
environment, naming ``android`` as its host. ``AgentRuntime.prepare()`` routes
it to ``AndroidCapability``, which checks it (``check_setup``) and binds it
last (``bind_live_verifiers``). At the scoring point, after the agent's run and
before the teardown, the capability hands it what it gives the benchmark's
setup component: the booted device, and the setup component that prepared the
task on it (:meth:`AndroidLiveVerifier.judge`). A verifier never ends the run
and never changes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
    from spectragent.core.capabilities.android.benchmark_setup import AndroidBenchmarkSetup
    from spectragent.schemas.verdict import Judgment


@runtime_checkable
class AndroidLiveVerifier(Protocol):
    """Judges one task of a benchmark against the device the Android capability booted."""

    @property
    def verifier_id(self) -> str: ...

    @property
    def capability_id(self) -> str: ...

    async def judge(
        self,
        device: AndroidProviderSession,
        setup: AndroidBenchmarkSetup | None,
        *,
        answer: str | None,
    ) -> Judgment:
        """Judge the task on *device*, which *setup* prepared (``None``: no setup component did).

        *answer* is the run's final answer, ``loop.done``'s text or ``None``
        (``LiveVerifierHost.verify_live``); a benchmark that judges no answer
        ignores it. Executes none of the run's actions and tears nothing down.
        Turns its own transport failures into ordinary exceptions and lets no
        ``CancelledError`` of its own escape.
        """
        ...


__all__ = ["AndroidLiveVerifier"]
