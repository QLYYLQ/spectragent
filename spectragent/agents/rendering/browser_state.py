"""Render the browser page state: the one writing of URL, title, tabs and media state.

``docs/architecture_design.md`` §4.4, "观测增强按消费方分层". Browser page state
reaches only browser-specific Agents (Skyvern, BrowserUse), which declare the
relative ``browser_meta`` fragment required; a cross-platform Agent never reads
it. Both render it here, followed by the platform-neutral capture state, so the
same page is written the same way whichever browser Agent reads it.

The media playback state (§4.4 "截图的媒体状态") rides on the screenshot
fragment and is written only when the Agent's ``show_media_state`` asks: it is
an explicit experiment variable, off by default.

The pointer in the capture state is the one point among these facts; it is
written by the ``write_point`` the Agent passes (``render_capture_state``), the
framework's writer for this step's frame, so a model that writes points on a
grid reads the pointer on that grid (§4.1, "坐标系归框架"). The default writes
viewport pixels.

Reads are structural, by relative fragment name, like
``render_browser_platform``: no capability or resolver class is imported.
"""

from __future__ import annotations

from typing import Any

from spectragent.agents.rendering.capture_state import render_capture_state, write_viewport_pixels
from spectragent.schemas.action import PointWriter

#: What the model reads when no media state was measured with this step's screenshot.
_MEDIA_NOT_MEASURED = (
    "<media_state>\nThe media playback state was not measured this step.\n</media_state>"
)


def render_browser_state(
    observation: Any,
    *,
    show_media_state: bool,
    can_wait: bool,
    write_point: PointWriter = write_viewport_pixels,
) -> str:
    """``URL``, ``Title`` and ``Tabs`` lines, the ``<capture_state>`` block, then ``<media_state>``.

    Parts are separated by a blank line; a part with nothing to say is left
    out, and ``""`` means there was nothing at all. The caller decides what
    an empty page state reads as and where the element tree goes. The media
    block is written only with *show_media_state*. A known pointer is written
    by *write_point*; *can_wait* is handed to ``render_capture_state``.
    """
    parts: list[str] = []
    meta = getattr(observation, "browser_meta", None)
    if meta is not None:
        url = getattr(meta, "url", "") or ""
        title = getattr(meta, "page_title", "") or ""
        if url:
            parts.append(f"URL: {url}")
        if title:
            parts.append(f"Title: {title}")
        tabs = getattr(meta, "tabs", None) or []
        if tabs:
            tab_info = ", ".join(
                f"[{t.get('tab_id', '?')}] {t.get('title', '')} ({t.get('url', '')})" for t in tabs
            )
            parts.append(f"Tabs: {tab_info}")
    capture_state = render_capture_state(observation, can_wait=can_wait, write_point=write_point)
    if capture_state:
        parts.append(capture_state)
    if show_media_state:
        parts.append(render_media_state(observation))
    return "\n\n".join(parts)


def _clock(seconds: float) -> str:
    whole_minutes, rest = divmod(max(seconds, 0.0), 60.0)
    return f"{int(whole_minutes):02d}:{rest:04.1f}"


def _element_line(element: Any) -> str:
    reading = getattr(element, "before", None) or getattr(element, "after", None)
    where = f"{element.tag} {element.order + 1} in frame {element.frame}"
    if element.live:
        position = "live stream"
    elif reading is None or reading.current_time is None:
        position = "position unknown"
    else:
        duration = element.duration
        total = _clock(duration) if duration is not None else "unknown length"
        position = f"at {_clock(reading.current_time)} of {total}"
    if reading is None or reading.paused is None:
        state = "play state unknown"
    elif reading.ended:
        state = "ended"
    else:
        state = "paused" if reading.paused else "playing"
    if reading is not None and reading.seeking:
        state += ", seeking"
    return f"{where}: {position}, {state}."


def render_media_state(observation: Any) -> str:
    """The ``<media_state>`` block of *observation*'s screenshot; never empty.

    Each audio or video element with its frame, its order, where it stands
    (as read just before the screenshot) and whether it plays. A screenshot
    without a media state, or one whose reads did not answer, says so in one
    sentence: unknown is never written as "no media".
    """
    screenshot = getattr(observation, "screenshot", None)
    media = getattr(screenshot, "media", None) if screenshot is not None else None
    if media is None or not getattr(media, "known", False):
        return _MEDIA_NOT_MEASURED
    lines = [_element_line(element) for element in media.elements]
    if not lines:
        lines.append("No audio or video element on the page.")
    if media.truncated:
        lines.append("More media elements were on the page than were read.")
    unreachable = len(media.unreachable_frames)
    if unreachable:
        lines.append(
            f"{unreachable} embedded frame(s) could not be read; any media in them is unknown."
        )
    return "<media_state>\n" + "\n".join(lines) + "\n</media_state>"


__all__ = ["render_browser_state", "render_media_state"]
