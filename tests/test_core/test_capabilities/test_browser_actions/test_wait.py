"""WaitAction — verifies sleep semantics + bounds checking.

The action emits no CDP event so we don't stub a scope handler; the
fixture's empty FakeScope is just there to satisfy the protocol.
"""

from __future__ import annotations

import time

from spectragent.core.capabilities.browser.actions.wait import WaitAction
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestWaitExecute:
    async def test_default_seconds_applied(
        self, fake_capability: FakeCapability,
    ) -> None:
        action = WaitAction(default_seconds=0.05)
        start = time.perf_counter()
        result = await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        elapsed = time.perf_counter() - start

        assert result.success is True
        assert result.extracted_content == "waited 0.05s"
        # Sleep actually happened (within scheduler slop).
        assert elapsed >= 0.04
        # And the action did NOT emit any CDP event.
        assert fake_capability._scopes["sid_main"].emitted == []

    async def test_param_overrides_default(
        self, fake_capability: FakeCapability,
    ) -> None:
        action = WaitAction(default_seconds=5.0)  # would be too slow if used
        start = time.perf_counter()
        result = await action.execute(
            {"seconds": 0.02},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        elapsed = time.perf_counter() - start
        assert result.success is True
        assert elapsed < 0.5  # well under default_seconds=5.0

    async def test_negative_seconds_returns_error(
        self, fake_capability: FakeCapability,
    ) -> None:
        result = await WaitAction().execute(
            {"seconds": -1},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
        assert result.error is not None and "must be >= 0" in result.error

    async def test_above_max_returns_error(
        self, fake_capability: FakeCapability,
    ) -> None:
        action = WaitAction(max_seconds=10.0)
        result = await action.execute(
            {"seconds": 999},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
        assert result.error is not None
        assert "exceeds max_seconds=10.0" in result.error

    async def test_invalid_param_type_returns_error(
        self, fake_capability: FakeCapability,
    ) -> None:
        result = await WaitAction().execute(
            {"seconds": "not-a-number"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
        assert result.error is not None and "Invalid seconds" in result.error

    async def test_descriptor_reflects_config(self) -> None:
        action = WaitAction(default_seconds=2.5, max_seconds=15.0)
        descriptor = action.descriptor()
        assert descriptor.name == "browser.wait"
        # max appears in description and params hint.
        assert "max=15.0" in descriptor.description
        assert "seconds" in descriptor.params
        assert "max=15.0" in descriptor.params["seconds"]
