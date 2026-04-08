"""Browser profile presets — named combinations of cleaner + serializer + watchdogs.

Usage::

    profile = resolve_profile("standard")
    profile = resolve_profile("standard", override={"cleaner": "skyvern"})
    profile = resolve_profile("standard", watchdogs_add=["permissions"])
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrowserProfileConfig(BaseModel):
    """Configuration for a browser session's component stack."""

    cleaner: str | None = "browser_use"
    serializer: str | None = "text"
    watchdogs: list[str] = Field(default_factory=lambda: list(_DEFAULT_WATCHDOGS))

    # SecurityWatchdog configuration.
    security_allowed_domains: list[str] | None = None
    security_blocked_domains: list[str] | None = None


_DEFAULT_WATCHDOGS: list[str] = [
    "dom", "action", "screenshot", "crash", "downloads", "popups", "security",
]

BUILTIN_PROFILES: dict[str, BrowserProfileConfig] = {
    "standard": BrowserProfileConfig(
        cleaner="browser_use",
        serializer="text",
        watchdogs=list(_DEFAULT_WATCHDOGS),
    ),
    "skyvern": BrowserProfileConfig(
        cleaner="skyvern",
        serializer="html",
        watchdogs=["dom", "action", "screenshot", "crash", "popups"],
    ),
    "vision_only": BrowserProfileConfig(
        cleaner=None,
        serializer=None,
        watchdogs=["action", "screenshot", "crash", "popups"],
    ),
    "lightweight": BrowserProfileConfig(
        cleaner="browser_use",
        serializer="text",
        watchdogs=["dom", "action", "screenshot"],
    ),
}


def resolve_profile(
    name: str,
    *,
    override: dict[str, str | None] | None = None,
    watchdogs_add: list[str] | None = None,
    watchdogs_remove: list[str] | None = None,
) -> BrowserProfileConfig:
    """Resolve a named profile with optional field overrides.

    Parameters
    ----------
    name:
        Built-in profile name (standard, skyvern, vision_only, lightweight).
    override:
        Override individual fields (cleaner, serializer).
    watchdogs_add:
        Watchdog types to add to the base list.
    watchdogs_remove:
        Watchdog types to remove from the base list.
    """
    if name not in BUILTIN_PROFILES:
        available = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"Unknown browser profile {name!r}. Available: {available}")

    profile = BUILTIN_PROFILES[name].model_copy(deep=True)

    if override:
        for key, value in override.items():
            if key in ("cleaner", "serializer"):
                setattr(profile, key, value)

    if watchdogs_add:
        for wd in watchdogs_add:
            if wd not in profile.watchdogs:
                profile.watchdogs.append(wd)

    if watchdogs_remove:
        profile.watchdogs = [w for w in profile.watchdogs if w not in watchdogs_remove]

    return profile
