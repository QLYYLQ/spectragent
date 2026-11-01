"""TypeCoordinateAction — type text into a text field: at a point of the viewport, or where focus is.

The coordinate sibling of :class:`~spectragent.core.capabilities.browser.
actions.type_action.TypeAction`, exactly as
:class:`~spectragent.core.capabilities.browser.actions.click_coordinate.
ClickCoordinateAction` is the coordinate sibling of ``ClickAction``.

Why two classes rather than one action with an optional index and an
optional point: a vision-only agent and a DOM agent do not have the same
*contract*, only the same verb. ``type`` needs a selector_map index, which
only exists when the run negotiated ``browser.dom``; ``type_coordinate``'s
point needs a pixel, which only a screenshot gives you. Merging them would
produce one descriptor whose params are half-inapplicable in either
configuration — and the prompt is generated from that descriptor, so the
model would be taught parameters its run cannot honour.

The point is optional (user ruling 2026-11-01, ``docs/architecture_design.md``
§4.5): given one, the action clicks there and types into the field that took
focus; without one it clicks nothing and types into the field that has focus.
Either way it sends no key when no text field has focus. Given a point, it
also sends none when focus stayed on the element that had it before the
click and the point is not in that element (user ruling 2026-11-02, §4.9); a
field that took focus is typed into wherever it is. Like ``scroll``'s start,
the point is both axes or neither, and a run whose agent declares no grid is
not offered it (``coordinate_frames.framed_action_space``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import PointAxis
from spectragent.core.capabilities.browser.actions._common import (
    effect_from,
    focused_scope,
    handler_field,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effects import TextInputEffect
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
)
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class TypeCoordinateAction(SettleableAction, ActionBase):
    """Type text into a text field, clicking a point of the viewport first when given one.

    The point is in **CSS pixels of the page viewport**, the same space as
    :class:`~spectragent.core.capabilities.browser.actions.click_coordinate.
    ClickCoordinateAction` — an agent that reads the point off a screenshot
    converts before emitting this action.
    """

    __registry_key__ = "browser.type_coordinate"

    #: The control the text went into, whether its value changed (never the
    #: value), the clear's keys, and the focus afterwards.
    Effect = TextInputEffect

    # ⭐ capability-side configurable side-effect
    clear_by_default: Annotated[
        bool,
        PydanticField(description="Whether to clear the target field first"),
    ] = True

    class Params(BaseModel):
        """The text, and where to click first to focus a field: an optional point.

        Both axes or neither, as ``scroll``'s start; the Android
        ``type_coordinate`` takes the same params (§4.9, "相对名就是参数契约").
        """

        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[
            int | None,
            PointAxis("coordinate", "x"),
            PydanticField(description="CSS px from the viewport's left edge"),
        ] = None
        coordinate_y: Annotated[
            int | None,
            PointAxis("coordinate", "y"),
            PydanticField(description="CSS px from the viewport's top edge"),
        ] = None
        text: str
        #: Omitted means ``clear_by_default`` — see ``TypeAction.Params``.
        clear: bool | None = None

        @model_validator(mode="after")
        def _both_axes(self) -> Self:
            if (self.coordinate_x is None) != (self.coordinate_y is None):
                raise ValueError("a typing point needs both coordinate_x and coordinate_y")
            return self

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Type into a text field. With a point, click there first to focus the field; "
            "without one, type into the field that already has focus.",
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
                coordinate_x=params.get("coordinate_x"),
                coordinate_y=params.get("coordinate_y"),
                text=params["text"],
                clear=(
                    self.clear_by_default if params.get("clear") is None else bool(params["clear"])
                ),
            )
            try:
                await dispatch_request(scope, event)
            except RuntimeError as exc:
                # The handler answers with an ``error`` when the click that
                # should focus the field failed, when no text field has focus
                # or focus stayed away from the point (it then types nothing,
                # and still answers the effect it measured), or when the
                # typing failed: the text never went in, so the action failed.
                effect = effect_from(self, handler_field(event, "effect_facts"))
                if effect is not None and event.coordinate_x is not None:
                    # Refused after the click: the click ran, and the page
                    # gets the same settle any clicked page gets. Refused
                    # without a point, nothing happened to settle.
                    await self.settle()
                return ActionResult(success=False, error=str(exc), effect=effect)
            effect = effect_from(self, handler_field(event, "effect_facts"))
            await self.settle()
            return ActionResult(
                success=True,
                typed_into_sensitive_field=handler_field(event, "sensitive"),
                effect=effect,
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except (TypeError, ValueError) as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))
