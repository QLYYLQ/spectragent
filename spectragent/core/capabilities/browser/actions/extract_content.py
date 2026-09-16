"""ExtractContentAction — extract text content from the page or an element."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import RequiresFragment
from spectragent.core.capabilities.browser.actions._common import (
    ElementRef,
    focused_scope,
    resolve_index,
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

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int | None,
            RequiresFragment("browser.dom"),
            PydanticField(description="element index; omit to extract page text"),
        ] = None
        goal: Annotated[
            str | None, PydanticField(description="free-form extraction goal")
        ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Extract text content from the page or an element.", **context
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            element: ElementRef | None = None
            if "index" in params and params["index"] is not None:
                element = resolve_index(capability, params["index"])
            scope = focused_scope(capability)
            event = ExtractContentEvent(
                backend_node_id=element.backend_node_id if element else None,
                session_id=element.session_id if element else None,
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
