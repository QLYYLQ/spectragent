"""The tap injectors (``docs/architecture_design.md`` §11.2 "策略轴"; the axis is in ``_registrar.py``).

Both programs take a stroke the same way (``device/clients.py``) and differ in
how it reaches the app, measured on the API 33 emulator (2026-11-01): the
service dispatches a stroke of two points as one gesture and a longer path one
segment after another, each waiting for the system to report the one before it
done (a 350 ms four-point path took 390-486 ms); the helper injects each event
at its planned time, moving every 8 ms (the same path, 372 ms). Taps took about
5 ms beyond their hold on both.
"""

from __future__ import annotations

from spectragent.core.capabilities.android.device.clients import Stroke
from spectragent.core.capabilities.android.input._registrar import (
    AndroidTapInjectorBase,
    DevicePrograms,
)


class AccessibilityGestureInjector(AndroidTapInjectorBase):
    """Strokes dispatched by our accessibility service (``dispatchGesture``).

    The system cancels every one on a device that does not declare
    ``android.hardware.faketouch``; setup's test tap measures that
    (``AndroidPlatformObs.gestures``).
    """

    __registry_key__ = "accessibility_gesture"

    async def inject(self, device: DevicePrograms, stroke: Stroke) -> int:
        return await device.service.gesture([stroke])


class ShellInjectionInjector(AndroidTapInjectorBase):
    """Strokes injected by the shell helper (``injectInputEvent``), each event at its planned time."""

    __registry_key__ = "shell_injection"

    async def inject(self, device: DevicePrograms, stroke: Stroke) -> int:
        return await device.helper.gesture(stroke)
