"""The Android capability's actions.

A relative name is a param contract (``docs/architecture_design.md`` §4.9,
"相对名就是参数契约"): negotiation binds names, never params, so an action that
means what a browser action means takes its relative name and exactly its
params. Six do: ``click``, ``click_coordinate``, ``drag``, ``type_coordinate``,
``scroll`` and ``wait``; typing into the focused field is ``type_coordinate``
without a point (user ruling 2026-11-01). Three are Android's own (§11.2, "动作"):
``long_press``, ``press_key`` (system keys) and ``open_app`` (by name; the
name-to-package table is this action's, never an agent's). Points are physical
screen pixels, the space touches take and the screenshot's viewport. A
gesture's duration is a setting on the action instance, never a model param.
There is no ``done``: the Loop's ``loop.done`` ends a run (§4.5).

Every touch is one finger's timed stroke (``device/clients.py``) that the
capability's configured tap injector delivers, and the action reports it in its
effect (``effects.py``), injector included (§11.2, "实验身份"). A tap holds the
finger down for :data:`~spectragent.core.capabilities.android.device.clients.TAP_MS`.
Keys go through the shell helper; text through the configured typing backend.

``click``'s ``index`` and ``scroll``'s names an element of ``android.ui_tree``,
numbered by the configured cleaner (user ruling 2026-11-01): the capability
turns it into the centre of the element's bounds on the screen
(``AndroidCapability.element_centre``), and the meaning stays here, never with
an agent. A run that does not negotiate the tree is offered no ``click``
(negotiation leaves out an action whose required param addresses a fragment
the run lacks) and a ``scroll`` without ``index`` (§4.5 "参数的含义"). A
scroll's start point and direction mean what they mean on the browser:
``down`` shows what is further down, so the finger moves up.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement, PointAxis
from spectragent.core.capabilities.android.device.clients import TAP_MS, FocusedNode, Stroke
from spectragent.core.capabilities.android.effects import (
    AndroidTextEffect,
    AndroidTouchEffect,
    Gesture,
    ScreenPoint,
    Touch,
)
from spectragent.core.capabilities.android.ui_tree.models import ElementIndexError
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.capability import AndroidCapability

_X = "Physical px from the screen's left edge"
_Y = "Physical px from the screen's top edge"


async def touch(
    capability: AndroidCapability,
    gesture: Gesture,
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int,
) -> Touch:
    """Deliver one straight stroke through the capability's tap injector and say what it was."""
    device = capability.bridge.input
    elapsed_ms = await device.stroke(Stroke.line(start, end, duration_ms))
    return Touch(
        gesture=gesture,
        start=ScreenPoint(x=start[0], y=start[1]),
        end=ScreenPoint(x=end[0], y=end[1]),
        duration_ms=duration_ms,
        elapsed_ms=elapsed_ms,
        delivered_by=device.injector,
    )


@dataclass
class AndroidClickAction(ActionBase):
    """Tap an element of the UI tree: the centre of its bounds on the screen."""

    __registry_key__ = "android.click"
    Effect = AndroidTouchEffect

    class Params(BaseModel):
        """The browser's ``click`` params: an element index, of ``android.ui_tree`` here.

        The param is required and addresses the tree, so the whole action
        needs the tree: a run without it has no index to tap.
        """

        model_config = ConfigDict(extra="forbid")

        index: Annotated[
            int,
            AddressesElement("android.ui_tree"),
            PydanticField(description="element index from the UI tree"),
        ]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Tap a UI element by its index in the UI tree.", **context)

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        index = self.Params.model_validate(params).index
        try:
            point = capability.element_centre(index)
        except ElementIndexError as exc:
            return ActionResult(success=False, error=str(exc))
        tapped = await touch(capability, "tap", point, point, TAP_MS)
        return ActionResult(
            success=True, effect=AndroidTouchEffect(effect_id=self.action_id, touch=tapped)
        )


@dataclass
class AndroidClickCoordinateAction(ActionBase):
    """Tap one point of the screen."""

    __registry_key__ = "android.click_coordinate"
    Effect = AndroidTouchEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[int, PointAxis("coordinate", "x"), PydanticField(description=_X)]
        coordinate_y: Annotated[int, PointAxis("coordinate", "y"), PydanticField(description=_Y)]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Tap a point on the screen.", **context)

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        point = (given.coordinate_x, given.coordinate_y)
        tapped = await touch(capability, "tap", point, point, TAP_MS)
        return ActionResult(
            success=True, effect=AndroidTouchEffect(effect_id=self.action_id, touch=tapped)
        )


