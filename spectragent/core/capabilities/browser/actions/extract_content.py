"""ExtractContentAction — extract text content from the page or an element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    resolve_index_to_backend_node_id,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    ExtractContentEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ExtractContentAction(ActionBase):
    """Extract text from the page or a specific element."""

    __registry_key__ = "browser.extract_content"

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="browser.extract_content",
            description="Extract text content from the page or an element.",
            params={
                "index": "int (optional) -- element index; omit to extract page text",
                "goal": "str (optional) -- free-form extraction goal",
            },
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            backend_node_id: int | None = None
            if "index" in params and params["index"] is not None:
                backend_node_id = resolve_index_to_backend_node_id(
                    capability, params["index"],
                )
            scope = focused_scope(capability)
            event = ExtractContentEvent(
                backend_node_id=backend_node_id,
                goal=params.get("goal"),
            )
            scope.emit(event)
            await event

            # Harvest extracted text from the first successful result.
            extracted: str | None = None
            for result_obj in event.event_results.values():
                if result_obj.is_success and isinstance(result_obj.result, str):
                    extracted = result_obj.result
                    break
            return ActionResult(success=True, extracted_content=extracted)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
