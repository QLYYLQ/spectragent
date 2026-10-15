"""Cloud session cleanup uses fake SDKs only; no cloud credentials or connections."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections import Counter
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.agentbay_lite import AgentBayProvider
from spectragent.core.capabilities.browser.backends.airtop_lite import AirtopProvider
from spectragent.core.capabilities.browser.backends.browser_use_lite import BrowserUseProvider
from spectragent.core.capabilities.browser.backends.hyperbrowser_lite import HyperbrowserProvider
from spectragent.core.capabilities.browser.backends.lexmount import LexmountProvider
from spectragent.core.capabilities.browser.backends.skyvern_lite import SkyvernProvider
from spectragent.schemas.provider_session import ProviderSession


class FakeSDK:
    def __init__(self) -> None:
        self.constructed = 0
        self.created = 0
        self.close_calls: list[str] = []
        self.errors: dict[str, BaseException] = {}
        self.block_session: str | None = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.async_started = asyncio.Event()
        self.async_release = asyncio.Event()
        self.finished = threading.Event()

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created += 1
        sid = f"owned-{self.created}"
        return SimpleNamespace(
            id=sid,
            session_id=sid,
            cdp_url=f"wss://fake.invalid/{sid}",
            connect_url=f"wss://fake.invalid/{sid}",
            close=lambda: self.close(sid),
        )

    async def acreate(self, **kwargs: Any) -> SimpleNamespace:
        return self.create(**kwargs)

    def record_close(self, sid: str) -> None:
        self.close_calls.append(sid)
        if sid in self.errors:
            raise self.errors[sid]

    def close(self, sid: str) -> None:
        self.record_close(sid)
        if sid == self.block_session:
            self.started.set()
            self.release.wait(timeout=5)
            self.finished.set()

    async def delete(self, sid: str) -> None:
        self.record_close(sid)
        if sid == self.block_session:
            self.async_started.set()
            await self.async_release.wait()

    def client(self, *, synchronous: bool) -> SimpleNamespace:
        self.constructed += 1
        return SimpleNamespace(
            api_key="fake-key",
            sessions=SimpleNamespace(
                create=self.create if synchronous else self.acreate, delete=self.delete
            ),
        )


@pytest.fixture(
    params=[
        (AgentBayProvider, "agentbay_lite", "AsyncAgentBayCloud"),
        (AirtopProvider, "airtop_lite", "AsyncAirtopCloud"),
        (BrowserUseProvider, "browser_use_lite", "AsyncBrowserUseCloud"),
        (HyperbrowserProvider, "hyperbrowser_lite", "AsyncHyperbrowserCloud"),
        (LexmountProvider, "lexmount", "Lexmount"),
        (SkyvernProvider, "skyvern_lite", "AsyncSkyvernCloud"),
    ],
    ids=["agentbay", "airtop", "browser_use", "hyperbrowser", "lexmount", "skyvern"],
)
def cloud(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeSDK]:
    provider_type, module_name, constructor = request.param
    sdk = FakeSDK()
    module = ModuleType(module_name)
    setattr(module, constructor, lambda **kwargs: sdk.client(synchronous=module_name == "lexmount"))
    for config in ("ContextAttach", "ManagedProxyConfig", "ProxyConfig", "RecordingConfig"):
        setattr(module, config, SimpleNamespace)
    monkeypatch.setitem(sys.modules, module_name, module)
    return provider_type(), sdk


async def test_failed_close_retains_ownership_for_retry(cloud: tuple[Any, FakeSDK]) -> None:
    provider, sdk = cloud
    session = await provider.open()
    failure = RuntimeError("remote close failed")
    sdk.errors[session.session_id] = failure

    with pytest.raises(RuntimeError) as exc_info:
        await provider.close(session)
    assert exc_info.value is failure

    sdk.errors.clear()
    await provider.close(session)
    await provider.close(session)
    await provider.close()
    assert sdk.close_calls == [session.session_id, session.session_id]


async def test_close_all_attempts_later_sessions_and_retries_only_failures(
    cloud: tuple[Any, FakeSDK],
) -> None:
    provider, sdk = cloud
    sessions = [await provider.open() for _ in range(3)]
    failure = RuntimeError("first session failed")
    sdk.errors[sessions[0].session_id] = failure

    with pytest.raises(RuntimeError) as exc_info:
        await provider.close()
    assert exc_info.value is failure
    assert sdk.close_calls == [session.session_id for session in sessions]

    sdk.errors.clear()
    await provider.close()
    await provider.close()
    assert Counter(sdk.close_calls) == {
        sessions[0].session_id: 2,
        sessions[1].session_id: 1,
        sessions[2].session_id: 1,
    }


async def test_close_all_reports_every_failure(cloud: tuple[Any, FakeSDK]) -> None:
    provider, sdk = cloud
    sessions = [await provider.open() for _ in range(3)]
    failures = [RuntimeError("first failure"), ValueError("second failure")]
    sdk.errors.update(
        {session.session_id: error for session, error in zip(sessions[:2], failures, strict=True)}
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await provider.close()
    assert exc_info.value.exceptions == tuple(failures)
    assert sdk.close_calls == [session.session_id for session in sessions]
    sdk.errors.clear()
    await provider.close()
    assert sdk.close_calls[-2:] == [session.session_id for session in sessions[:2]]


async def test_unknown_session_and_empty_close_do_not_construct_sdk(
    cloud: tuple[Any, FakeSDK],
) -> None:
    provider, sdk = cloud
    session = ProviderSession(session_id="borrowed", provider_id=provider.provider_id)
    await provider.close(session)
    await provider.close()
    assert sdk.constructed == 0
    assert sdk.close_calls == []


async def test_foreign_session_and_unknown_id_leave_owned_session_alone(
    cloud: tuple[Any, FakeSDK],
) -> None:
    provider, sdk = cloud
    session = await provider.open()
    foreign = session.model_copy(update={"provider_id": "other-provider"})
    unknown = session.model_copy(update={"session_id": "borrowed"})
    await provider.close(foreign)
    await provider.close(unknown)
    assert sdk.close_calls == []
    assert foreign.provider_id == "other-provider"
    assert unknown.session_id == "borrowed"
    await provider.close(session)
    assert sdk.close_calls == [session.session_id]


async def test_another_instances_session_is_not_owned(cloud: tuple[Any, FakeSDK]) -> None:
    provider, sdk = cloud
    other = type(provider)()
    session = await other.open()
    await provider.close(session)
    assert sdk.close_calls == []
    assert sdk.constructed == 1  # only the owning provider opened a client
    await other.close(session)
    assert sdk.close_calls == [session.session_id]


async def test_cancellation_stops_batch_preserves_failures_and_allows_retry(
    cloud: tuple[Any, FakeSDK],
) -> None:
    provider, sdk = cloud
    sessions = [await provider.open() for _ in range(3)]
    failure = RuntimeError("first close failed")
    cancellation = asyncio.CancelledError("cancel close")
    sdk.errors.update({sessions[0].session_id: failure, sessions[1].session_id: cancellation})

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await provider.close()
    assert exc_info.value is cancellation
    assert any("first close failed" in note for note in cancellation.__notes__)
    assert sdk.close_calls == [session.session_id for session in sessions[:2]]
    sdk.errors.clear()
    await provider.close()
    assert sdk.close_calls[-3:] == [session.session_id for session in sessions]


async def test_external_cancellation_does_not_continue_batch(cloud: tuple[Any, FakeSDK]) -> None:
    provider, sdk = cloud
    sessions = [await provider.open() for _ in range(2)]
    sdk.block_session = sessions[0].session_id
    task = asyncio.create_task(provider.close())
    try:
        if provider.provider_id == "lexmount":
            assert await asyncio.to_thread(sdk.started.wait, 2)
        else:
            await asyncio.wait_for(sdk.async_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert sdk.close_calls == [sessions[0].session_id]
    finally:
        sdk.release.set()
        sdk.async_release.set()
        if provider.provider_id == "lexmount":
            assert await asyncio.to_thread(sdk.finished.wait, 2)
    sdk.block_session = None
    await provider.close()
    assert sdk.close_calls[-2:] == [session.session_id for session in sessions]
