"""DoneAction — verifies the non-CDP terminal action contract.

Unlike other actions, ``DoneAction`` does not emit an event. It takes
``extracted_content`` from the action params and returns
``ActionResult(is_done=True, extracted_content=...)``. This test pins
down the M13-Rev contract so parse_react_output can rely on it.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.done import DoneAction
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestDoneDescriptor:
    def test_descriptor_returns_extracted_content_param(self) -> None:
        desc = DoneAction().descriptor()
        assert desc.name == "browser.done"
        assert "extracted_content" in desc.params


class TestDoneExecute:
    async def test_sets_is_done_true(self, fake_capability: FakeCapability) -> None:
        result = await DoneAction().execute(
            {"extracted_content": "42"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.success is True
        assert result.extracted_content == "42"

    async def test_missing_extracted_content_is_none(self, fake_capability: FakeCapability) -> None:
        result = await DoneAction().execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.extracted_content is None

    async def test_success_flag_passes_through(self, fake_capability: FakeCapability) -> None:
        result = await DoneAction().execute(
            {"extracted_content": "fail", "success": False},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.success is False

    async def test_emits_no_cdp_event(self, fake_capability: FakeCapability) -> None:
        scope = fake_capability._scopes["sid_main"]
        await DoneAction().execute(
            {"extracted_content": "x"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert scope.emitted == []
