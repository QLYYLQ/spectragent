"""``none`` — the default tier: nothing is pinned, nothing is sent.

With ``none`` and no archiver bound the capability opens no recorder
connection at all and sends the control connection exactly what it always
did. With archivers bound (and ``require_replayable: false`` on
``browser.network``) the recorder runs and this tier's plan contributes no
command to any target.

Spelled ``none``, the configuration surface's word for "not enabled": a bare
``off`` is a boolean to YAML 1.1 (``docs/architecture_design.md`` §4.14).
"""

from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityBase,
    ReplayabilityPlan,
)


class NoneReplayability(ReplayabilityBase):
    """No launch switches, no launch environment, the platform DPR, an empty plan."""

    __registry_key__ = "none"

    @property
    def replayable(self) -> bool:
        return False

    def launch_args(self) -> tuple[str, ...]:
        return ()

    def launch_env(self) -> dict[str, str]:
        return {}

    @property
    def device_scale_factor(self) -> float:
        return 0.0

    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan:
        return ReplayabilityPlan(tier=self.replayability_id)
