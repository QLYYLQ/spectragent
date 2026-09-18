"""ExtractContentAction — dump the page's text, or one element's HTML.

It carries **no** extraction goal. It used to: a ``goal`` param rode the
descriptor into the prompt and the event into the watchdog, where nothing
read it — the handler returns the whole scope either way. The model was
being asked to state what it wanted and handed the page regardless. The
param is deleted rather than kept as a promise; the honest version of that
feature needs an LLM inside the action, which the framework cannot yet
wire or attribute. See ``docs/actions_that_call_an_llm.md``.
"""

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
    """Return a scope wholesale: page ``innerText``, or element ``outerHTML``."""

    __registry_key__ = "browser.extract_content"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int | None,
            RequiresFragment("browser.dom"),
            PydanticField(description="element index; omit to extract page text"),
        ] = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Extract the page's visible text, or one element's HTML. "
            "Returns the whole scope — it does not search or summarise.",
            **context,
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
