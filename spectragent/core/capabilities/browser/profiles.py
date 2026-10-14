"""Browser profile presets — what a run captures, and at what cost.

A profile says which watchdogs to **leave out**, not which to install.
The platform resolver joins every built-in profile, including vision-only;
it reads the setup fact and does not require DOM capture.
Enumerating them was one of the nine places a new observation fragment
had to be registered by hand: every ``BUILTIN_PROFILES`` entry listed
watchdog names, so a newly registered watchdog was silently absent from
all of them. Inverting it means a new watchdog joins every profile and
costs nothing unless its fragment is negotiated active — cost control
belongs to negotiation (``BrowserStateRequestEvent.fragments``), not to
a hand-maintained name list.

**No strategy name may live on this model.** It happened three times —
``mouse_trajectory``, then ``dom_cleaner``, then ``serializer`` — and the
failure is identical each time: a name on the profile means the consumer
does ``SomeRegistrar.get(name)()``, the strategy arrives
default-constructed, and every knob its generated config declares
(``TextSerializer.max_text_length``, ``HTMLSerializer.max_href_length``,
``BrowserUseCleaner.descendants_occlude``, ...) validates in YAML and then
goes nowhere. All three are wired receptors on
:class:`~spectragent.core.capabilities.browser.capability.BrowserCapability`
now, so the configured *instance* is what reaches ``DOMWatchdog``.
``tests/test_core/test_capabilities/test_receptor_hygiene.py`` is the
structural guard that stops instance number four.

What belongs here instead is what a profile actually is: which watchdogs
are installed, and which CDP round trips they are allowed to spend.

Usage::

    profile = resolve_profile("standard")
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrowserProfileConfig(BaseModel):
    """Configuration for a browser session's component stack."""

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
    # such frame is reached through a CDP session of its own. Its capture
    # cost is strategy-selected: CDP Document/Snapshot/AX for BrowserUse,
    # JS extraction for Skyvern, with one ``Target.setAutoAttach`` on first
    # discovery and a shared frame deadline. Off, the iframe stays opaque.
    # Same-site frames and shadow roots are handled by each strategy too.
    cross_site_iframe_capture: bool = False

    # SecurityWatchdog configuration.
    security_allowed_domains: list[str] | None = None
    security_blocked_domains: list[str] | None = None


BUILTIN_PROFILES: dict[str, BrowserProfileConfig] = {
    "standard": BrowserProfileConfig(
        watchdogs_exclude=[],
        # Accuracy-first profile: pay the extra round trip.
        js_click_listener_detection=True,
        cross_site_iframe_capture=True,
    ),
    # Pair with ``dom_cleaner: {cleaner_type: skyvern}`` and
    # ``dom_serializer: {serializer_type: html}``. The cleaner chooses its
    # own JS capture and ignores BrowserUse's listener-detection switch,
    # but still needs frame-target discovery for cross-site iframes.
    "skyvern": BrowserProfileConfig(
        watchdogs_exclude=["security"],
        js_click_listener_detection=False,
        cross_site_iframe_capture=True,
    ),
    "vision_only": BrowserProfileConfig(
        # Hard opt-out, and the only one that ever worked: excluding the
        # ``dom`` watchdog removes ``browser.dom`` from
        # ``BrowserCapability.declared_fragments``, so an agent that
        # *requires* that fragment fails negotiation rather than silently
        # receiving an empty tree every step. The ``serializer: None`` that
        # used to sit beside this line only suppressed the serialized
        # string — it never touched what the capability declared.
        watchdogs_exclude=["dom", "security"],
    ),
    "lightweight": BrowserProfileConfig(
        watchdogs_exclude=["crash", "popups", "security"],
        # Cuts CDP work: cross-site frames stay opaque.
        cross_site_iframe_capture=False,
    ),
}


def resolve_profile(name: str) -> BrowserProfileConfig:
    """Resolve a named profile to its own mutable copy.

    Parameters
    ----------
    name:
        Built-in profile name (standard, skyvern, vision_only, lightweight).
    """
    if name not in BUILTIN_PROFILES:
        available = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"Unknown browser profile {name!r}. Available: {available}")

    return BUILTIN_PROFILES[name].model_copy(deep=True)
