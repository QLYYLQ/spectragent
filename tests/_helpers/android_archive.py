"""The Android capability on a scripted phone, archiving: shared by the archive contract and the archive tests.

:class:`AndroidArchiveDriver` holds a :class:`~tests._helpers.android_device.ScriptedPhone`
behind a fake ADB server and an ``AndroidCapability`` on it, and sets the
capability up on first use, once a test has bound what it binds (the runtime's
order). Each :meth:`AndroidArchiveDriver.observe` is one observation cycle that
resolves every negotiated fragment (the UI tree unless a test negotiates
otherwise); :meth:`AndroidArchiveDriver.close` tears down what it set up.
"""

from __future__ import annotations

import tempfile
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from unittest import mock

from spectragent.core.archive import ArchiveFile
from spectragent.core.capabilities.android.archive.ui_tree_raw import (
    RawUITreeArchiver,
    RawUITreeSession,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.observations import UI_TREE
from tests._helpers.android_device import (
    ScriptedPhone,
    configured,
    fake_service_apk,
    phone_server,
    settings_forest,
)


class AndroidArchiveDriver:
    """An ``AndroidCapability`` on a scripted phone, set up on first use."""

    def __init__(self, *, fragments: frozenset[str] = frozenset({UI_TREE})) -> None:
        self.phone = ScriptedPhone()
        self.capability = AndroidCapability(service_apk="written at setup", settle_max_wait_s=0)
        self.archivers = (RawUITreeArchiver(),)
        self.fragments = fragments
        self._cycle = 0
        self._resources = AsyncExitStack()
        self._patches = ExitStack()
        self._started = False

    async def ensure_setup(self) -> None:
        """Start the phone's ADB server and set the capability up, negotiated to :attr:`fragments`."""
        if self._started:
            return
        self._started = True
        adb = await self._resources.enter_async_context(phone_server(self.phone))
        directory = self._resources.enter_context(tempfile.TemporaryDirectory())
        self.capability.service_apk = str(fake_service_apk(Path(directory)))
        configured(self.capability, adb)
        await self.capability.setup(negotiation=self.fragments)
        self._resources.push_async_callback(self.capability.teardown)

    async def observe(self) -> None:
        """One observation cycle: one capture, every negotiated fragment served from it."""
        await self.ensure_setup()
        self._cycle += 1
        await self.capability.begin_observation(self._cycle)
        for key in sorted(self.fragments):
            await self.capability.resolve_fragment(key)

    async def produce(self, step: int) -> None:
        """One step: a screen of its own, observed."""
        self.phone.forest = settings_forest((f"Row {step}", (0, 300, 1080, 450)))
        await self.observe()

    def break_collect(self) -> None:
        """Encoding the tree fails from now on: every collection names the loss."""

        async def failing(session: RawUITreeSession) -> list[ArchiveFile]:
            raise OSError("the tree could not be encoded")

        self._patches.enter_context(mock.patch.object(RawUITreeSession, "collect", failing))

    async def close(self) -> None:
        self._patches.close()
        await self._resources.aclose()