@dataclass
class AndroidLongPressAction(ActionBase):
    """Press and hold one point of the screen."""

    __registry_key__ = "android.long_press"
    Effect = AndroidTouchEffect

    duration_ms: Annotated[
        int,
        PydanticField(
            ge=1,
            description=(
                "How long the finger stays down, in ms; Android takes a press held longer "
                "than about 500 ms as a long press."
            ),
        ),
    ] = 1000

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[int, PointAxis("coordinate", "x"), PydanticField(description=_X)]
        coordinate_y: Annotated[int, PointAxis("coordinate", "y"), PydanticField(description=_Y)]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Press and hold a point on the screen, for a context menu or to pick an item up.",
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        point = (given.coordinate_x, given.coordinate_y)
        pressed = await touch(capability, "long_press", point, point, self.duration_ms)
        return ActionResult(
            success=True, effect=AndroidTouchEffect(effect_id=self.action_id, touch=pressed)
        )


@dataclass
class AndroidDragAction(ActionBase):
    """Touch one point, move to another, lift."""

    __registry_key__ = "android.drag"
    Effect = AndroidTouchEffect

    duration_ms: Annotated[
        int,
        PydanticField(
            ge=1, description="How long the finger takes from one point to the other, in ms."
        ),
    ] = 1000

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        start_x: Annotated[int, PointAxis("start", "x"), PydanticField(description=_X)]
        start_y: Annotated[int, PointAxis("start", "y"), PydanticField(description=_Y)]
        end_x: Annotated[int, PointAxis("end", "x"), PydanticField(description=_X)]
        end_y: Annotated[int, PointAxis("end", "y"), PydanticField(description=_Y)]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Touch one point on the screen, move to a second one and lift, for sliders and "
            "drag-and-drop.",
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        dragged = await touch(
            capability,
            "drag",
            (given.start_x, given.start_y),
            (given.end_x, given.end_y),
            self.duration_ms,
        )
        return ActionResult(
            success=True, effect=AndroidTouchEffect(effect_id=self.action_id, touch=dragged)
        )


#: What a scroll's start does, in the parenthesis of its two params: framing
#: rewrites a point param's description into the model's grid and keeps it.
_START_HINT = "the swipe starts there; never with index"


