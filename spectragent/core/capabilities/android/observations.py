"""The Android capability's observation fragments (``docs/architecture_design.md`` §11.2, "片段").

One capture serves every negotiated fragment of an observation cycle:

- ``android.screenshot``: the screen, with the four sizes of
  :class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry` and the
  producer that made it. ``screencap`` captures the display in its current
  orientation at the resolution ``input`` takes points in, so the viewport is
  the image's own size;
- ``android.foreground_app``: the app and window in front (§11.2 left this
  fragment's name open);
- ``android.platform``: the operating system the input reaches (a closed
  ``Literal``, so ``prepare()`` can read it with ``typing.get_args``), with the
  device facts measured on the device and never inferred from the host;
- ``android.capture_state``: the platform-neutral capture state. A phone has
  no pointer and these reads measure no failed resources, so both are
  ``unavailable``; the settle wait is the capability's "still"
  (``settle.py``); the capability never moves a focus of its own, so the
  foreground change is always ``None``.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar, Literal, Self

from pydantic import field_serializer

from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.capture_state import (
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)
from spectragent.schemas.observation.image import image_suffix

SCREENSHOT = "android.screenshot"
FOREGROUND_APP = "android.foreground_app"
PLATFORM = "android.platform"
CAPTURE_STATE = "android.capture_state"


class AndroidScreenshotObs(FragmentBase):
    """The screen; its viewport is the screen in the pixels taps are given in."""

    __registry_key__ = SCREENSHOT
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_bytes: bytes | None = None
    image_width: int = 0
    image_height: int = 0
    viewport_width: int = 0
    viewport_height: int = 0
    #: What made the image (``screencap``); another producer is another experiment.
    producer: str = ""

    def model_image_bytes(self) -> bytes | None:
        return self.image_bytes

    @field_serializer("image_bytes")
    def _serialize_image_bytes(self, value: bytes | None) -> str | None:
        return None if value is None else base64.b64encode(value).decode("ascii")

    def trace_binary(self) -> tuple[str, bytes] | None:
        """The image as a file of its own, never base64 inside the JSON sidecar."""
        if not self.image_bytes:
            return None
        return image_suffix(self.image_bytes) or ".png", self.image_bytes

    def trace_json_exclude(self) -> set[str]:
        return super().trace_json_exclude() | {"image_bytes"}

    @classmethod
    def from_trace(cls, payload: dict[str, Any], artifact: bytes | None) -> Self:
        """The inverse of :meth:`trace_binary`: the artifact is ``image_bytes``."""
        return cls.model_validate({**payload, "image_bytes": artifact})

    def summary(self) -> str:
        if not self.viewport_width and not self.viewport_height:
            return ""
        return f"{self.viewport_width}x{self.viewport_height} by {self.producer}"


class AndroidForegroundAppObs(FragmentBase):
    """The app and window in front, as the window manager reports them.

    ``None`` fields are facts the device did not report: no focused app (a
    system window over nothing), no focused window.
    """

    __registry_key__ = FOREGROUND_APP
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    #: The focused app's package.
    package: str | None = None
    #: Its focused activity, the class name in full.
    activity: str | None = None
    #: The window with input focus: the app's, or a system window over it.
    window: str | None = None

    def summary(self) -> str:
        return self.activity or self.package or self.window or ""


class AndroidPlatformObs(FragmentBase):
    """The operating system the input reaches, and facts measured on the device.

    The facts identify the experiment the device ran (§11.2, "实验身份") and are
    measured, never taken from the host; ``None`` is a fact the device did not
    report.
    """

    __registry_key__ = PLATFORM
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    operating_system: Literal["android"] = "android"
    #: ``ro.build.fingerprint``: the build that runs, image and revision.
    fingerprint: str | None = None
    #: The current input method, which changes what a screenshot shows while typing.
    input_method: str | None = None
    #: Whether the device declares ``android.hardware.faketouch``; without it the
    #: system cancels accessibility gestures (§11.2), which a later stage uses.
    faketouch: bool | None = None

    def summary(self) -> str:
        return self.operating_system


class AndroidCaptureStateObs(CaptureStateFragment):
    """How the device stood when it was captured (``capture_state``, §4.4)."""

    __registry_key__ = CAPTURE_STATE
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    pointer: PointerState = PointerState(status="unavailable")
    failed_visible_resources: FailedVisibleResources = FailedVisibleResources(status="unavailable")
    #: The wait before the capture (``settle.py``); ``None`` when the wait is off.
    settle: SettleState | None = None

    @property
    def settle_state(self) -> SettleState | None:
        return self.settle

    @property
    def foreground_change(self) -> ForegroundChange | None:
        return None
