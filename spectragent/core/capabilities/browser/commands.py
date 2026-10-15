"""The command-only CDP surface used by browser startup measurements."""

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
