"""The Android capability's observation fragments (``docs/architecture_design.md`` §11.2, "片段").

One capture serves every negotiated fragment of an observation cycle:

- ``android.screenshot``: the screen, with the four sizes of
  :class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry`, the
  program that took it and what it cost on the device. Both programs capture
  the display in its current orientation at the resolution touches take points
  in, so the viewport is the image's own size;
- ``android.foreground_app``: the app and window in front, as the window
  manager reports them;
- ``android.platform``: the operating system the input reaches (a closed
  ``Literal``, so ``prepare()`` can read it with ``typing.get_args``), with the
  device facts measured on the device and never inferred from the host: its
  build, its input method, the faketouch check and the test tap, and the
  device-side APK that runs there (§11.2, "实验身份");
- ``android.capture_state``: the platform-neutral capture state. A phone has
  no pointer and these reads measure no failed resources, so both are
  ``unavailable``; the settle wait is the capability's "still"
  (``settle.py``); the capability never moves a focus of its own, so the
  foreground change is always ``None``;
- ``android.ui_tree``: the cleaned UI tree, in android_env's
  ``AndroidAccessibilityForest`` form, the elements the configured cleaner
  numbered, the configured serializer's text of them, and the tree source that
  captured it (``producer``, §14.4). Captured only in a run that negotiates it.

The screenshot, the foreground app and the UI tree also carry how the wait
before their capture ended, and the device's clock and the app in front when
it was taken (:class:`AndroidCaptureFragment`), in the trace and never shown
to the agent: a branch run reads them to pin the device clock and to compare
the restored screen with the recorded one (§11.2 "分支回放").
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, field_serializer, model_validator

from spectragent.core.capabilities.android.device.clients import ScreenshotProducer
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.presentation import element_description
from spectragent.core.capabilities.android.ui_tree.models import UITree, UITreeElement
from spectragent.core.presentation import ElementDescription
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
UI_TREE = "android.ui_tree"


class DeviceAtCapture(BaseModel):
    """The device's clock and the app in front when one capture was taken.

    Read on the device in the capture, beside its other sources: the clock with
    ``date``, the app and window from the window manager, as
    ``android.foreground_app`` reports them (the same reading, when the run
    negotiates that fragment). ``None`` fields are facts the device did not
    report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The device's wall clock, in ms since the epoch.
    clock_ms: int
    #: The focused app's package.
    package: str | None = None
    #: Its focused activity, the class name in full.
    activity: str | None = None
    #: The window with input focus: the app's, or a system window over it.
    window: str | None = None


class AndroidCaptureFragment(FragmentBase):
    """What every fragment of one capture of the screen shares: the wait before it, and the device then.

    One capture serves every negotiated fragment of an observation cycle,
    after one wait for a still screen (``settle.py``). ``settle`` records that
    wait: whether it ended still, at its limit or on an error, and how long
    it took. ``device`` records the device's clock and the app in front
    (:class:`DeviceAtCapture`), so a recording carries both whatever the run
    negotiated (§11.2 "分支回放"). The capability sets both on each fragment
    of the capture, the same records on each, so a reader finds them on
    whichever of these fragments the run negotiated, ``capture_state`` or
    not. No agent renders them: the browser's ``settle`` is the same record
    (``docs/architecture_design.md`` §4.14, §11.2). ``settle`` is ``None``
    when the wait is turned off (``settle_max_wait_s: 0``); ``device`` when
    reading it failed, which fails nothing else of the capture (a fact only
    the recording needs never costs the agent its observation); either when
    no capture served the fragment (a test built it). A ``None`` record is
    left out of the trace JSON.
    """

    __abstract__ = True

    settle: SettleState | None = None
    device: DeviceAtCapture | None = None

    def trace_json_exclude(self) -> set[str]:
        # Left out when there is no record, so a capture without one writes
        # what it always wrote.
        excluded = super().trace_json_exclude()
        absent = {name for name in ("settle", "device") if getattr(self, name) is None}
        return excluded | absent


