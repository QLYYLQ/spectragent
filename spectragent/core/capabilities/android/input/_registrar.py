"""The Android input axes: the tap injector and the typing backend (``docs/architecture_design.md`` §11.2 "策略轴").

Each is a registrar in the Android package and a receptor on
``AndroidCapability`` (``tap_injector``, ``typing_backend``). Which one a run
uses changes what the device receives, so both are experiment identity, and
neither has a default: a block that leaves one out is refused (§4.2.1, a
strategy whose default would choose behaviour stays required).

- **Tap injector** (``injector_id``): who delivers a touch. Every pointer
  action (a tap, a long press, a drag, a scroll's swipe, the tap before typing
  at a point) is one finger's timed stroke (``device/clients.py``'s
  :class:`~spectragent.core.capabilities.android.device.clients.Stroke`), and
  the injector delivers it: ``accessibility_gesture`` through our
  accessibility service's ``dispatchGesture``, ``shell_injection`` through the
  shell helper's ``injectInputEvent``. An app can tell both from a finger; the
  choice is made for fidelity and stability only (§11.2). The emulator's own
  gRPC touch is a third value, not built yet: it needs the emulator's gRPC
  endpoint (``DeviceNeeds.emulator_grpc``), a gRPC client and its stubs.
- **Typing backend** (``typing_id``): who enters text into the field that has
  input focus: ``set_text`` (``ACTION_SET_TEXT`` from our accessibility
  service, API 30 and up) or ``commit_text`` (our service's own input method,
  API 33 and up, the current keyboard kept). Both take any text, Chinese
  included, unlike ``input text``, which typed only the virtual keyboard's
  characters and is gone with the rest of adb's ``input``.

This module stays light: the package's ``declarations.py`` imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.device.clients import (
        HelperClient,
        ServiceClient,
        Stroke,
        TextTarget,
    )


class DevicePrograms(Protocol):
    """The device-side programs a strategy acts through (``device/lifecycle.py``'s ``DeviceSide``)."""

    @property
    def service(self) -> ServiceClient: ...

    @property
    def helper(self) -> HelperClient: ...


@runtime_checkable
class AndroidTapInjector(Protocol):
    """Delivers one finger's timed stroke to the screen."""

    @property
    def injector_id(self) -> str: ...

    async def inject(self, device: DevicePrograms, stroke: Stroke) -> int:
        """Deliver *stroke* and return, once the finger lifted, how long it took on the device in ms.

        Raises:
            RemoteError: The device refused or cancelled the stroke; the code
                says why (``gesture_cancelled`` on a device without
                ``android.hardware.faketouch``, for the service).
        """
        ...


AndroidTapInjectorRegistrar = create_registrar(
    "android_tap_injector",
    AndroidTapInjector,
    discriminator_field="injector_id",
    inline_only=True,
)


class AndroidTapInjectorBase(metaclass=AndroidTapInjectorRegistrar.Meta):
    """Base of every tap injector; ``injector_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def injector_id(self) -> str:
        return self.__registry_key__


class TypingBackendUnsupported(ValueError):
    """The device's API level cannot run the configured typing backend; the message says why."""


@runtime_checkable
class AndroidTypingBackend(Protocol):
    """Enters text into the field that has input focus."""

    @property
    def typing_id(self) -> str: ...

    def unsupported(self, sdk_int: int) -> str | None:
        """Why this backend cannot enter text on a device of API level *sdk_int*; ``None`` when it can."""
        ...

    async def finds_field(self, device: DevicePrograms) -> bool:
        """Whether this backend finds a field to enter text into now, as the device shows it.

        Typing waits for it within the action's ``focus_wait_ms`` before it
        enters text, once the shared focus rule lets the text in; when the
        wait runs out it enters the text as asked, and :meth:`enter` says why
        it could not (``docs/architecture_design.md`` §11.2, "动作").
        """
        ...

    async def enter(self, device: DevicePrograms, text: str, *, replace: bool) -> TextTarget:
        """Enter *text*: over the field's whole text with *replace*, else at its cursor.

        Returns the field that took it, as the device names it.

        Raises:
            RemoteError: No field took it; the code says why
                (``no_focused_field``).
        """
        ...


AndroidTypingBackendRegistrar = create_registrar(
    "android_typing_backend",
    AndroidTypingBackend,
    discriminator_field="typing_id",
    inline_only=True,
)


class AndroidTypingBackendBase(metaclass=AndroidTypingBackendRegistrar.Meta):
    """Base of every typing backend; ``typing_id`` is the registry key."""

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def typing_id(self) -> str:
        return self.__registry_key__

    async def finds_field(self, device: DevicePrograms) -> bool:
        """Yes: a backend that types through the input connection needs no more than the shared focus rule checked.

        ``set_text`` finds its field otherwise, and says so itself.
        """
        return True
