"""AF's business contracts on agent-cdp request deliveries.

The scheduler knows whether a receiver ran. These validators say whether
that receiver completed the browser operation it promised.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_cdp import BaseEvent, EventExecutionError, EventScope, RequestContract, ResultContract


def action_result(*, marker: str | None = None) -> ResultContract:
    """A void operation succeeds with None; a valued one requires its success marker."""

    def validate(value: Any) -> bool:
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(str(value["error"]))
        if marker is None:
            return value is None
        return isinstance(value, dict) and value.get(marker) is True

    return ResultContract(
        validate, description=f"Browser action returned no successful {marker or 'completion'}"
    )


async def dispatch_request(
    scope: EventScope, event: BaseEvent[Any], *, receiver: str = "browser.action"
) -> None:
    """Dispatch an owned request, check execution, and withdraw work not yet started.

    Only its initiator calls this. An ordinary observer uses ``event.wait``;
    stopping that wait must not cancel another caller's execution. A running
    browser side effect remains running when this owner stops waiting.
    """
    event.request_contract = RequestContract(required_receivers=(receiver,))
    try:
        scope.emit(event)
        await event.wait()
        event.raise_for_status()
    except (TimeoutError, asyncio.CancelledError):
        event.cancel_execution(include_running=False)
        raise
    except EventExecutionError as exc:
        # Preserve the scheduler's delivery evidence as the cause, while the
        # action result names the browser's actual failure to the agent.
        details = "; ".join(str(cause) or type(cause).__name__ for cause in exc.causes)
        if details:
            raise RuntimeError(details) from exc
        raise
