"""The typing backends (``docs/architecture_design.md`` §11.2 "策略轴"; the axis is in ``_registrar.py``).

Each enters text into the field that has input focus. ``set_text`` and
``commit_text`` go through our accessibility service and take any text.
``set_text`` replaces the field's text in one action, or sets the text at its
selection; ``commit_text`` commits through the service's own input method at
the cursor, and for a replacement selects the whole text first through the
same input connection, so it never follows a ``set_text``: right after one the
field restarts its input, and a commit then can land on the connection it
replaced and be lost without an error (measured, §11.2). ``key_events`` types
through the shell helper as AndroidWorld types (``adb shell input text``), one
key event after another, so a page that listens for keys sees them.
"""

from __future__ import annotations

from spectragent.core.capabilities.android.device.clients import TextTarget
from spectragent.core.capabilities.android.input._registrar import (
    AndroidTypingBackendBase,
    DevicePrograms,
)

#: The first API level whose accessibility services have an input method of their own.
COMMIT_TEXT_SDK = 33


class SetTextBackend(AndroidTypingBackendBase):
    """``ACTION_SET_TEXT`` on the editable field with input focus, from our accessibility service.

    The field's text changes in one step, without the keyboard: no key event
    and no input method is involved. The field is the editable node the
    service's ``focus`` names, which can lag the input method's connection:
    after a tap in a WebView the page gives its field focus a moment later,
    while the connection of a keyboard already up stays.
    """

    __registry_key__ = "set_text"

    def unsupported(self, sdk_int: int) -> str | None:
        return None

    async def finds_field(self, device: DevicePrograms) -> bool:
        node = await device.service.focus()
        return node is not None and node.is_editable

    async def enter(self, device: DevicePrograms, text: str, *, replace: bool) -> TextTarget:
        return await device.service.set_text(text, replace=replace)


class CommitTextBackend(AndroidTypingBackendBase):
    """Text committed through our service's own input method (API 33 and up), the current keyboard kept."""

    __registry_key__ = "commit_text"

    def unsupported(self, sdk_int: int) -> str | None:
        if sdk_int >= COMMIT_TEXT_SDK:
            return None
        return (
            f"an accessibility service has an input method of its own from API {COMMIT_TEXT_SDK} "
            f"(Android 13); this device runs API {sdk_int}"
        )

    async def enter(self, device: DevicePrograms, text: str, *, replace: bool) -> TextTarget:
        return await device.service.commit_text(text, replace=replace)


class KeyEventsBackend(AndroidTypingBackendBase):
    """Key events injected by the shell helper, as AndroidWorld's ``adb shell input text`` types.

    The device's virtual keyboard key map turns each character into the keys a
    keyboard presses for it, shift with a capital and Enter for a newline, so
    the app gets key events: a page that reads ``keydown`` (MiniWoB's
    ``terminal``) sees every key, which the service's two backends never send.
    Only characters the key map has keys for can be typed: text with any other
    (Chinese, an accented letter) is refused and no key is pressed, where
    AndroidWorld drops those characters and types the rest. A combining accent
    counts as having no key: the map's are dead keys, which would put the
    accent on the character typed after them. A replacement is
    ctrl+A then Delete before the text, as AndroidWorld clears a field. The keys
    go to the window with focus and the helper learns nothing of the field, so
    the effect names none.
    """

    __registry_key__ = "key_events"

    def unsupported(self, sdk_int: int) -> str | None:
        return None

    async def enter(self, device: DevicePrograms, text: str, *, replace: bool) -> TextTarget:
        await device.helper.type_text(text, replace=replace)
        return TextTarget()