@dataclass
class AndroidScrollAction(ActionBase):
    """Swipe to scroll: from the start point, from the centre of an element, or from the middle of the screen."""

    __registry_key__ = "android.scroll"
    Effect = AndroidTouchEffect

    default_amount: Annotated[
        int,
        PydanticField(ge=1, description="Swipe distance in px when the agent gives no amount"),
    ] = 1000
    max_amount: Annotated[
        int, PydanticField(ge=1, description="Per-call cap on the swipe distance in px")
    ] = 2400
    duration_ms: Annotated[
        int,
        PydanticField(
            ge=1,
            description=(
                "How long the swipe takes, in ms. A faster swipe flings the content further "
                "than its own distance."
            ),
        ),
    ] = 500

    class Params(BaseModel):
        """The browser's ``scroll`` params; ``index`` names an element of ``android.ui_tree``."""

        model_config = ConfigDict(extra="forbid")

        direction: Literal["up", "down", "left", "right"]
        amount: int | None = None
        index: Annotated[
            int | None,
            AddressesElement("android.ui_tree"),
            PydanticField(description="scroll inside specific element"),
        ] = None
        start_x: Annotated[int | None, PointAxis("start", "x"), PydanticField(description=_X)] = (
            None
        )
        start_y: Annotated[int | None, PointAxis("start", "y"), PydanticField(description=_Y)] = (
            None
        )

        @model_validator(mode="after")
        def _one_target(self) -> Self:
            if (self.start_x is None) != (self.start_y is None):
                raise ValueError("a scroll start needs both start_x and start_y")
            if self.index is not None and self.start_x is not None:
                raise ValueError("scroll takes an element index or a start point, not both")
            return self

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            f"Scroll the screen by swiping (default {self.default_amount}px). down shows what "
            f"is further down.",
            hints={"start_x": _START_HINT, "start_y": _START_HINT},
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        width, height = capability.screen_size
        start = (width // 2, height // 2)
        if given.start_x is not None and given.start_y is not None:
            start = (given.start_x, given.start_y)
        elif given.index is not None:
            try:
                start = capability.element_centre(given.index)
            except ElementIndexError as exc:
                return ActionResult(success=False, error=str(exc))
        distance = max(1, min(given.amount or self.default_amount, self.max_amount))
        # The content follows the finger, so it moves against the direction scrolled.
        dx, dy = {
            "down": (0, -distance),
            "up": (0, distance),
            "right": (-distance, 0),
            "left": (distance, 0),
        }[given.direction]
        end = (min(max(start[0] + dx, 0), width - 1), min(max(start[1] + dy, 0), height - 1))
        swiped = await touch(capability, "swipe", start, end, self.duration_ms)
        return ActionResult(
            success=True, effect=AndroidTouchEffect(effect_id=self.action_id, touch=swiped)
        )


#: How often typing reads again whether a text field has focus while it waits
#: for one; each reading is two reports of the device, about 0.1-0.2 s.
FOCUS_POLL_S = 0.1


def _field(node: FocusedNode) -> str:
    """A focused node for an error: its class, its view id, its package; never where it is."""
    name = node.class_name.rsplit(".", 1)[-1] if node.class_name else "a node"
    if node.view_id_resource_name:
        name += f" {node.view_id_resource_name}"
    if node.package_name:
        name += f" of {node.package_name}"
    return name


@dataclass
class AndroidTypeCoordinateAction(ActionBase):
    """Type into a text field: tap it first when given a point, else the one that has focus.

    The point is optional, both axes or neither, as on the browser (user ruling
    2026-11-01). Either way, before any text is entered the device is asked
    whether a text field has focus (:meth:`~spectragent.core.capabilities.android.bridge.
    ForegroundApps.text_focus`), again every :data:`FOCUS_POLL_S` while it
    answers that none has, for up to ``focus_wait_ms``: a field may take focus a
    moment after the tap, or after an earlier action that opened it. When none
    has by then, the action fails without entering anything, as the browser's
    does; a device that cannot tell is typed into as asked.

    With a point, the rule is the browser's too (user ruling 2026-11-02,
    ``docs/architecture_design.md`` §4.9 "相对名就是参数契约"): our accessibility
    service names the field with input focus before the tap and again after it
    (:meth:`~spectragent.core.capabilities.android.bridge.InputInjection.focused_field`).
    Focus that moved to a text field is typed into, wherever that field is: a
    tap on Settings' search bar opens a screen whose field, away from the
    point, takes focus. Focus that stayed on the field that had it, with the
    point outside that field when tapped, is refused: the tap landed on the
    caption of another field, and nothing is cleared or typed. The field is
    the one the person sees (user ruling 2026-11-02): the focused node's
    largest ancestor whose subtree holds no other editable node, such as the
    layout an EditText shares with its label or hint, which the service
    reports with the focused node before the tap (a keyboard that opens moves
    it afterwards); a tap on the view that labels the field
    (``android:labelFor``) counts as in it too. A tap is in the field only
    where the field's own window is on top: the status bar drawn over an
    app's full-screen root is not part of a field there. Focus may leave the
    old field a moment after the tap, so the wait covers that too; a service
    that names no field with focus by then cannot tell which field it is, and
    the text is entered as asked. A refusal still reports the tap
    (``AndroidTextEffect``).
    """

    __registry_key__ = "android.type_coordinate"
    Effect = AndroidTextEffect

    clear_by_default: Annotated[
        bool, PydanticField(description="Whether to clear the target field first")
    ] = True
    focus_wait_ms: Annotated[
        int,
        PydanticField(
            ge=0,
            description=(
                "How long typing waits, in ms, while no text field has focus yet (after its tap, "
                "or at once without a point) and, with a point, while focus stays on the field "
                "that had it before the tap, with the point outside that field as drawn, or the "
                "service does not name the field with focus yet. Read again every 0.1 s. When "
                "the wait runs out, typing fails without entering text, except where the service "
                "still names no field: it cannot tell then, and enters the text as asked. A tap "
                "that opens a screen takes a while: on the API 33 emulator, Settings' search "
                "field had focus 1.8-2.8 s after a tap on its bar on a still page, and up to "
                "4.6 s after a tap right after Settings started."
            ),
        ),
    ] = 5000

    class Params(BaseModel):
        """The browser's ``type_coordinate`` params: the text, and an optional point."""

        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[
            int | None, PointAxis("coordinate", "x"), PydanticField(description=_X)
        ] = None
        coordinate_y: Annotated[
            int | None, PointAxis("coordinate", "y"), PydanticField(description=_Y)
        ] = None
        text: str
        #: Omitted means ``clear_by_default``.
        clear: bool | None = None

        @model_validator(mode="after")
        def _both_axes(self) -> Self:
            if (self.coordinate_x is None) != (self.coordinate_y is None):
                raise ValueError("a typing point needs both coordinate_x and coordinate_y")
            return self

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Type into a text field. With a point, tap there first to focus the field; "
            "without one, type into the field that already has focus.",
            hints={"clear": f"default={str(self.clear_by_default).lower()}"},
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        device = capability.bridge.input
        no_field = "No text field has focus"
        point: tuple[int, int] | None = None
        before: FocusedNode | None = None
        tapped: Touch | None = None
        if given.coordinate_x is not None and given.coordinate_y is not None:
            point = (given.coordinate_x, given.coordinate_y)
            before = await device.focused_field(field_bounds=True, at=point)
            tapped = await touch(capability, "tap", point, point, TAP_MS)
            no_field = "No text field took focus at the tapped point"
        where = await self._no_field_at(capability, point, before)
        if where is not None:
            return ActionResult(
                success=False,
                error=f"{no_field}: {where}, so no text was typed.",
                effect=AndroidTextEffect(effect_id=self.action_id, tap=tapped, text_entered=False),
            )
        clear = self.clear_by_default if given.clear is None else given.clear
        target = await device.enter_text(given.text, replace=clear)
        return ActionResult(
            success=True,
            effect=AndroidTextEffect(
                effect_id=self.action_id,
                tap=tapped,
                text_entered=True,
                entered_by=device.typing,
                replaced=clear,
                field_package=target.package_name,
                field_class=target.class_name,
                field_view_id=target.view_id_resource_name,
            ),
        )

    async def _no_field_at(
        self,
        capability: AndroidCapability,
        point: tuple[int, int] | None,
        before: FocusedNode | None,
    ) -> str | None:
        """Where focus is when no text should go in (at *point*, *before* the tap); ``None``: it should.

        Read again every :data:`FOCUS_POLL_S` while that is so, or while the
        service names no field with focus, up to ``focus_wait_ms``; a service
        that still names none then cannot tell, and the answer is ``None``.
        """
        deadline = time.monotonic() + self.focus_wait_ms / 1000
        while True:
            where, untold = await self._no_field_now(capability, point, before)
            if (where is None and not untold) or time.monotonic() >= deadline:
                return where
            await asyncio.sleep(max(0.0, min(FOCUS_POLL_S, deadline - time.monotonic())))

    async def _no_field_now(
        self,
        capability: AndroidCapability,
        point: tuple[int, int] | None,
        before: FocusedNode | None,
    ) -> tuple[str | None, bool]:
        """Where focus is when no text should go in now, and whether the service could not tell.

        Focus on a text field is refused only when it stayed on the field
        *before* named, with the point outside that field as drawn when tapped.
        """
        focus = await capability.bridge.apps.text_focus()
        if focus.text_field is False:
            if focus.window is None:
                return "nothing has focus", False
            return f"focus is in {focus.window}, not on a text field", False
        if point is None or before is None or before.field_holds(*point) is not False:
            # A text field has focus, and no field had it before the tap with
            # the point measured outside it: none, one the point was in, or
            # one whose bounds the service could not give.
            return None, False
        node = await capability.bridge.input.focused_field()
        if node is None:
            return None, True
        if node.same_node(before):
            return f"focus stayed on {_field(node)}, away from the tapped point", False
        return None, False


SystemKey = Literal["back", "home", "enter", "delete", "recent_apps"]
#: Each system key, as Android names its key code.
KEYCODES: dict[SystemKey, str] = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "recent_apps": "KEYCODE_APP_SWITCH",
}


