"""The typing backends (``docs/architecture_design.md`` §11.2 "策略轴"; the axis is in ``_registrar.py``).

Both enter text through our accessibility service, into the field that has
input focus, and take any text. ``set_text`` replaces the field's text in one
action, or sets the text at its selection; ``commit_text`` commits through the
service's own input method at the cursor, and for a replacement selects the
whole text first through the same input connection, so it never follows a
``set_text``: right after one the field restarts its input, and a commit then
can land on the connection it replaced and be lost without an error (measured,
§11.2).
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
    and no input method is involved.
    """

    __registry_key__ = "set_text"

    def unsupported(self, sdk_int: int) -> str | None:
        return None

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
