"""Experiment ownership is explicit, lazy, and independent of resource kind."""

import asyncio

import pytest

from spectragent.core.composer import ComposedRun
from spectragent.core.experiment import ExperimentScope


class Participant:
    def __init__(self, closed, configuration="same"):
        self.closed, self.configuration = closed, configuration
        self.binds = 0
        self.owner = None

    def bind_experiment(self, scope, *, identity):
        self.binds += 1

        async def close(owner):
            self.closed.append(owner)

        self.owner = scope.resource(
            identity, configuration=self.configuration, create=object, close=close
        )


async def test_fresh_runs_share_stable_identity_and_repeated_instances_bind_once():
    closed = []
    a, b = Participant(closed), Participant(closed)
    async with ExperimentScope() as scope:
        one = ComposedRun({"resource": {"first": a}, "alias": {"same": a}})
        scope.bind(one)
        scope.bind(one)
        scope.bind(ComposedRun({"resource": {"first": b}}))
        assert a.binds == b.binds == 1
        assert a.owner is b.owner
        assert closed == []
    assert closed == [a.owner]


async def test_same_identity_cannot_silently_reuse_different_configuration():
    async with ExperimentScope() as scope:
        scope.bind(ComposedRun({"resource": {"first": Participant([])}}))
        with pytest.raises(ValueError, match="conflicting configuration"):
            scope.bind(ComposedRun({"resource": {"first": Participant([], "different")}}))


async def test_cleanup_attempts_every_resource_without_replacing_execution_error():
    closed = []
    error = ValueError("the original failure")
    with pytest.raises(ValueError, match="original failure") as caught:
        async with ExperimentScope() as scope:

            async def fail(_):
                closed.append("failed")
                raise RuntimeError("cleanup broke")

            async def good(_):
                closed.append("good")

            scope.resource("good", configuration=1, create=object, close=good)
            scope.resource("bad", configuration=1, create=object, close=fail)
            raise error
    assert caught.value is error
    assert closed == ["failed", "good"]
    assert "cleanup" in error.__notes__[0]


async def test_cancellation_waits_for_owned_cleanup_and_preserves_cancellation():
    entered, release, closed = asyncio.Event(), asyncio.Event(), []
    scope = ExperimentScope()
    await scope.__aenter__()

    async def close(_):
        entered.set()
        await release.wait()
        closed.append(True)

    scope.resource("resource", configuration=1, create=object, close=close)
    closing = asyncio.create_task(scope.aclose())
    await entered.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert closed == [True]


async def test_timeout_reports_unfinished_cleanup_instead_of_success():
    released = asyncio.Event()
    async with ExperimentScope(cleanup_timeout_s=0.01) as scope:

        async def close(_):
            await released.wait()

        scope.resource("resource", configuration=1, create=object, close=close)
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            await scope.aclose()
        # A resource that cannot close remains retryable and is not called complete.
        released.set()
        await scope.aclose()


async def test_local_provider_binding_does_not_open_chrome():
    from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider

    provider = LocalChromeProvider(lifetime="experiment")
    async with ExperimentScope() as scope:
        scope.bind(ComposedRun({"browser_provider": {"local": provider}}))
        assert provider.managed_browser is not None
        assert not provider._launches


async def test_body_error_does_not_swallow_cancellation_during_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    original = ValueError("body failed")

    async def run():
        async with ExperimentScope() as scope:

            async def close(_):
                entered.set()
                await release.wait()

            scope.resource("resource", configuration=1, create=object, close=close)
            raise original

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.__cause__ is original


async def test_uncooperative_close_remains_owned_and_does_not_defeat_budget():
    release = asyncio.Event()
    scope = ExperimentScope(cleanup_timeout_s=0.01)
    await scope.__aenter__()

    async def close(_):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    scope.resource("resource", configuration=1, create=object, close=close)
    try:
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            await asyncio.wait_for(scope.aclose(), 0.5)
    finally:
        release.set()
        await scope.aclose()
