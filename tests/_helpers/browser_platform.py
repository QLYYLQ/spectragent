"""Internal platform-page replies for narrow CDP test doubles.

These doubles test tab lifecycle or capture routing, not the platform reader.
They still need to provide an explicit OS fact when driving real setup.
"""

from typing import Any

from spectragent.core.capabilities.browser.platform import PLATFORM_URL, PLATFORM_WORLD


class PlatformProbe:
    def __init__(self, operating_system: str = "macOS") -> None:
        self.operating_system = operating_system

    def reply(
        self, method: str, params: dict[str, Any] | None, session_id: str | None
    ) -> dict[str, Any] | None:
        params = params or {}
        if method == "Target.createTarget" and params.get("url") == PLATFORM_URL:
            return {"targetId": "platform-probe-target"}
        if method == "Target.attachToTarget" and params.get("targetId") == "platform-probe-target":
            return {"sessionId": "platform-probe-session"}
        if method == "Target.closeTarget" and params.get("targetId") == "platform-probe-target":
            return {"success": True}
        if session_id != "platform-probe-session":
            return None
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "platform-probe-frame", "url": PLATFORM_URL}}}
        if method == "Page.createIsolatedWorld" and params.get("worldName") == PLATFORM_WORLD:
            return {"executionContextId": 13001}
        if method == "Runtime.evaluate" and params.get("contextId") == 13001:
            return {"result": {"type": "string", "value": self.operating_system}}
        return None
