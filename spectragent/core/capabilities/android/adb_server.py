"""This run's own adb server, on a private loopback port, with its own key.

``docs/architecture_design.md`` §11.2 ("主机传输层", "两把钥匙"). ``adb connect``
registers a device in a server every run of the user shares, which breaks zero
global state, so a run starts its own server and every device of the run is
reached through it. Measured on adb 1.0.41 (2026-11-01): a server reads and
writes ``$HOME/.android/adbkey`` whatever port it listens on, and generates a
pair when there is none, so a ``HOME`` of the run's own gives the server a key
of the run's own and leaves the user's ``~/.android/adbkey``, which also
unlocks the user's phones, untouched. adb reads no other variable for that
directory.

The server runs in the foreground (``adb server nodaemon``), in a process
group of the run's own that ends with this process (``processes.py``). It serves
only the devices that register with it, such as an emulator started with
``ANDROID_ADB_SERVER_PORT`` pointing here: ``ADB_EMU=0``, ``ADB_USB=0`` and
``ADB_MDNS=0`` turn off its scans of the default emulator ports, of USB and of
the network, so it never reaches another run's emulator, nor a phone on this
machine whose owner would be asked to trust the run's key. ``ADB_VENDOR_KEYS``
(more keys for the server to offer) and the variables that would point the
server at another address or socket are not passed on; adb reads
``ANDROID_ADB_SERVER_ADDRESS`` even when ``-P`` names the port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from spectragent.core.capabilities.android.processes import ProcessGroup, finish, log_tail

#: Where the server listens and clients connect: loopback only.
HOST = "127.0.0.1"
#: Variables of this process the server must not inherit.
_NOT_INHERITED = (
    "ADB_VENDOR_KEYS",
    "ADB_SERVER_SOCKET",
    "ANDROID_ADB_SERVER_ADDRESS",
    "ANDROID_ADB_SERVER_PORT",
)
#: Turns off a server's scans of the default emulator ports, of USB and of the network.
NO_SCANS: Mapping[str, str] = MappingProxyType({"ADB_EMU": "0", "ADB_USB": "0", "ADB_MDNS": "0"})
_POLL_S = 0.05


def server_environment(home: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The environment of a server whose ``HOME`` (and so key) is *home*, built on *base*."""
    env = {name: value for name, value in base.items() if name not in _NOT_INHERITED}
    env.update(HOME=str(home), **NO_SCANS)
    return env


@dataclass
class PrivateAdbServer:
    """An adb server this run started; the only one its devices are reached through.

    Attributes:
        port: The loopback port it listens on.
        home: Its ``HOME``; its key is ``home/.android/adbkey``.
        group: The process group it runs in, which ends with this process.
        process: Its process.
    """

    port: int
    home: Path
    group: ProcessGroup
    process: asyncio.subprocess.Process

    @property
    def log(self) -> Path:
        return _log(self.home)

    @property
    def public_key(self) -> Path:
        """The key the server offers devices, which an emulator must be handed too."""
        return self.home / ".android" / "adbkey.pub"

    @classmethod
    async def start(
        cls, adb: str, home: Path, *, ready_timeout_s: float = 10.0
    ) -> PrivateAdbServer:
        """Start ``adb server nodaemon`` with *home* as its ``HOME``, on a free loopback port.

        Ready once the port accepts a connection and the server has written its
        key pair, which a device started after it must be handed.

        Raises:
            RuntimeError: The server exited before it was ready.
            TimeoutError: It was not ready within *ready_timeout_s*.
        """
        port = free_port()
        group = await ProcessGroup.open()
        try:
            process = await group.spawn(
                [adb, "-P", str(port), "server", "nodaemon"],
                env=server_environment(home, os.environ),
                log=_log(home),
            )
        except BaseException:
            await finish(asyncio.ensure_future(group.close(None, grace_s=0)))
            raise
        server = cls(port=port, home=home, group=group, process=process)
        try:
            await asyncio.wait_for(server._ready(), timeout=ready_timeout_s)
        except BaseException as failure:
            error = failure
            if isinstance(failure, TimeoutError):
                error = TimeoutError(
                    f"the adb server on port {port} was not ready within {ready_timeout_s:g} s:\n"
                    f"{log_tail(server.log)}"
                )
            try:
                await finish(asyncio.ensure_future(server.stop()))
            except Exception as exc:
                error.add_note(f"stopping the adb server also failed: {exc}")
            if error is failure:
                raise
            raise error from None
        return server

    async def stop(self, *, grace_s: float = 5.0) -> None:
        """Stop the server, then its process group; it holds no state worth more than SIGTERM."""
        await self.group.close(self.process, grace_s=grace_s)

    async def _ready(self) -> None:
        while True:
            if self.process.returncode is not None:
                raise RuntimeError(
                    f"the adb server exited with code {self.process.returncode} before it "
                    f"listened on port {self.port}:\n{log_tail(self.log)}"
                )
            if self.public_key.is_file() and await _accepts(self.port):
                return
            await asyncio.sleep(_POLL_S)


def _log(home: Path) -> Path:
    return home / "adb-server.log"


async def _accepts(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection(HOST, port)
    except OSError:
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def free_port() -> int:
    """A loopback port nothing listens on now; whoever binds it next may lose it to another process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]
