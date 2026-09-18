"""NavigateAction — verifies URL + capability-side wait_until / timeout_ms."""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.navigate import NavigateAction
from spectragent.core.capabilities.browser.watchdogs.events import (
    NavigateToUrlEvent,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestNavigateExecute:
    async def test_emits_event_with_url(self, fake_capability: FakeCapability) -> None:
        await NavigateAction().execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, NavigateToUrlEvent)
        assert event.url == "https://example.com"
        assert event.wait_until == "load"
        # NavigateAction's default timeout overrides the event class'
        # None default so watchdogs see a concrete value.
        assert event.timeout_ms == 30000

    async def test_wait_until_config_applied(self, fake_capability: FakeCapability) -> None:
        action = NavigateAction(wait_until="networkidle", timeout_ms=5000)
        await action.execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert event.wait_until == "networkidle"
        assert event.timeout_ms == 5000

    async def test_missing_url_fails(self, fake_capability: FakeCapability) -> None:
        result = await NavigateAction().execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False

    async def test_lifecycle_timeout_ms_propagates_to_event(
        self, fake_capability: FakeCapability
    ) -> None:
        action = NavigateAction(
            wait_until="networkidle",
            lifecycle_timeout_ms=8000,
        )
        await action.execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert event.wait_until == "networkidle"
        assert event.lifecycle_timeout_ms == 8000
        # ``timeout_ms`` keeps its action-level default; lifecycle wait
        # uses ``lifecycle_timeout_ms`` first when both are set.
        assert event.timeout_ms == 30000

    async def test_lifecycle_timeout_ms_default_is_none(
        self, fake_capability: FakeCapability
    ) -> None:
        await NavigateAction().execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        # ``None`` means "fall back to timeout_ms" in the watchdog.
        assert event.lifecycle_timeout_ms is None
