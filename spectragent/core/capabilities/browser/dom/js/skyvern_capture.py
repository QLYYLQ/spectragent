"""Capture a tab's DOM through the separate JS execution scope.

Only JavaScript Runtime commands travel through that scope. CDP node IDs are
resolved while the returned JS objects are still alive, and every observation
gets a fresh object group and isolated-world context for child documents.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from spectragent.core.capabilities.browser.dom.capture import (
    MAX_FRAME_DEPTH,
    FrameTarget,
)
from spectragent.core.capabilities.browser.dom.cleaning import DOMCaptureSource
from spectragent.core.capabilities.browser.dom.js.skyvern_extraction import EXTRACTION_SCRIPT
from spectragent.core.capabilities.browser.dom.models import TargetCapture

logger = logging.getLogger(__name__)


async def capture_skyvern(source: DOMCaptureSource) -> TargetCapture:
    """Build JS trees for the tab, its documents and attached frame targets."""
    if source.session_id is None:
        raise RuntimeError("Skyvern DOM extraction requires a tab CDP session")
    return await _capture_target(
        source,
        source.session_id,
        frame=None,
        depth=0,
        deadline=asyncio.get_running_loop().time() + source.frame_timeout_s,
    )


async def _capture_target(
    source: DOMCaptureSource,
    session_id: str,
    *,
    frame: FrameTarget | None,
    depth: int,
    deadline: float,
) -> TargetCapture:
    oop: list[FrameTarget] = []
    if source.frames is not None:
        try:
            oop = await asyncio.wait_for(
                source.frames.children(session_id),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError:
            logger.warning("Skyvern frame discovery timed out for session %s", session_id)
    oop_by_id = {item.target_id: item for item in oop}
    frame_tree = await source.cdp.send("Page.getFrameTree", {}, session_id=session_id)
    root_entry = (frame_tree or {}).get("frameTree")
    if not isinstance(root_entry, dict):
        raise RuntimeError("Page.getFrameTree returned no root frame for JS extraction")
    capture = await _capture_frame_entry(
        source,
        session_id,
        root_entry,
        oop_by_id=oop_by_id,
        depth=depth,
        deadline=deadline,
    )
    capture.target_id = frame.target_id if frame is not None else None
    capture.namespace = frame.namespace if frame is not None else 0
    # The document stamp: the root frame this extraction read (§4.14).
    root_frame = root_entry.get("frame") or {}
    capture.loader_id = root_frame.get("loaderId")
    capture.url = root_frame.get("url")

    # Some Chrome versions omit OOP descendants from Page.getFrameTree on the
    # parent renderer. The target attachment itself is authoritative.
    for child in oop:
        if not _contains_frame(capture, child.target_id) and depth < MAX_FRAME_DEPTH:
            nested = await _capture_oop(source, child, depth=depth + 1, deadline=deadline)
            if nested is not None:
                capture.frames[child.target_id] = nested
                await _record_owner(source, capture, session_id, child.target_id, deadline)
    return capture


def _contains_frame(capture: TargetCapture, frame_id: str) -> bool:
    return frame_id in capture.frames or any(
        _contains_frame(child, frame_id) for child in capture.frames.values()
    )


async def _record_owner(
    source: DOMCaptureSource,
    capture: TargetCapture,
    session_id: str,
    frame_id: str,
    deadline: float,
) -> None:
    try:
        reply = await asyncio.wait_for(
            source.cdp.send("DOM.getFrameOwner", {"frameId": frame_id}, session_id=session_id),
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
    except Exception:
        logger.debug("Could not resolve owner of frame %s", frame_id, exc_info=True)
        return
    backend_id = (reply or {}).get("backendNodeId")
    if isinstance(backend_id, int):
        capture.raw_dom.setdefault("skyvern_frame_owners", {})[frame_id] = backend_id


async def _capture_frame_entry(
    source: DOMCaptureSource,
    session_id: str,
    entry: dict[str, Any],
    *,
    oop_by_id: dict[str, FrameTarget],
    depth: int,
    deadline: float,
) -> TargetCapture:
    frame_id = (entry.get("frame") or {}).get("id")
    if not isinstance(frame_id, str):
        raise RuntimeError("Page.getFrameTree returned a frame without an id")

    reply = await source.run_js(
        "Page.createIsolatedWorld",
        {"frameId": frame_id, "worldName": "all4one-dom-extraction"},
        session_id,
    )
    context_id = (reply or {}).get("executionContextId")
    if not isinstance(context_id, int):
        raise RuntimeError(f"No JS execution context for frame {frame_id}")

    capture = await _capture_document(source, session_id, context_id=context_id, frame_id=frame_id)
    if depth >= MAX_FRAME_DEPTH:
        return capture

    for child_entry in entry.get("childFrames") or []:
        child_id = (child_entry.get("frame") or {}).get("id")
        if not isinstance(child_id, str):
            continue
        if child_id in oop_by_id:
            child = await _capture_oop(
                source, oop_by_id.pop(child_id), depth=depth + 1, deadline=deadline
            )
        else:
            try:
                child = await asyncio.wait_for(
                    _capture_frame_entry(
                        source,
                        session_id,
                        child_entry,
                        oop_by_id=oop_by_id,
                        depth=depth + 1,
                        deadline=deadline,
                    ),
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
            except (TimeoutError, RuntimeError) as exc:
                logger.warning("Skyvern JS frame %s unavailable: %s", child_id, exc)
                child = None
        if child is not None:
            capture.frames[child_id] = child
            await _record_owner(source, capture, session_id, child_id, deadline)
    return capture


async def _capture_oop(
    source: DOMCaptureSource, frame: FrameTarget, *, depth: int, deadline: float
) -> TargetCapture | None:
    try:
        return await asyncio.wait_for(
            _capture_target(source, frame.session_id, frame=frame, depth=depth, deadline=deadline),
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
    except (TimeoutError, RuntimeError) as exc:
        logger.warning("Skyvern JS target %s unavailable: %s", frame.target_id, exc)
        return None


async def _capture_document(
    source: DOMCaptureSource,
    session_id: str,
    *,
    context_id: int | None,
    frame_id: str | None,
) -> TargetCapture:
    group = f"all4one-dom-{uuid4().hex}"
    params: dict[str, Any] = {
        "expression": EXTRACTION_SCRIPT,
        "returnByValue": False,
        "objectGroup": group,
    }
    if context_id is not None:
        params["contextId"] = context_id
    try:
        reply = await source.run_js("Runtime.evaluate", params, session_id)
        remote_id = ((reply or {}).get("result") or {}).get("objectId")
        if not isinstance(remote_id, str):
            raise RuntimeError("JS DOM extraction returned no remote object")

        data_reply = await source.run_js(
            "Runtime.callFunctionOn",
            {
                "objectId": remote_id,
                "functionDeclaration": "function(){return {tree:this.tree,scroll:this.scroll};}",
                "returnByValue": True,
                "objectGroup": group,
            },
            session_id,
        )
        data = ((data_reply or {}).get("result") or {}).get("value")
        if not isinstance(data, dict) or not isinstance(data.get("tree"), dict):
            raise RuntimeError("JS DOM extraction returned no element tree")

        refs_reply = await source.run_js(
            "Runtime.callFunctionOn",
            {
                "objectId": remote_id,
                "functionDeclaration": "function(){return this.refs;}",
                "returnByValue": False,
                "objectGroup": group,
            },
            session_id,
        )
        refs_id = ((refs_reply or {}).get("result") or {}).get("objectId")
        if not isinstance(refs_id, str):
            raise RuntimeError("JS DOM extraction returned no node references")
        props = await source.run_js(
            "Runtime.getProperties", {"objectId": refs_id, "ownProperties": True}, session_id
        )
        remote_refs = {
            int(item["name"]): item["value"]["objectId"]
            for item in (props or {}).get("result", [])
            if isinstance(item, dict)
            and str(item.get("name", "")).isdigit()
            and isinstance(item.get("value"), dict)
            and isinstance(item["value"].get("objectId"), str)
        }

        semaphore = asyncio.Semaphore(32)

        async def describe(index: int, object_id: str) -> tuple[int, dict[str, Any]]:
            async with semaphore:
                result = await source.cdp.send(
                    "DOM.describeNode", {"objectId": object_id}, session_id=session_id
                )
            node = (result or {}).get("node")
            if not isinstance(node, dict):
                raise RuntimeError(f"DOM.describeNode failed for JS reference {index}")
            return index, node

        resolved = await asyncio.gather(
            *(describe(index, object_id) for index, object_id in remote_refs.items())
        )
        return TargetCapture(
            raw_dom={
                "skyvern_tree": data["tree"],
                "skyvern_scroll": data.get("scroll") or {},
                "skyvern_nodes": dict(resolved),
                "skyvern_frame_id": frame_id,
            },
            snapshot={},
            ax_tree={},
            session_id=session_id,
        )
    finally:
        try:
            await source.run_js("Runtime.releaseObjectGroup", {"objectGroup": group}, session_id)
        except Exception:
            logger.debug("Could not release JS DOM object group %s", group, exc_info=True)