@dataclass
class AndroidPressKeyAction(ActionBase):
    """Press one of the phone's system keys."""

    __registry_key__ = "android.press_key"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        key: SystemKey

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Press a system key: back, home, enter, delete (the character before the cursor) "
            "or recent_apps (the app switcher).",
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        key = self.Params.model_validate(params).key
        await capability.bridge.input.press_key(KEYCODES[key])
        return ActionResult(success=True)


#: The launcher apps of the stock Google APIs image (API 33), by their name on
#: the home screen; read off the device with ``cmd package query-activities``.
STOCK_APPS: dict[str, str] = {
    "Calendar": "com.google.android.calendar",
    "Camera": "com.android.camera2",
    "Chrome": "com.android.chrome",
    "Clock": "com.google.android.deskclock",
    "Contacts": "com.google.android.contacts",
    "Drive": "com.google.android.apps.docs",
    "Files": "com.google.android.documentsui",
    "Gmail": "com.google.android.gm",
    "Google": "com.google.android.googlequicksearchbox",
    "Maps": "com.google.android.apps.maps",
    "Messages": "com.google.android.apps.messaging",
    "Phone": "com.google.android.dialer",
    "Photos": "com.google.android.apps.photos",
    "Settings": "com.android.settings",
    "YouTube": "com.google.android.youtube",
    "YouTube Music": "com.google.android.apps.youtube.music",
}


