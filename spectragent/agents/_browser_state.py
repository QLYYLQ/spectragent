"""Render the browser page state: the one writing of URL, title and tabs.

``docs/architecture_design.md`` §4.4, "观测增强按消费方分层". Browser page state
reaches only browser-specific Agents (Skyvern, BrowserUse), which declare the
relative ``browser_meta`` fragment required; a cross-platform Agent never reads
it. Both render it here, followed by the platform-neutral capture state, so the
same page is written the same way whichever browser Agent reads it.

Reads are structural, by relative fragment name, like
``render_browser_platform``: no capability or resolver class is imported.
"""

from __future__ import annotations

from typing import Any

from spectragent.agents._capture_state import render_capture_state


def render_browser_state(observation: Any) -> str:
    """``URL``, ``Title`` and ``Tabs`` lines, then the ``<capture_state>`` block.

    Parts are separated by a blank line; a part with nothing to say is left
    out, and ``""`` means there was nothing at all. The caller decides what
    an empty page state reads as and where the element tree goes.
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
    capture_state = render_capture_state(observation)
    if capture_state:
        parts.append(capture_state)
    return "\n\n".join(parts)


__all__ = ["render_browser_state"]
