"""ExtractContentAction — dump the page's text, or one element's HTML.

It carries **no** extraction goal. It used to: a ``goal`` param rode the
descriptor into the prompt and the event into the watchdog, where nothing
read it — the handler returns the whole scope either way. The model was
being asked to state what it wanted and handed the page regardless. The
param is deleted rather than kept as a promise; the honest version of that
feature needs an LLM inside the action, which the framework cannot yet
wire or attribute. See ``docs/actions_that_call_an_llm.md``.

The result goes into the agent's next prompt, so one element's HTML comes back
with the sensitivity rule applied (``dom/sensitivity.py``): a sensitive
control's ``value`` attributes and the text inside it are ``<redacted>``,
escaped as markup. The page text holds no field's value to begin with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from agent_cdp import Participation
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement
from spectragent.core.capabilities.browser.actions._common import (
    ElementRef,
    focused_scope,
    resolve_index,
)
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import (
    ExtractContentEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class ExtractContentAction(ActionBase):
    """Return a scope wholesale: page ``innerText``, or element ``outerHTML`` (values withheld)."""

    __registry_key__ = "browser.extract_content"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int | None,
            AddressesElement("browser.dom"),
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
            await dispatch_request(scope, event)

            # Harvest extracted text from the first successful result.
            extracted: str | None = None
            for result_obj in event.results.values():
                if (
                    result_obj.is_success
                    and result_obj.participation is Participation.HANDLER
                    and result_obj.receiver_key == "browser.action"
                    and isinstance(result_obj.result, str)
                ):
                    extracted = result_obj.result
                    break
            if extracted is None:
                raise RuntimeError("Extraction executor returned no text")
            return ActionResult(success=True, extracted_content=extracted)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
