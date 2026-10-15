"""Shared command and batched connection contracts for browser operations."""

from collections.abc import Callable
from typing import Any, Protocol


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
