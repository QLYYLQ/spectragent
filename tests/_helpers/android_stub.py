"""A stand-in Android capability, for checking the framework's seams before the real one exists.

Test-only scaffolding: ``docs/execution_dag.md`` M26 runs a stub capability
through the observation-cycle contract and assembles UI-TARS against it before
any Android code is written. It provides what ``docs/architecture_design.md``
§11.2 gives the real capability for an agent that only looks at the screen:

- ``android.screenshot``, carrying the four
  :class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry` sizes
  (the viewport is the physical screen, the unit a tap takes);
- ``android.capture_state``: a phone has no pointer and loads no page
  resources, so both are ``unavailable``; the stub does not wait before a
  capture and follows no new window;
- ``android.platform``, whose ``operating_system`` is ``android``;
- ``click_coordinate``, ``type_coordinate``, ``scroll`` and ``wait`` under the
  browser's relative names and with its params, because a relative name is a
  param contract (§4.9). ``scroll``'s optional ``index`` names an element of
  ``android.ui_tree``, which this stub does not provide, so it is never
  offered;
- ``long_press``, ``open_app`` and ``press_key``, three of the Android-only
  names (§11.2), which UI-TARS's mobile verbs run as. UI-TARS's wiring does not
  name them until the real capability registers them, so its tests hand the
  codec an action space holding these descriptors (:func:`mobile_descriptors`).

The actions are described for negotiation and never executed.

Its device is a script (:class:`StubDevice`): one capture reads the screen
once and all three fragments come from it; a failed capture fails the rest of
its cycle without capturing again (``tests/_helpers/capability_contract.py``).

Every class registers inside :data:`ANDROID_STUB`, a conscribe
``isolated_registries`` capture: a test activates it with
``ANDROID_STUB.activate()``, and no other test's schema sees an ``android``
key. Delete this module when the real capability lands; its contract harness
then goes into ``HARNESS_FACTORIES``.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Self

from conscribe.testing import isolated_registries
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.params import AddressesElement, PointAxis
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.capture_state import (
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)
from tests._helpers.capability_contract import CaptureHarness

SCREENSHOT = "android.screenshot"
CAPTURE_STATE = "android.capture_state"
PLATFORM = "android.platform"
ACTIONS = (
    "android.click_coordinate",
    "android.type_coordinate",
    "android.scroll",
    "android.wait",
)
#: The Android-only actions UI-TARS's mobile verbs run as; no browser action shares a name.
MOBILE_ACTIONS = ("android.long_press", "android.open_app", "android.press_key")

ANDROID_STUB = isolated_registries()


def _png_header(width: int, height: int) -> bytes:
    """The PNG signature and IHDR chunk: enough for every reader of an image's type and size."""
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))


# ── fragments ──────────────────────────────────────────────────────

with ANDROID_STUB:

    class AndroidScreenshotObs(FragmentBase):
        """``android.screenshot``: the screen, and the sizes a point on it needs."""

        __registry_key__ = SCREENSHOT
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        image_bytes: bytes | None = None
        image_width: int = 0
        image_height: int = 0
        viewport_width: int = 0
        viewport_height: int = 0

        def model_image_bytes(self) -> bytes | None:
            return self.image_bytes

    class AndroidCaptureStateObs(CaptureStateFragment):
        """``android.capture_state``: no pointer, no page resources, no wait, no window followed."""

        __registry_key__ = CAPTURE_STATE
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        @property
        def settle_state(self) -> SettleState | None:
            return None

        @property
        def foreground_change(self) -> ForegroundChange | None:
            return None

    class AndroidPlatformObs(FragmentBase):
        """``android.platform``: the operating system the taps and keys reach."""

        __registry_key__ = PLATFORM
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        operating_system: Literal["android"] = "android"


# ── actions: the browser's relative names and params, described, never executed ──

with ANDROID_STUB:

    @dataclass
    class AndroidClickCoordinateAction(ActionBase):
        __registry_key__ = "android.click_coordinate"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            coordinate_x: Annotated[
                int,
                PointAxis("coordinate", "x"),
                PydanticField(description="Physical px from the screen's left edge"),
            ]
            coordinate_y: Annotated[
                int,
                PointAxis("coordinate", "y"),
                PydanticField(description="Physical px from the screen's top edge"),
            ]

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Tap a point on the screen.", **context)

    @dataclass
    class AndroidTypeCoordinateAction(ActionBase):
        __registry_key__ = "android.type_coordinate"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            coordinate_x: Annotated[
                int,
                PointAxis("coordinate", "x"),
                PydanticField(description="Physical px from the screen's left edge"),
            ]
            coordinate_y: Annotated[
                int,
                PointAxis("coordinate", "y"),
                PydanticField(description="Physical px from the screen's top edge"),
            ]
            text: str
            clear: bool | None = None

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Tap a text field on the screen, then type.", **context)

    @dataclass
    class AndroidScrollAction(ActionBase):
        __registry_key__ = "android.scroll"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            direction: Literal["up", "down", "left", "right"]
            amount: int | None = None
            index: Annotated[
                int | None,
                AddressesElement("android.ui_tree"),
                PydanticField(description="scroll inside specific element"),
            ] = None
            start_x: Annotated[
                int | None,
                PointAxis("start", "x"),
                PydanticField(description="Physical px from the screen's left edge"),
            ] = None
            start_y: Annotated[
                int | None,
                PointAxis("start", "y"),
                PydanticField(description="Physical px from the screen's top edge"),
            ] = None

            @model_validator(mode="after")
            def _one_target(self) -> Self:
                if (self.start_x is None) != (self.start_y is None):
                    raise ValueError("a scroll start needs both start_x and start_y")
                if self.index is not None and self.start_x is not None:
                    raise ValueError("scroll takes an element index or a start point, not both")
                return self

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Swipe the screen or an element.", **context)

    @dataclass
    class AndroidWaitAction(ActionBase):
        __registry_key__ = "android.wait"
        only_waits = True

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            seconds: float | None = None

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Pause before the next observation.", **context)


