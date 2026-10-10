"""``off`` — the default tier: nothing is pinned, nothing is sent.

With ``off`` and no archiver bound the capability opens no recorder
connection at all and sends the control connection exactly what it always
did. With archivers bound (and ``require_replayable: false`` on
``browser.network``) the recorder runs and this tier's plan contributes no
command to any target.
"""

from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityBase,
    ReplayabilityPlan,
)


class OffReplayability(ReplayabilityBase):
    """No launch switches, the platform DPR, an empty plan."""

    __registry_key__ = "off"

    @property
    def replayable(self) -> bool:
        return False

    def launch_args(self) -> tuple[str, ...]:
        return ()

    @property
    def device_scale_factor(self) -> float:
        return 0.0

    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan:
        return ReplayabilityPlan(tier=self.replayability_id)
