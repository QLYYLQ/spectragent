"""What the Android actions measurably did (``docs/architecture_design.md`` §4.5 "动作效果", §11.2 "实验身份").

Each step records which tap injector delivered its touches and which typing
backend entered its text (§11.2): the touch and text actions return it in their
effect. The configured strategy is the one used, with no fallback, so these
fields name what ran; they are trace facts, never said to a model.
:meth:`describe` says only what happened on the screen, so two runs that differ
in their injector show their models the same lines.

Points are physical screen pixels, the capability's units; ``describe`` writes
each one with the caller's ``write_point`` (§4.1, "坐标系归框架").
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas.action import ActionEffect, PointWriter, write_viewport_point

#: Which touch a stroke made.
Gesture = Literal["tap", "long_press", "drag", "swipe"]


class ScreenPoint(BaseModel):
    """A point on the screen, in physical pixels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: int
    y: int

    def text(self, write_point: PointWriter) -> str:
        return write_point(self.x, self.y)


class Touch(BaseModel):
    """One touch: where the finger went down and lifted, over how long, and who delivered it.

    ``duration_ms`` is the stroke's own length, the action's setting;
    ``elapsed_ms`` how long the device took to deliver it, from the finger
    going down to the injector's report that it lifted (a timed path chained
    one segment at a time takes longer than its length). ``delivered_by`` is
    the tap injector that delivered it, by its registry key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    gesture: Gesture
    start: ScreenPoint
    end: ScreenPoint
    duration_ms: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    delivered_by: str

    def describe(self, write_point: PointWriter) -> str:
        start, end = self.start.text(write_point), self.end.text(write_point)
        if self.gesture == "tap":
            return f"Tapped {start}."
        if self.gesture == "long_press":
            return f"Pressed {start} for {self.duration_ms} ms."
        verb = "Dragged" if self.gesture == "drag" else "Swiped"
        return f"{verb} from {start} to {end} over {self.duration_ms} ms."


class AndroidTouchEffect(ActionEffect):
    """The touch a tap, a long press, a drag or a scroll made (:class:`Touch`)."""

    touch: Touch

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return self.touch.describe(write_point)


class AndroidTextEffect(ActionEffect):
    """Text entered into the field with input focus, or none: the tap before it, and how it went in.

    ``tap`` is the touch the action made first, when it was given a point; a
    refused action reports it as well, since the tap happened.
    ``text_entered``: whether any text went in. When it did, ``entered_by`` is
    the typing backend, by its registry key, and ``replaced`` whether the text
    replaced the field's own (``clear``) or went at its cursor; the field is
    named as the device reports it (the input method knows only its package).
    The text itself, and its length, are never recorded.
    """

    tap: Touch | None = None
    text_entered: bool
    entered_by: str | None = None
    replaced: bool | None = None
    field_package: str | None = None
    field_class: str | None = None
    field_view_id: str | None = None

    @model_validator(mode="after")
    def _how_only_when_entered(self) -> Self:
        if self.text_entered != (self.entered_by is not None and self.replaced is not None):
            raise ValueError("entered_by and replaced are given exactly when text was entered")
        return self

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        head = f"{self.tap.describe(write_point)} " if self.tap is not None else ""
        if not self.text_entered:
            return f"{head}No text was entered."
        field = self.field_class.rsplit(".", 1)[-1] if self.field_class else "the field"
        if self.field_view_id:
            field += f" {self.field_view_id}"
        if self.field_package:
            field += f" of {self.field_package}"
        how = "replacing its text" if self.replaced else "at its cursor"
        return f"{head}Entered the text into {field}, {how}."
