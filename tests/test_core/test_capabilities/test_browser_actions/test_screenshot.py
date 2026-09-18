"""ScreenshotAction — verifies CDP event emission, on-disk persistence,
and the path/base64/both return modes."""

from __future__ import annotations

import base64
from pathlib import Path

from spectragent.core.capabilities.browser.actions.screenshot import (
    ScreenshotAction,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    ScreenshotRequestEvent,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeScope,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-screenshot-payload"


def _stub_screenshot_handler(scope: FakeScope, payload: bytes = _PNG_BYTES) -> None:
    """Patch ``scope.emit`` so ScreenshotRequestEvent records ``payload`` once."""

    original_emit = scope.emit

    def emit(event):  # type: ignore[no-untyped-def]
        original_emit(event)
        if isinstance(event, ScreenshotRequestEvent):
            event.record_result(
                connection_id="fake",
                handler_name="ScreenshotWatchdog",
                result=payload,
            )

    scope.emit = emit  # type: ignore[assignment]


class TestScreenshotExecute:
    async def test_emits_event_with_defaults(
        self, fake_capability: FakeCapability, tmp_path: Path
    ) -> None:
        scope = fake_capability._scopes["sid_main"]
        _stub_screenshot_handler(scope)

        action = ScreenshotAction(output_dir=str(tmp_path))
        result = await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )

        event = scope.emitted[0]
        assert isinstance(event, ScreenshotRequestEvent)
        assert event.full_page is False
        assert event.format == "png"
        assert event.quality is None
        assert result.success is True
        assert result.extracted_content is not None
        assert "saved=" in result.extracted_content
        # Exactly one PNG written under the configured output dir.
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".png"
        assert files[0].read_bytes() == _PNG_BYTES

    async def test_param_overrides_propagate_to_event(
        self, fake_capability: FakeCapability, tmp_path: Path
    ) -> None:
        scope = fake_capability._scopes["sid_main"]
        _stub_screenshot_handler(scope)

        action = ScreenshotAction(output_dir=str(tmp_path))
        await action.execute(
            {"full_page": True, "format": "jpeg", "quality": 70},
            capability=fake_capability,  # type: ignore[arg-type]
        )

        event = scope.emitted[0]
        assert event.full_page is True
        assert event.format == "jpeg"
        assert event.quality == 70
        assert list(tmp_path.iterdir())[0].suffix == ".jpg"

    async def test_capability_defaults_applied(
        self, fake_capability: FakeCapability, tmp_path: Path
    ) -> None:
        scope = fake_capability._scopes["sid_main"]
        _stub_screenshot_handler(scope)

        action = ScreenshotAction(
            output_dir=str(tmp_path),
            default_full_page=True,
            default_format="webp",
            default_quality=50,
        )
        await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )

        event = scope.emitted[0]
        assert event.full_page is True
        assert event.format == "webp"
        assert event.quality == 50

    async def test_return_mode_base64(self, fake_capability: FakeCapability) -> None:
        scope = fake_capability._scopes["sid_main"]
        _stub_screenshot_handler(scope)

        action = ScreenshotAction(output_dir=None, return_mode="base64")
        result = await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )

        assert result.success is True
        b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
        assert result.extracted_content == f"data:image/png;base64,{b64}"

    async def test_return_mode_both(self, fake_capability: FakeCapability, tmp_path: Path) -> None:
        scope = fake_capability._scopes["sid_main"]
        _stub_screenshot_handler(scope)

        action = ScreenshotAction(output_dir=str(tmp_path), return_mode="both")
        result = await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )

        assert result.extracted_content is not None
        assert "saved=" in result.extracted_content
        assert "data:image/png;base64," in result.extracted_content

    async def test_no_handler_yields_failure(
        self, fake_capability: FakeCapability, tmp_path: Path
    ) -> None:
        # No handler stub — event_results stays empty, action should fail.
        action = ScreenshotAction(output_dir=str(tmp_path))
        result = await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False
        assert result.error is not None
        assert "no bytes" in result.error
        # No file should have been written when capture fails.
        assert list(tmp_path.iterdir()) == []
