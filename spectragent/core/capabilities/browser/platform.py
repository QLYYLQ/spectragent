"""Actual desktop input platform, read from Chrome's internal OS facts (§4.13).

UA, navigator fields and the agent process's platform are not fallback sources:
all may describe something other than the browser that receives the key events.
"""

import asyncio
import logging
from typing import Any, Literal, Protocol

InputPlatform = Literal["macos", "windows", "linux"]
PLATFORM_URL = "chrome://version/"
PLATFORM_WORLD = "af-input-platform"
PLATFORM_PROBE = "document.getElementById('os_type')?.textContent || null"
PLATFORM_TIMEOUT_S = 5.0
_CLOSE_TIMEOUT_S = 2.0

logger = logging.getLogger(__name__)


class PlatformClient(Protocol):
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...


def input_platform_from_record(value: Any) -> InputPlatform | None:
    """Read a recorded contract value without inventing an unknown platform."""
    return value if isinstance(value, str) and value in ("macos", "windows", "linux") else None


def platform_from_os_text(value: str) -> InputPlatform:
    """Normalize Chrome's OS field, never its user agent or a hardware model."""
    text = value.strip().casefold()
    if text == "macos" or text.startswith(("macos ", "mac os x")):
        return "macos"
    if text == "windows" or text.startswith("windows "):
        return "windows"
    if text == "linux" or text.startswith("linux "):
        return "linux"
    raise ValueError(f"unsupported browser operating system {value!r}")


async def _close_probe(client: PlatformClient, target_id: str) -> None:
    async def close() -> None:
        await client.send("Target.closeTarget", {"targetId": target_id}, timeout=_CLOSE_TIMEOUT_S)
        while True:
            targets = await client.send("Target.getTargets", {}, timeout=_CLOSE_TIMEOUT_S)
            if all(t.get("targetId") != target_id for t in (targets or {}).get("targetInfos", [])):
                return
            await asyncio.sleep(0.02)

    cleanup = asyncio.create_task(asyncio.wait_for(close(), _CLOSE_TIMEOUT_S))
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except Exception:
            break
    if cancellation is not None:
        try:
            cleanup.result()
        except BaseException:
            logger.warning("Could not close cancelled platform probe", exc_info=True)
        raise cancellation
    cleanup.result()


async def measure_input_platform(
    client: PlatformClient,
    *,
    timeout: float = PLATFORM_TIMEOUT_S,
) -> InputPlatform:
    """Measure once before business targets are adopted; close only our temporary target.

    The OS cell is populated by Chrome's browser-side version UI, independently
    of user-agent overrides. Internal pages may be blocked by a CDP provider;
    that is a measurement failure, not permission to guess Linux or the agent OS.
    """
    target_id: str | None = None
    measured: InputPlatform
    try:
        async with asyncio.timeout(timeout):
            created = await client.send(
                "Target.createTarget", {"url": PLATFORM_URL, "background": True}, timeout=timeout
            )
            target_id = (created or {}).get("targetId")
            if not target_id:
                raise RuntimeError("platform probe created no target")
            attached = await client.send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=timeout
            )
            sid = (attached or {}).get("sessionId")
            if not sid:
                raise RuntimeError("platform probe attached no session")
            while True:
                tree = await client.send("Page.getFrameTree", {}, sid, timeout=timeout)
                frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
                if str(frame.get("url", "")).rstrip("/") == PLATFORM_URL.rstrip("/"):
                    world = await client.send(
                        "Page.createIsolatedWorld",
                        {"frameId": frame["id"], "worldName": PLATFORM_WORLD},
                        sid,
                        timeout=timeout,
                    )
                    reply = await client.send(
                        "Runtime.evaluate",
                        {
                            "expression": PLATFORM_PROBE,
                            "returnByValue": True,
                            "contextId": (world or {})["executionContextId"],
                        },
                        sid,
                        timeout=timeout,
                    )
                    if (reply or {}).get("exceptionDetails"):
                        raise RuntimeError("Chrome's internal OS field could not be read")
                    value = ((reply or {}).get("result") or {}).get("value")
                    if isinstance(value, str) and value.strip():
                        measured = platform_from_os_text(value)
                        break
                await asyncio.sleep(0.05)
    except BaseException as primary:
        if target_id:
            try:
                await _close_probe(client, target_id)
            except asyncio.CancelledError:
                if isinstance(primary, asyncio.CancelledError):
                    raise primary from None
                raise
            except Exception:
                logger.warning("Could not close failed platform probe", exc_info=True)
        raise
    else:
        await _close_probe(client, target_id)
        return measured