# ── actions only Android has, under the names of §11.2, described, never executed ──

with ANDROID_STUB:

    @dataclass
    class AndroidLongPressAction(ActionBase):
        __registry_key__ = "android.long_press"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            coordinate_x: Annotated[
                int,
                PointAxis("coordinate", "x"),
                PydanticField(description="Physical px from the screen's left edge"),
            ]
            coordinate_y: Annotated[
                int,
                PointAxis("coordinate", "y"),
                PydanticField(description="Physical px from the screen's top edge"),
            ]

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Press and hold a point on the screen.", **context)

    @dataclass
    class AndroidOpenAppAction(ActionBase):
        __registry_key__ = "android.open_app"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            app_name: str

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Open an installed app by its name.", **context)

    @dataclass
    class AndroidPressKeyAction(ActionBase):
        __registry_key__ = "android.press_key"

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

            key: Literal["back", "home", "enter"]

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Press a system key.", **context)


def mobile_descriptors(**context: Any) -> list[ActionDescriptor]:
    """The descriptors of :data:`MOBILE_ACTIONS`, as the stub describes them to a run."""
    actions = (AndroidLongPressAction(), AndroidOpenAppAction(), AndroidPressKeyAction())
    return [action.descriptor(**context) for action in actions]


# ── the capability ─────────────────────────────────────────────────


@dataclass
class StubDevice:
    """The scripted phone behind one :class:`StubAndroidCapability`.

    Attributes:
        width: The screen's width in physical pixels (a Pixel 6 by default).
        height: Its height.
        captures: Screen reads so far, failed ones included.
        broken: Every read from now on fails.
    """

    width: int = 1080
    height: int = 2400
    captures: int = 0
    broken: bool = False


@dataclass
class _Cycle:
    """One observation cycle: its capture once taken, or why taking it failed."""

    fragments: dict[str, FragmentBase] | None = None
    error: Exception | None = None


with ANDROID_STUB:

    class StubAndroidCapability(CapabilityBase):
        """``android``: three fragments from one screen read, seven actions described."""

        __registry_key__ = "android"
        __wiring__: ClassVar[dict[str, Any]] = {
            "observation": {
                "registry": "observation",
                "keys": [SCREENSHOT, CAPTURE_STATE, PLATFORM],
                "declare_only": True,
            },
            "action": {
                "registry": "action",
                "keys": [*ACTIONS, *MOBILE_ACTIONS],
                "declare_only": True,
            },
        }

        # No custom __init__ (CapabilityBase.id must stay visible to conscribe):
        # per-instance state is created on first use.

        @property
        def device(self) -> StubDevice:
            return self.__dict__.setdefault("_device", StubDevice())

        def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
            actions = (
                AndroidClickCoordinateAction(),
                AndroidTypeCoordinateAction(),
                AndroidScrollAction(),
                AndroidWaitAction(),
            )
            return [
                *(action.descriptor(**context) for action in actions),
                *mobile_descriptors(**context),
            ]

        async def begin_observation(self, cycle: int) -> None:
            self.__dict__["_cycle"] = _Cycle()

        async def resolve_fragment(self, fragment_key: str) -> FragmentBase:
            cycle: _Cycle = self.__dict__.setdefault("_cycle", _Cycle())
            if cycle.error is not None:
                raise cycle.error
            if cycle.fragments is None:
                try:
                    cycle.fragments = self._capture()
                except Exception as exc:
                    cycle.error = exc
                    raise
            return cycle.fragments[fragment_key]

        def _capture(self) -> dict[str, FragmentBase]:
            device = self.device
            device.captures += 1
            if device.broken:
                raise RuntimeError(f"screen read {device.captures} failed")
            return {
                SCREENSHOT: AndroidScreenshotObs(
                    image_bytes=_png_header(device.width, device.height),
                    image_width=device.width,
                    image_height=device.height,
                    viewport_width=device.width,
                    viewport_height=device.height,
                ),
                CAPTURE_STATE: AndroidCaptureStateObs(
                    pointer=PointerState(status="unavailable"),
                    failed_visible_resources=FailedVisibleResources(status="unavailable"),
                ),
                PLATFORM: AndroidPlatformObs(),
            }


def android_stub_harness() -> CaptureHarness:
    """The stub under the shared contract: three fragments of one capture, screen reads counted."""
    capability = StubAndroidCapability()
    device = capability.device

    def break_capture() -> None:
        device.broken = True

    return CaptureHarness(
        capability=capability,
        fragments=(SCREENSHOT, CAPTURE_STATE, PLATFORM),
        captures=lambda: device.captures,
        break_capture=break_capture,
    )