def stock_apps() -> dict[str, str]:
    """A fresh copy of :data:`STOCK_APPS`: ``open_app``'s default table."""
    return dict(STOCK_APPS)


@dataclass
class AndroidOpenAppAction(ActionBase):
    """Open an app by its name; which package a name means is this action's table."""

    __registry_key__ = "android.open_app"

    apps: Annotated[
        dict[str, str],
        PydanticField(
            description=(
                "The apps an agent can open by name: name -> package. A name matches ignoring "
                "case and extra spaces; an installed package name opens too. The default "
                "lists the launcher apps of the stock Google APIs image."
            )
        ),
    ] = field(default_factory=stock_apps)

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        app_name: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Open an app by its name on the home screen (for example Settings or Clock), or by "
            "its package name.",
            **context,
        )

    def package_of(self, app_name: str) -> str | None:
        """The package *app_name* names in :attr:`apps`, or ``None``."""
        wanted = " ".join(app_name.split()).casefold()
        for name, package in self.apps.items():
            if " ".join(name.split()).casefold() == wanted:
                return package
        return None

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        app_name = self.Params.model_validate(params).app_name
        package = self.package_of(app_name) or app_name.strip()
        launcher = capability.bridge.launcher
        try:
            installed = await launcher.is_installed(package)
        except ValueError:  # neither a known name nor a package name
            installed = False
        if not installed:
            return ActionResult(
                success=False,
                error=(
                    f"no app {app_name!r} is installed; apps by name: "
                    f"{', '.join(sorted(self.apps))}, or an installed package name"
                ),
            )
        await launcher.launch(package)
        return ActionResult(success=True)


@dataclass
class AndroidWaitAction(ActionBase):
    """Let time pass before the next observation; touches nothing on the device."""

    __registry_key__ = "android.wait"
    only_waits = True

    default_seconds: Annotated[
        float,
        PydanticField(ge=0.0, description="Default seconds when the agent omits ``seconds``."),
    ] = 1.0
    max_seconds: Annotated[
        float,
        PydanticField(
            gt=0.0,
            description="Longest wait an agent may ask for; a longer one is refused.",
        ),
    ] = 30.0

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        seconds: float | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            f"Pause for N seconds before the next observation, when the screen is still "
            f"loading or changing. Default={self.default_seconds}s, max={self.max_seconds}s.",
            hints={"seconds": f"default={self.default_seconds}, max={self.max_seconds}"},
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        requested = self.Params.model_validate(params).seconds
        seconds = self.default_seconds if requested is None else requested
        if not 0 <= seconds <= self.max_seconds:
            return ActionResult(
                success=False, error=f"seconds={seconds} is outside 0..{self.max_seconds}"
            )
        await asyncio.sleep(seconds)
        return ActionResult(success=True)


#: Every action of the Android capability, the order its prompt lists them in.
ACTIONS: tuple[type[ActionBase], ...] = (
    AndroidClickAction,
    AndroidClickCoordinateAction,
    AndroidLongPressAction,
    AndroidDragAction,
    AndroidScrollAction,
    AndroidTypeCoordinateAction,
    AndroidPressKeyAction,
    AndroidOpenAppAction,
    AndroidWaitAction,
)
