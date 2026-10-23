"""The browser's media theme, measured once before any target is prepared (§4.14).

The recording pins this observed fact for its whole run; a replay takes the
recorded value. It is not a configuration default or an OS setting to compare.
"""

import logging
from typing import Any, Literal

from spectragent.core.capabilities.browser.commands import BrowserCommands, measure_in_attempts

ColorScheme = Literal["light", "dark"]
COLOR_SCHEME_WORLD = "af-color-scheme"
COLOR_SCHEME_PROBE = """(() => {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const light = matchMedia('(prefers-color-scheme: light)').matches;
    return dark === light ? null : dark ? 'dark' : 'light';
})()"""

logger = logging.getLogger(__name__)


def color_scheme_from_record(value: Any) -> ColorScheme | None:
    """A known media theme, or no valid evidence; never infer a light default."""
    return value if isinstance(value, str) and value in ("light", "dark") else None


async def measure_color_scheme(
    client: BrowserCommands, *, timeout: float, attempt_timeout: float
) -> ColorScheme:
    """Read the initial page in an isolated world, before any media override.

    The fresh browser already has an initial page. Attach only for this read,
    then detach even if measurement fails. No page script can change matchMedia
    in the isolated world. A read Chrome does not answer within
    *attempt_timeout* is made again, within *timeout* (``measure_in_attempts``);
    every other failed or ambiguous read raises at once.
    """
    return await measure_in_attempts(
        lambda bound: _read_color_scheme(client, bound),
        timeout=timeout,
        attempt_timeout=attempt_timeout,
        what="the browser's prefers-color-scheme",
    )


async def _read_color_scheme(client: BrowserCommands, timeout: float) -> ColorScheme:
    reply = await client.send("Target.getTargets", {}, None, timeout=timeout)
    pages = [
        info
        for info in (reply or {}).get("targetInfos", [])
        if info.get("type") == "page" and info.get("targetId")
    ]
    if not pages:
        raise RuntimeError("no page target to measure the browser's color scheme in")
    attached = await client.send(
        "Target.attachToTarget",
        {"targetId": pages[0]["targetId"], "flatten": True},
        None,
        timeout=timeout,
    )
    session_id = str((attached or {}).get("sessionId") or "")
    if not session_id:
        raise RuntimeError("Target.attachToTarget returned no sessionId")
    try:
        tree = await client.send("Page.getFrameTree", {}, session_id, timeout=timeout)
        frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
        world = await client.send(
            "Page.createIsolatedWorld",
            {"frameId": frame["id"], "worldName": COLOR_SCHEME_WORLD},
            session_id,
            timeout=timeout,
        )
        evaluated = await client.send(
            "Runtime.evaluate",
            {
                "expression": COLOR_SCHEME_PROBE,
                "returnByValue": True,
                "contextId": (world or {})["executionContextId"],
            },
            session_id,
            timeout=timeout,
        )
        value = ((evaluated or {}).get("result") or {}).get("value")
        scheme = color_scheme_from_record(value)
        if scheme is None or (evaluated or {}).get("exceptionDetails"):
            raise RuntimeError(f"browser reported no valid prefers-color-scheme: {value!r}")
        return scheme
    finally:
        try:
            await client.send(
                "Target.detachFromTarget", {"sessionId": session_id}, None, timeout=timeout
            )
        except Exception as exc:
            logger.debug("color scheme: detaching %s failed: %s", session_id, exc)
