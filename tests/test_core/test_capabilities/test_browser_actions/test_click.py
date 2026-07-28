"""ClickAction — selector map lookup + event emission."""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.click import ClickAction
from spectragent.core.capabilities.browser.watchdogs.events import ClickElementEvent
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeElementNode,
)


class TestClickDescriptor:
    def test_descriptor_has_index_param(self) -> None:
        desc = ClickAction().descriptor()
        assert desc.name == "browser.click"
        assert "index" in desc.params


class TestClickExecute:
    async def test_looks_up_backend_node_id_and_emits(
        self, fake_capability: FakeCapability
    ) -> None:
        fake_capability._selector_maps["sid_main"][42] = FakeElementNode(
            backend_node_id=777
        )
        result = await ClickAction().execute(
            {"index": 42},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is True
        scope = fake_capability._scopes["sid_main"]
        assert len(scope.emitted) == 1
        event = scope.emitted[0]
        assert isinstance(event, ClickElementEvent)
        assert event.backend_node_id == 777

    async def test_missing_index_in_selector_map_fails(
        self, fake_capability: FakeCapability
    ) -> None:
        result = await ClickAction().execute(
            {"index": 99},  # not in selector_map
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
        assert "99" in (result.error or "")

    async def test_missing_index_param_fails(
        self, fake_capability: FakeCapability
    ) -> None:
        result = await ClickAction().execute(
            {},  # no index
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
