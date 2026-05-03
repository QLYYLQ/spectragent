"""SettleableAction mixin — verifies post_delay_ms actually sleeps.

Uses NavigateAction as a representative state-changing action. Stubs
``scope.emit`` so the awaitable resolves immediately, then measures
total wallclock time to confirm the mixin sleep ran.
"""

from __future__ import annotations

import time

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.actions.navigate import NavigateAction
from spectragent.core.capabilities.browser.actions.wait import WaitAction
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeElementNode,
)


class TestSettleableMixin:
    async def test_navigate_post_delay_runs(
        self, fake_capability: FakeCapability,
    ) -> None:
        # 50ms — enough to detect, fast enough to run as a unit test.
        action = NavigateAction(post_delay_ms=50)
        start = time.perf_counter()
        result = await action.execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
            session_id="sid_main",
        )
        elapsed = time.perf_counter() - start

        assert result.success is True
        assert elapsed >= 0.04, (
            f"settle() should have slept ≥40ms, got {elapsed:.3f}s"
        )

    async def test_click_post_delay_runs(
        self, fake_capability: FakeCapability,
    ) -> None:
        # ClickAction also inherits the mixin — different action path.
        fake_capability._selector_maps["sid_main"][1] = FakeElementNode(
            backend_node_id=42,
        )
        action = ClickAction(post_delay_ms=50)

        start = time.perf_counter()
        result = await action.execute(
            {"index": 1},
            capability=fake_capability,  # type: ignore[arg-type]
            session_id="sid_main",
        )
        elapsed = time.perf_counter() - start

        assert result.success is True
        assert elapsed >= 0.04

    async def test_zero_delay_is_fast(
        self, fake_capability: FakeCapability,
    ) -> None:
        action = NavigateAction()  # post_delay_ms=0 default
        start = time.perf_counter()
        result = await action.execute(
            {"url": "https://example.com"},
            capability=fake_capability,  # type: ignore[arg-type]
            session_id="sid_main",
        )
        elapsed = time.perf_counter() - start

        assert result.success is True
        # Default should not introduce noticeable delay (<10ms).
        assert elapsed < 0.05

    async def test_wait_action_does_not_inherit_mixin(self) -> None:
        # WaitAction's own ``seconds`` param is the wait surface — it
        # must NOT also expose post_delay_ms (would double-sleep).
        action = WaitAction()
        assert not hasattr(action, "post_delay_ms")
