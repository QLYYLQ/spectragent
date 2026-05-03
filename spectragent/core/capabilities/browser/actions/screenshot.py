"""ScreenshotAction — capture the current page via CDP Page.captureScreenshot.

Note on LLM delivery
--------------------
``ActionResult.extracted_content`` is a *text-only* field; whatever this
action returns is rendered as plain text inside the next step's
``<results>`` block (see ``BrowserUseAgent._render_history``). That means
neither a file path nor an inline base64 data URL placed here is shown to
the model as a real image. To have the LLM actually *see* a screenshot,
use the existing ``browser.screenshot`` observation fragment with
``use_vision=True`` — that is the multimodal channel
(``BrowserUseAgent._build_vision_message`` → ``ContentPartImageParam``).

This action's job is therefore to **capture and persist on demand**: write
the PNG (or jpeg/webp) to disk so it lands as a run artifact, and hand
back a textual receipt the LLM can refer to in subsequent steps. An
opt-in ``return_mode="base64"`` exists for pipelines that want the inline
data URL in the text channel — bear in mind it inflates context badly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.watchdogs.events import (
    ScreenshotRequestEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ScreenshotAction(ActionBase):
    """Capture a screenshot and return a textual receipt."""

    __registry_key__ = "browser.screenshot"

    # ⭐ capability-side configurable side-effects
    output_dir: Annotated[
        str | None,
        PydanticField(
            description=(
                "Directory under which captured PNGs are written. "
                "Set to None to skip on-disk persistence — only useful "
                "in combination with return_mode='base64'."
            ),
        ),
    ] = "screenshots"

    return_mode: Annotated[
        Literal["path", "base64", "both"],
        PydanticField(
            description=(
                "How extracted_content is filled. 'path' (default) is "
                "the on-disk filename — small, LLM-friendly. 'base64' "
                "is an inline data URL — large, text-only (won't be "
                "rendered as an image by vision models). 'both' joins "
                "the two."
            ),
        ),
    ] = "path"

    default_format: Annotated[
        Literal["png", "jpeg", "webp"],
        PydanticField(description="Capture format when the agent omits one"),
    ] = "png"

    default_full_page: Annotated[
        bool,
        PydanticField(
            description="Capture beyond the viewport when the agent omits a flag",
        ),
    ] = False

    default_quality: Annotated[
        int | None,
        PydanticField(description="Default quality (jpeg/webp 0-100); ignored for png"),
    ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.screenshot",
            description=(
                "Capture a screenshot of the current page. The PNG is "
                "saved to disk as a run artifact and the file path is "
                "returned. This action does not display the image to "
                "the model — vision-capable agents read screenshots "
                "from the browser.screenshot observation fragment."
            ),
            params={
                "full_page": "bool (optional) -- capture beyond viewport",
                "format": "png|jpeg|webp (optional)",
                "quality": "int (optional) -- jpeg/webp quality 0-100",
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
        session_id: str,
    ) -> ActionResult:
        try:
            scope = capability._scopes[session_id]
            full_page = bool(params.get("full_page", self.default_full_page))
            fmt = params.get("format") or self.default_format
            quality = params.get("quality", self.default_quality)
            quality_int = int(quality) if isinstance(quality, (int, float)) else None

            event = ScreenshotRequestEvent(
                full_page=full_page,
                format=fmt,
                quality=quality_int,
            )
            scope.emit(event)
            await event

            png_bytes: bytes | None = None
            for r in event.event_results.values():
                if r.is_success and isinstance(r.result, (bytes, bytearray)):
                    png_bytes = bytes(r.result)
                    break
            if png_bytes is None:
                return ActionResult(
                    success=False,
                    error="ScreenshotRequestEvent returned no bytes",
                )

            parts: list[str] = []
            if self.output_dir is not None:
                out_dir = Path(self.output_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                ext = "jpg" if fmt == "jpeg" else fmt
                file_path = out_dir / f"{stamp}_{session_id[:8]}.{ext}"
                file_path.write_bytes(png_bytes)
                parts.append(f"saved={file_path} bytes={len(png_bytes)}")

            if self.return_mode in ("base64", "both"):
                media = "image/jpeg" if fmt == "jpeg" else f"image/{fmt}"
                b64 = base64.b64encode(png_bytes).decode("ascii")
                parts.append(f"data:{media};base64,{b64}")

            if not parts:
                parts.append(f"captured bytes={len(png_bytes)}")

            return ActionResult(
                success=True,
                extracted_content=" | ".join(parts),
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
