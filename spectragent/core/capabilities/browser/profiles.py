"""Browser profile presets — what a run captures, and at what cost.

A profile says which watchdogs to **leave out**, not which to install.
Enumerating them was one of the nine places a new observation fragment
had to be registered by hand: every ``BUILTIN_PROFILES`` entry listed
watchdog names, so a newly registered watchdog was silently absent from
all of them. Inverting it means a new watchdog joins every profile and
costs nothing unless its fragment is negotiated active — cost control
belongs to negotiation (``BrowserStateRequestEvent.fragments``), not to
a hand-maintained name list.

The DOM *cleaner* is deliberately not here. It used to be a strategy key
on this model, which meant the cleaner arrived default-constructed and
every knob its config declared (``descendants_occlude``,
``max_occlusion_nodes``, ...) was unreachable from YAML — the same shape
``mouse_trajectory`` was fixed out of. It is a wired receptor on
:class:`~spectragent.core.capabilities.browser.capability.BrowserCapability`
now, so the configured *instance* is what reaches ``DOMWatchdog``.

Usage::

    profile = resolve_profile("standard")
    profile = resolve_profile("standard", override={"serializer": "html"})
    profile = resolve_profile("standard", watchdogs_remove=["popups"])
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrowserProfileConfig(BaseModel):
    """Configuration for a browser session's component stack."""

    serializer: str | None = "text"

    #: Registry keys of watchdogs this profile deliberately does not
    #: install. Everything registered under ``WatchdogRegistrar`` that is
    #: not listed here is installed. Names are validated against the
    #: registry at setup — a typo is a hard error, not a silent no-op.
    watchdogs_exclude: list[str] = Field(default_factory=list)

    # Ask the page which elements carry JS click/mouse listeners (CDP
    # ``getEventListeners`` via Runtime.evaluate) and feed the answer to
    # the cleaner. This is the *only* signal that catches a bare
    # ``<div onclick=...>`` with no role and no semantic tag — it is
    # check #1 in ``dom/filters/interactivity.is_interactive``. Costs one
    # extra Runtime.evaluate plus one DOM.describeNode per hit element
    # per observe, which is why the cheap profiles leave it off.
    js_click_listener_detection: bool = False

    # Capture the documents of cross-site iframes — the ones Chrome runs in
    # another renderer process (captcha widgets, payment fields, embeds,
    # ads), whose content is not in the tab's own ``DOM.getDocument``. Each
    # such frame is reached through a CDP session of its own and costs its
    # own DOM + DOMSnapshot + Accessibility round trips per observe (plus
    # one ``Target.setAutoAttach`` when first seen), all frames together
    # bounded by ``dom.capture.FRAME_CAPTURE_TIMEOUT_S``. Off, such an iframe
    # stays opaque: the ``<iframe>`` element is its only target. Same-site
    # iframes and shadow roots are captured either way — they arrive with
    # the tab's own ``DOM.getDocument(pierce=True)`` at no extra cost.
    cross_site_iframe_capture: bool = False

    # SecurityWatchdog configuration.
    security_allowed_domains: list[str] | None = None
    security_blocked_domains: list[str] | None = None


BUILTIN_PROFILES: dict[str, BrowserProfileConfig] = {
    "standard": BrowserProfileConfig(
        serializer="text",
        watchdogs_exclude=[],
        # Accuracy-first profile: pay the extra round trip.
        js_click_listener_detection=True,
        cross_site_iframe_capture=True,
    ),
    # Pair with ``dom_cleaner: {cleaner_type: skyvern}``: the html
    # serializer prints what that cleaner produces, and the two capture
    # switches are off because that cleaner reads neither
    # ``js_click_listener_ids`` nor frame captures.
    "skyvern": BrowserProfileConfig(
        serializer="html",
        watchdogs_exclude=["security"],
        js_click_listener_detection=False,
        cross_site_iframe_capture=False,
    ),
    "vision_only": BrowserProfileConfig(
        serializer=None,
        # Hard opt-out: this profile cannot serve ``browser.dom`` at all,
        # so an agent that *requires* that fragment fails negotiation
        # rather than silently receiving an empty tree every step.
        watchdogs_exclude=["dom", "security"],
    ),
    "lightweight": BrowserProfileConfig(
        serializer="text",
        watchdogs_exclude=["crash", "popups", "security"],
        # Cuts CDP work: cross-site frames stay opaque.
        cross_site_iframe_capture=False,
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
        Override individual fields (``serializer``).
    watchdogs_add:
        Watchdog types to re-enable (drop them from ``watchdogs_exclude``).
    watchdogs_remove:
        Watchdog types to disable (add them to ``watchdogs_exclude``).
    """
    if name not in BUILTIN_PROFILES:
        available = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"Unknown browser profile {name!r}. Available: {available}")

    profile = BUILTIN_PROFILES[name].model_copy(deep=True)

    if override:
        for key, value in override.items():
            if key == "serializer":
                setattr(profile, key, value)

    if watchdogs_add:
        profile.watchdogs_exclude = [
            w for w in profile.watchdogs_exclude if w not in watchdogs_add
        ]

    if watchdogs_remove:
        for wd in watchdogs_remove:
            if wd not in profile.watchdogs_exclude:
                profile.watchdogs_exclude.append(wd)

    return profile
