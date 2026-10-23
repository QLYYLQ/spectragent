"""Shared command and batched connection contracts for browser operations."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class BrowserCommands(Protocol):
    """Send a CDP command with the measurement's bounded timeout.

    ``agent_cdp.CDPCommandProtocol`` also requires event subscription and
    connection state. Startup measurements consume only ``send``, whether
    their client is the control connection or the recorder connection.
    """

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...


class PendingCommand(Protocol):
    """A command already written to a browser connection."""

    async def wait(self, timeout: float | None = None) -> Any: ...

    def abandon(self) -> None: ...


class BrowserConnection(BrowserCommands, Protocol):
    """A batched CDP connection shared by recording and initial-state preparation.

    Dedicated transports and context-restricted views satisfy the same contract.
    Ownership stays with the caller; accepting this interface does not transfer
    the connection's lifetime to a storage or recording helper.
    """

    @property
    def is_connected(self) -> bool: ...

    async def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> PendingCommand: ...

    def on_event(self, method: str, callback: Callable[..., Any]) -> None: ...

    def off_event(self, method: str, callback: Callable[..., Any]) -> None: ...

    def add_close_callback(self, callback: Callable[[BaseException | None], None]) -> None: ...

    async def stop(self) -> None: ...


async def measure_in_attempts(
    attempt: Callable[[float], Awaitable[T]],
    *,
    timeout: float,
    attempt_timeout: float,
    what: str,
) -> T:
    """Make a start-up measurement in attempts until one is answered in time (§4.14).

    *attempt* receives how long each of its commands may wait for Chrome:
    *attempt_timeout*, or what is left of *timeout* when that is less. An
    attempt that times out is made again until *timeout* has passed since the
    first one began; then a ``TimeoutError`` names *what*, the attempts made
    and the budget. Any other error ends the measurement at once: an answer
    that was refused or wrong does not improve by asking again.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            return await attempt(min(attempt_timeout, max(deadline - loop.time(), 0.0)))
        except TimeoutError as exc:
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{what}: Chrome gave no answer within {timeout:g}s "
                    f"({attempts} attempts, each allowed {attempt_timeout:g}s)"
                ) from exc