class AndroidScreenshotObs(AndroidCaptureFragment):
    """The screen; its viewport is the screen in the pixels taps are given in."""

    __registry_key__ = SCREENSHOT
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    image_bytes: bytes | None = None
    image_width: int = 0
    image_height: int = 0
    viewport_width: int = 0
    viewport_height: int = 0
    #: The program that took the image: the shell helper where its screenshot
    #: works (API 31-33), else the accessibility service.
    producer: ScreenshotProducer | None = None
    #: What it cost on the device, in ms: taking the image, encoding the PNG,
    #: and waiting first to keep the service's one screenshot per 333 ms.
    capture_ms: int | None = None
    encode_ms: int | None = None
    waited_ms: int | None = None

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
        return f"{self.viewport_width}x{self.viewport_height} by {self.producer or 'unknown'}"


class AndroidForegroundAppObs(AndroidCaptureFragment):
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
    #: The API level, as the device-side programs report it.
    sdk_int: int | None = None
    #: The current input method, which changes what a screenshot shows while typing.
    input_method: str | None = None
    #: Whether the device declares ``android.hardware.faketouch``; without it the
    #: system cancels accessibility gestures (§11.2).
    faketouch: bool | None = None
    #: Whether setup's test tap through the accessibility service completed:
    #: whether accessibility gestures work on this device.
    gestures: bool | None = None
    #: The device-side APK that runs: its SHA-256 as the service computed it on
    #: the device, which setup checked against the configured build's sidecar.
    service_apk_sha256: str | None = None
    #: Its ``versionName`` and ``versionCode``, as the service reported them.
    service_apk_version: str | None = None
    #: Whether the system takes the service for an accessibility tool
    #: (``isAccessibilityTool``), as it read the APK; ``None`` before API 31.
    accessibility_tool: bool | None = None
    #: The SHA-256 of the certificate that signed it, from the configured
    #: build's sidecar: the same build, since its SHA-256 matched.
    service_apk_signing_certificate_sha256: str | None = None

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


class AndroidUITreeObs(AndroidCaptureFragment):
    """The screen's UI tree, as the configured cleaner kept it and the configured serializer printed it.

    ``forest`` and ``elements`` are the cleaned tree
    (:class:`~spectragent.core.capabilities.android.ui_tree.models.UITree`): the
    captured ``AndroidAccessibilityForest`` holding only the nodes the cleaner
    kept, each with its bounds and fields as captured, ``is_important``
    included, and the numbers it gave them, which ``click`` and ``scroll``
    take as ``index``. ``serialized`` is what an agent reads; its format is the
    serializer's to describe (``AndroidCapability.fragment_formats``).
    ``producer`` is the tree source that captured it (``accessibility_service``),
    in case it ever changes during a run (§14.4). The raw tree is not here:
    ``node_count`` and ``omitted_offscreen`` say how large it was.
    """

    __registry_key__ = UI_TREE
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    producer: str | None = None
    forest: AndroidAccessibilityForest = AndroidAccessibilityForest()
    elements: tuple[UITreeElement, ...] = ()
    serialized: str = ""
    #: How many nodes the source captured, before cleaning.
    node_count: int = 0
    #: How many off-screen nodes the source left out, each with everything
    #: under it, past its node limit.
    omitted_offscreen: int = 0
    #: How long the capture took on the device, in ms.
    capture_ms: int | None = None

    @model_validator(mode="after")
    def _elements_name_kept_nodes(self) -> Self:
        self.tree()
        return self

    def tree(self) -> UITree:
        """The cleaned tree: ``forest`` and ``elements`` together."""
        return UITree(forest=self.forest, elements=self.elements)

    def describe_element(self, index: int) -> ElementDescription:
        """Element *index* as a person reads it, with its bounds (``core/presentation.py``)."""
        return element_description(self.tree(), index)

    def summary(self) -> str:
        return f"{len(self.elements)} elements of {self.node_count} nodes"
