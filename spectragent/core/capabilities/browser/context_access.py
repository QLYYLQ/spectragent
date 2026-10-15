"""One BrowserContext's command and event boundary, independent of transport."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_cdp import CDPCommandProtocol


class BrowserContextAccess:
    """Validate browser identifiers before sending and route only owned events.

    This protects cooperating AF extensions from crossing a run boundary. It
    is not a sandbox for arbitrary Python code holding another raw connection.
    """

    def __init__(self, context_id: str, client: CDPCommandProtocol) -> None:
        self.context_id = context_id
        self._client = client
        self._sessions: set[str] = set()
        self._retired_sessions: set[str] = set()
        self._targets: set[str] = set()

    def owns_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def is_retired_session(self, session_id: str) -> bool:
        """True only for a detached session previously owned by this view."""
        return session_id in self._retired_sessions

    async def command(
        self, method: str, params: Mapping[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        result = dict(params or {})
        if session_id is not None and not self.owns_session(session_id):
            raise PermissionError(f"CDP session {session_id!r} does not belong to this run")
        if "browserContextId" in result and result["browserContextId"] != self.context_id:
            raise PermissionError("A CDP command named another BrowserContext")
        target = result.get("targetId")
        if target:
            info = await self._client.send("Target.getTargetInfo", {"targetId": target})
            if (info or {}).get("targetInfo", {}).get("browserContextId") != self.context_id:
                raise PermissionError(f"CDP target {target!r} does not belong to this run")
            self._targets.add(str(target))
        if method == "Target.detachFromTarget":
            sid = str(result.get("sessionId", ""))
            if not (self.owns_session(sid) or self.is_retired_session(sid)):
                raise PermissionError("Cannot detach another run's CDP session")
            return result
        if method in {
            "Target.createTarget",
            "Storage.getCookies",
            "Storage.setCookies",
            "Storage.clearCookies",
            "Browser.setPermission",
            "Browser.resetPermissions",
            "Browser.setDownloadBehavior",
            "Browser.cancelDownload",
        }:
            result["browserContextId"] = self.context_id
            return result
        if method in {"Target.getTargets", "Target.setDiscoverTargets", "Browser.getVersion"}:
            return result
        if method in {
            "Target.getTargetInfo",
            "Target.attachToTarget",
            "Target.activateTarget",
            "Target.closeTarget",
            "Browser.getWindowForTarget",
        }:
            if not target:
                raise PermissionError(f"{method} requires an explicitly owned targetId")
            return result
        if method.startswith(("Browser.", "Target.", "Storage.")):
            if method == "Target.setAutoAttach" and session_id is not None:
                return result
            if (
                method in {"Storage.getStorageKey", "Storage.getStorageKeyForFrame"}
                and session_id is not None
            ):
                return result
            raise PermissionError(f"Browser-global operation {method} is unavailable to a run")
        if session_id is None:
            raise PermissionError(f"CDP operation {method} requires an owned session")
        return result

    def response(self, method: str, result: Any) -> Any:
        if method == "Target.getTargets" and isinstance(result, dict):
            return {
                **result,
                "targetInfos": [
                    info
                    for info in result.get("targetInfos", [])
                    if info.get("browserContextId") == self.context_id
                ],
            }
        return result

    def accept_event(
        self, method: str, params: Mapping[str, Any], parent_session_id: str | None = None
    ) -> bool:
        if method == "Target.attachedToTarget":
            if str(params.get("sessionId", "")) in self._retired_sessions:
                return False
            info = params.get("targetInfo") or {}
            if info.get("browserContextId") != self.context_id:
                return False
            if parent_session_id is not None and not self.owns_session(parent_session_id):
                return False
            self._sessions.add(str(params["sessionId"]))
            self._targets.add(str(info["targetId"]))
            return True
        if method == "Target.detachedFromTarget":
            sid = str(params.get("sessionId", ""))
            if not self.owns_session(sid):
                return False
            self._sessions.discard(sid)
            self._retired_sessions.add(sid)
            return True
        if parent_session_id is not None:
            return self.owns_session(parent_session_id)
        info = params.get("targetInfo")
        if isinstance(info, Mapping):
            if info.get("browserContextId") != self.context_id:
                return False
            self._targets.add(str(info["targetId"]))
            return True
        target = str(params.get("targetId", ""))
        if target and target in self._targets:
            if method == "Target.targetDestroyed":
                self._targets.discard(target)
            return True
        return False
