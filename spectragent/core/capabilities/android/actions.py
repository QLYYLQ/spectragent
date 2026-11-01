"""The Android capability's actions.

A relative name is a param contract (``docs/architecture_design.md`` §4.9,
"相对名就是参数契约"): negotiation binds names, never params, so an action that
means what a browser action means takes its relative name and exactly its
params. Five do: ``click_coordinate``, ``drag``, ``type_coordinate``, ``scroll``
and ``wait``. Four are Android's own (§11.2, "动作"): ``long_press``,
``type_text`` (into the focused field), ``press_key`` (system keys) and
``open_app`` (by name; the name-to-package table is this action's, never an
agent's). Points are physical screen pixels, the space ``input`` takes and the
screenshot's viewport. A gesture's duration is a setting on the action
instance, never a model param. There is no ``done``: the Loop's ``loop.done``
ends a run (§4.5).

``scroll``'s ``index`` names an element of ``android.ui_tree``, which arrives
with a later stage; until then no run negotiates the tree, so the param is
never offered, and its start point and direction mean what they mean on the
browser: ``down`` shows what is further down, so the finger moves up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement, PointAxis
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.capability import AndroidCapability

_X = "Physical px from the screen's left edge"
_Y = "Physical px from the screen's top edge"


@dataclass
class AndroidClickCoordinateAction(ActionBase):
    """Tap one point of the screen."""

    __registry_key__ = "android.click_coordinate"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[int, PointAxis("coordinate", "x"), PydanticField(description=_X)]
        coordinate_y: Annotated[int, PointAxis("coordinate", "y"), PydanticField(description=_Y)]

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Tap a point on the screen.", **context)

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        point = self.Params.model_validate(params)
        await capability.bridge.input.tap(point.coordinate_x, point.coordinate_y)
        return ActionResult(success=True)


@dataclass
class AndroidLongPressAction(ActionBase):
    """Press and hold one point of the screen."""

    __registry_key__ = "android.long_press"

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
        await capability.bridge.input.swipe(point, point, duration_ms=self.duration_ms)
        return ActionResult(success=True)


@dataclass
class AndroidDragAction(ActionBase):
    """Touch one point, move to another, lift."""

    __registry_key__ = "android.drag"

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
        await capability.bridge.input.swipe(
            (given.start_x, given.start_y),
            (given.end_x, given.end_y),
            duration_ms=self.duration_ms,
        )
        return ActionResult(success=True)


#: What a scroll's start does, in the parenthesis of its two params: framing
#: rewrites a point param's description into the model's grid and keeps it.
_START_HINT = "the swipe starts there; never with index"


@dataclass
class AndroidScrollAction(ActionBase):
    """Swipe to scroll: from the start point, or from the middle of the screen."""

    __registry_key__ = "android.scroll"

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
        if given.index is not None:
            return ActionResult(
                success=False,
                error="scrolling inside an element needs the screen's UI tree, and this run has none",
            )
        width, height = capability.screen_size
        start = (
            (given.start_x, given.start_y)
            if given.start_x is not None and given.start_y is not None
            else (width // 2, height // 2)
        )
        distance = max(1, min(given.amount or self.default_amount, self.max_amount))
        # The content follows the finger, so it moves against the direction scrolled.
        dx, dy = {
            "down": (0, -distance),
            "up": (0, distance),
            "right": (-distance, 0),
            "left": (distance, 0),
        }[given.direction]
        end = (min(max(start[0] + dx, 0), width - 1), min(max(start[1] + dy, 0), height - 1))
        await capability.bridge.input.swipe(start, end, duration_ms=self.duration_ms)
        return ActionResult(success=True)


@dataclass
class AndroidTypeCoordinateAction(ActionBase):
    """Tap a text field, then type into it.

    Unlike the browser's ``type_coordinate`` it cannot check that the tap gave a
    text field focus, because adb's built-in commands do not say which view has
    it. When the point misses the field (it lands on the label beside it), the
    clear wipes whatever field still has focus, the text goes there, and the
    action reports success; the browser refuses that case ("No text field took
    focus at the clicked point"). The gap is recorded in
    ``docs/architecture_design.md`` §11.2 with the open question of how UI-TARS's
    ``type`` maps on Android, whose answer may retire this action here, so no
    focus check is written until it is settled.
    """

    __registry_key__ = "android.type_coordinate"

    clear_by_default: Annotated[
        bool, PydanticField(description="Whether to clear the target field first")
    ] = True
    focus_wait_ms: Annotated[
        int,
        PydanticField(
            ge=0,
            description="Pause between the tap and the typing, in ms, for the field to take focus",
        ),
    ] = 500

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        coordinate_x: Annotated[int, PointAxis("coordinate", "x"), PydanticField(description=_X)]
        coordinate_y: Annotated[int, PointAxis("coordinate", "y"), PydanticField(description=_Y)]
        text: str
        #: Omitted means ``clear_by_default``.
        clear: bool | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Tap a text field on the screen to focus it, then type into it.",
            hints={"clear": f"default={str(self.clear_by_default).lower()}"},
            **context,
        )

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        given = self.Params.model_validate(params)
        device = capability.bridge.input
        await device.tap(given.coordinate_x, given.coordinate_y)
        await asyncio.sleep(self.focus_wait_ms / 1000)
        clear = self.clear_by_default if given.clear is None else given.clear
        if clear:
            await device.clear_focused_field()
        await device.type_text(given.text)
        return ActionResult(success=True)


@dataclass
class AndroidTypeTextAction(ActionBase):
    """Type into the field that has focus."""

    __registry_key__ = "android.type_text"

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        text: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe("Type text into the text field that has focus.", **context)

    async def execute(
        self, params: dict[str, Any], *, capability: AndroidCapability
    ) -> ActionResult:
        await capability.bridge.input.type_text(self.Params.model_validate(params).text)
        return ActionResult(success=True)


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
    AndroidClickCoordinateAction,
    AndroidLongPressAction,
    AndroidDragAction,
    AndroidScrollAction,
    AndroidTypeCoordinateAction,
    AndroidTypeTextAction,
    AndroidPressKeyAction,
    AndroidOpenAppAction,
    AndroidWaitAction,
)
