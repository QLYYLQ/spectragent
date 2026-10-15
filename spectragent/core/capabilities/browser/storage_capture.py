"""Read cookies and frame storage using their actual context and partition keys."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.schemas.browser_storage import (
    BrowserStorageState,
    SessionStorageArea,
    StorageArea,
    StorageFailure,
    StorageTab,
)


def frame_tree_nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    pending = [tree.get("frameTree") or {}]
    frames: list[dict[str, Any]] = []
    while pending:
        node = pending.pop(0)
        if node.get("frame"):
            frames.append(node["frame"])
        pending.extend(node.get("childFrames") or [])
    return frames


class BrowserStorageCapture:
    """Run-owned tab identity and storage reads; no WAL or observation dependency."""

    def __init__(
        self,
        send: Callable[..., Awaitable[Any]],
        targets: Callable[[], dict[str, RecorderTarget]],
        browser_context_id: str | None,
        tabs: Mapping[str, str] | None = None,
    ) -> None:
        self.send = send
        self.targets = targets
        self.browser_context_id = browser_context_id
        self._tabs: dict[str, str] = dict(tabs or {})

    def _next_tab(self) -> str:
        index = 0
        while f"tab-{index}" in self._tabs.values():
            index += 1
        return f"tab-{index}"

    def _tab(self, target: RecorderTarget, targets: dict[str, RecorderTarget]) -> str:
        current = target
        seen: set[str] = set()
        while current.type != "page":
            parent = current.parent_session_id
            if parent is None or parent in seen or parent not in targets:
                raise ValueError("iframe has no owning page session")
            seen.add(parent)
            current = targets[parent]
        if current.target_id not in self._tabs:
            self._tabs[current.target_id] = self._next_tab()
        return self._tabs[current.target_id]

    async def read(self, *, timeout: float = 1.0) -> BrowserStorageState:
        send = self.send
        params = {"browserContextId": self.browser_context_id} if self.browser_context_id else {}
        reply = await send("Storage.getCookies", params, None, timeout=timeout)
        state = BrowserStorageState(cookies=(reply or {}).get("cookies") or [])
        targets = self.targets()
        # Allocate page identities before reading OOPIFs, independent of frame order.
        for target in targets.values():
            if target.type == "page":
                state.tabs.append(StorageTab(tab=self._tab(target, targets), url=target.url))
        local: set[str] = set()
        session: set[tuple[str, str]] = set()
        frames: set[str] = set()
        oopifs = {target.target_id for target in targets.values() if target.type == "iframe"}
        # An OOPIF's own session owns its frame, even if the parent's tree lists it.
        ordered = sorted(targets.values(), key=lambda t: t.type != "iframe")
        for target in ordered:
            if target.type not in {"page", "iframe"}:
                continue
            try:
                tab = self._tab(target, targets)
                tree = await send("Page.getFrameTree", {}, target.session_id, timeout=timeout)
            except Exception as exc:
                state.unavailable.append(
                    StorageFailure(target=target.target_id, operation="frames", error=str(exc))
                )
                continue
            for frame in frame_tree_nodes(tree or {}):
                fid = str(frame.get("id") or "")
                origin = str(frame.get("securityOrigin") or "")
                if (
                    (target.type == "page" and fid in oopifs)
                    or fid in frames
                    or not origin.startswith(("http://", "https://"))
                ):
                    continue
                try:
                    key = await send(
                        "Storage.getStorageKey",
                        {"frameId": fid},
                        target.session_id,
                        timeout=timeout,
                    )
                    storage_key = key["storageKey"]
                    world = await send(
                        "Page.createIsolatedWorld",
                        {"frameId": fid, "worldName": "af-storage-evidence"},
                        target.session_id,
                        timeout=timeout,
                    )
                    result = await send(
                        "Runtime.evaluate",
                        {
                            "expression": "Array.from(location.ancestorOrigins)",
                            "contextId": world["executionContextId"],
                            "returnByValue": True,
                        },
                        target.session_id,
                        timeout=timeout,
                    )
                    ancestors = result["result"]["value"]
                    frames.add(fid)
                    common = {
                        "storage_key": storage_key,
                        "origin": origin,
                        "ancestor_origins": ancestors,
                    }
                    for is_local in (True, False):
                        if (is_local and storage_key in local) or (
                            not is_local and (tab, storage_key) in session
                        ):
                            continue
                        values = await send(
                            "DOMStorage.getDOMStorageItems",
                            {"storageId": {"storageKey": storage_key, "isLocalStorage": is_local}},
                            target.session_id,
                            timeout=timeout,
                        )
                        items = dict(values.get("entries") or [])
                        if is_local:
                            local.add(storage_key)
                            state.local.append(StorageArea(**common, items=items))
                        else:
                            session.add((tab, storage_key))
                            state.session.append(SessionStorageArea(**common, tab=tab, items=items))
                except Exception as exc:
                    state.unavailable.append(
                        StorageFailure(
                            target=target.target_id, frame=fid, operation="storage", error=str(exc)
                        )
                    )
        state.local.sort(key=lambda area: area.storage_key)
        state.session.sort(key=lambda area: (area.tab, area.storage_key))
        return state
