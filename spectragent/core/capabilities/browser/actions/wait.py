"""WaitAction — pause the agent loop for N seconds before the next observe.

The action emits no CDP event and touches no browser state — it simply
``asyncio.sleep`` s. The ReactLoop's ``observe → think → act → next-observe``
cycle (see ``spectragent/core/loops/react.py``) means the next iteration's
observation will reflect any DOM mutations that happened during the sleep.

Two complementary mechanisms exist for "wait longer":

- :class:`WaitAction` — agent-decision-time. The LLM emits ``browser.wait``
  when it sees evidence the page is still loading or a popup is expected.
- ``post_delay_ms`` on :class:`SettleableAction` — capability-side static
  config, applied unconditionally to every navigation/click/etc. on a known
  slow site.

Use whichever fits the call site. If you have a specific URL that always
needs an extra 5s settle, configure ``post_delay_ms``. If the LLM reasons
about it ("the page is mostly blank, let me wait"), it emits ``browser.wait``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class WaitAction(ActionBase):
    """Pause for N seconds before the next observation."""

    __registry_key__ = "browser.wait"

    # ⭐ capability-side configurable side-effects
    default_seconds: Annotated[
        float,
        PydanticField(
            description="Default seconds when the agent omits ``seconds``.",
            ge=0.0,
        ),
    ] = 1.0

    max_seconds: Annotated[
        float,
        PydanticField(
            description=(
                "Hard cap — requests above this return an error so the LLM "
                "sees the rejection and adjusts. Prevents runaway sleeps "
                "from a confused or adversarial prompt."
            ),
            gt=0.0,
        ),
    ] = 30.0

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        seconds: float | None = None

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            (
                f"Pause for N seconds before the next observation. Use when "
                f"the page is still loading, a popup/modal is expected to "
                f"appear after a delay, or what you were last shown looks "
                f"clearly incomplete. Default={self.default_seconds}s, "
                f"max={self.max_seconds}s."
            ),
            hints={"seconds": f"default={self.default_seconds}, max={self.max_seconds}"},
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            requested = params.get("seconds")
            seconds = float(self.default_seconds if requested is None else requested)
        except (TypeError, ValueError) as exc:
            return ActionResult(
                success=False,
                error=f"Invalid seconds: {exc}",
            )

        if seconds < 0:
            return ActionResult(
                success=False,
                error=f"seconds={seconds} must be >= 0",
            )
        if seconds > self.max_seconds:
            return ActionResult(
                success=False,
                error=(f"seconds={seconds} exceeds max_seconds={self.max_seconds}"),
            )

        await asyncio.sleep(seconds)
        return ActionResult(
            success=True,
            extracted_content=f"waited {seconds}s",
        )
