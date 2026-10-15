"""Current control-session ownership for tabs and their out-of-process frames.

This is browser topology, shared by JS execution and element actions. It does
not grant ownership of the browser process or turn scheduler scopes into permissions.
"""

from __future__ import annotations

from typing import Any


class TabSessions:
    """Only live sessions descended from a currently bound tab are addressable."""

    def __init__(self) -> None:
        self._tabs: set[str] = set()
        self._owner_by_child: dict[str, str] = {}
        self._parent_by_child: dict[str, str] = {}

    def bind(self, session_id: str) -> None:
        self._tabs.add(session_id)

    def owns(self, tab_session_id: str, session_id: str) -> bool:
        return tab_session_id in self._tabs and (
            session_id == tab_session_id or self._owner_by_child.get(session_id) == tab_session_id
        )

    def clear(self) -> None:
        self._tabs.clear()
        self._owner_by_child.clear()
        self._parent_by_child.clear()

    def unbind(self, session_id: str) -> None:
        """Revoke a closed tab and every child session before its scopes close."""
        self._tabs.discard(session_id)
        for child, owner in list(self._owner_by_child.items()):
            if owner == session_id:
                self._owner_by_child.pop(child, None)
                self._parent_by_child.pop(child, None)

    def attached(self, params: dict[str, Any], parent_session_id: str | None) -> None:
        """Accept only iframe sessions descended from a currently bound tab."""
        if parent_session_id is None:
            return
        owner = (
            parent_session_id
            if parent_session_id in self._tabs
            else self._owner_by_child.get(parent_session_id)
        )
        if owner is None:
            return
        info = params.get("targetInfo") or {}
        child = params.get("sessionId")
        if info.get("type") != "iframe" or not isinstance(child, str) or not child:
            return
        if child in self._tabs:
            return
        if child in self._owner_by_child:
            # A session cannot migrate to a sibling tab because a later
            # envelope happens to name it. Detach must revoke it first.
            return
        self._owner_by_child[child] = owner
        self._parent_by_child[child] = parent_session_id

    def detached(self, params: dict[str, Any], parent_session_id: str | None) -> None:
        child = params.get("sessionId")
        if isinstance(child, str):
            if child in self._tabs and parent_session_id is None:
                self.unbind(child)
            elif (
                self._parent_by_child.get(child) == parent_session_id
                and parent_session_id is not None
            ):
                self._revoke_subtree(child)

    def _revoke_subtree(self, child: str) -> None:
        """Forget a detached frame even if nested detach events arrive late."""
        pending = [child]
        while pending:
            session = pending.pop()
            pending.extend(
                candidate
                for candidate, parent in self._parent_by_child.items()
                if parent == session
            )
            self._owner_by_child.pop(session, None)
            self._parent_by_child.pop(session, None)
