"""TypeCoordinateAction — type text at absolute screen coordinates.

The coordinate sibling of :class:`~spectragent.core.capabilities.browser.
actions.type_action.TypeAction`, exactly as
:class:`~spectragent.core.capabilities.browser.actions.click_coordinate.
ClickCoordinateAction` is the coordinate sibling of ``ClickAction``.

Why two classes rather than one action with optional coordinates: a
vision-only agent and a DOM agent do not have the same *contract*, only
the same verb. ``type`` needs a selector_map index, which only exists
when the run negotiated ``browser.dom``; ``type_coordinate`` needs a
pixel, which only a screenshot gives you. Merging them would produce one
descriptor whose params are half-inapplicable in either configuration —
and the prompt is generated from that descriptor, so the model would be
taught parameters its run cannot honour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._common import (
    focused_scope,
    handler_field,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class TypeCoordinateAction(SettleableAction, ActionBase):
    """Type text into whatever sits at a point of the viewport.

    The point is in **CSS pixels of the page viewport**, the same space as
    :class:`~spectragent.core.capabilities.browser.actions.click_coordinate.
    ClickCoordinateAction` — an agent that reads the point off a screenshot
    converts before emitting this action.
    """

    __registry_key__ = "browser.type_coordinate"

    # ⭐ capability-side configurable side-effect
    clear_by_default: Annotated[
        bool,
        PydanticField(description="Whether to clear the target field first"),
    ] = True

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[
            int, PydanticField(description="CSS px from the viewport's left edge")
        ]
        coordinate_y: Annotated[
            int, PydanticField(description="CSS px from the viewport's top edge")
        ]
        text: str
        #: Omitted means ``clear_by_default`` — see ``TypeAction.Params``.
        clear: bool | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            ("Click a point in the page viewport to focus a text field, then type into it."),
            # Interpolated, not literal: ``clear_by_default`` is a YAML
            # knob, and this string is what the prompt teaches.
            hints={"clear": f"default={str(self.clear_by_default).lower()}"},
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            scope = focused_scope(capability)
            event = TypeAtCoordinateEvent(
                coordinate_x=int(params["coordinate_x"]),
                coordinate_y=int(params["coordinate_y"]),
                text=params["text"],
                clear=(
                    self.clear_by_default if params.get("clear") is None else bool(params["clear"])
                ),
            )
            scope.emit(event)
            await event
            await self.settle()
            return ActionResult(
                success=True, typed_into_sensitive_field=handler_field(event, "sensitive")
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except (TypeError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
