"""Veto URL shortcuts when a run requires interaction through page controls (§4.14).

The action guard already qualified every name before the environment's AOP
boundary. This hook only reads those names and the browser actions' declared
parameters. It does not inspect the page, change prompts or execute actions.
"""

import ipaddress
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.loops.errors import RunAbort
from spectragent.schemas.action import Action


def _browser_shortcut(keys: str) -> bool:
    """Recognize browser-chrome navigation in SendKeysAction's ``keys`` string.

    The executor splits this parameter on ``+`` (ActionWatchdog._on_send_keys);
    UI-TARS converts its own whitespace/list spelling before emitting it.
    That split currently has no shared read-only helper. Keep this recognizer
    at the same parameter boundary, and additionally veto common spellings
    of a navigation shortcut even when today's executor would ignore an alias.
    No change to what send_keys dispatches is implied.
    """
    parts = [part.strip().casefold().removeprefix("keys.") for part in keys.split("+")]
    key = parts[-1]
    if key == "f6":
        return True
    modifiers = set(parts[:-1])
    control_or_meta = modifiers.intersection(
        {
            "ctrl",
            "control",
            "ctrlleft",
            "ctrlright",
            "controlleft",
            "controlright",
            "control_l",
            "control_r",
            "cmd",
            "command",
            "meta",
            "metaleft",
            "metaright",
            "meta_l",
            "meta_r",
            "super",
            "win",
            "windows",
            "⌘",
            "^",
            "controlormeta",
            "ctrlorcmd",
        }
    )
    if control_or_meta and key in {"l", "t", "n", "k", "e"}:
        return True
    return key == "d" and bool(
        modifiers.intersection({"alt", "altleft", "altright", "alt_l", "alt_r", "option", "⌥"})
    )


def _url_input(text: str) -> bool:
    """Whether the whole input looks like a URL or a host with an optional path.

    Embedded links in prose, email addresses, ordinary sentences and decimals
    are not rejected. This is a lexical protocol guard, not an intent classifier
    or a DNS lookup. A bare domain-shaped filename can be ambiguous; the guard
    deliberately treats a whole domain-shaped token as an address.
    """
    value = text.strip()
    if not value or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme:
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
                return bool(parsed.netloc or parsed.scheme == "file")
            if parsed.scheme.casefold() in {
                "about",
                "data",
                "javascript",
                "blob",
                "mailto",
                "tel",
                "view-source",
            }:
                return bool(parsed.path)
        bare = urlsplit(value if value.startswith("//") else "//" + value)
        if bare.username is not None or bare.password is not None:
            return False  # a bare email value is ordinary page input
        host = bare.hostname
        if not host:
            return False
        _ = bare.port  # reject malformed ports instead of guessing a host
        if host.casefold() == "localhost":
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if "." not in host:
            return False
        labels = host.rstrip(".").encode("idna").decode("ascii").split(".")
        if len(labels) < 2 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            return False
        return bool(re.fullmatch(r"[A-Za-z]{2,63}|xn--[A-Za-z0-9-]+", labels[-1]))
    except (ValueError, UnicodeError):
        return False


def navigation_violation(action: Action) -> str | None:
    """Explain a prohibited qualified browser action, for execution and trace audits."""
    if action.name in {"browser.navigate", "browser.create_tab"}:
        return "direct URL navigation is prohibited; use page controls"
    if action.name == "browser.send_keys":
        keys = action.params.get("keys")
        if isinstance(keys, str) and _browser_shortcut(keys):
            return "browser address-bar, new-tab or new-window shortcuts are prohibited"
    if action.name in {"browser.type", "browser.type_coordinate"}:
        text = action.params.get("text")
        if isinstance(text, str) and _url_input(text):
            return "typing a complete URL or host path is prohibited; use page controls"
    return None


class UiNavigationGuard(HookBase):
    """Opt-in veto of direct URL navigation and browser-chrome shortcuts."""

    __registry_key__ = "ui_navigation_guard"
    __intercepts__: ClassVar[list[Intercept]] = [Intercept("act", role="env", advice="before")]
    priority = HookBase.PRIORITY_DEFAULT

    async def before_act(
        self, *, args: tuple[Any, ...], kwargs: dict[str, Any], **ctx: Any
    ) -> None:
        action = args[0] if args else kwargs["action"]
        reason = navigation_violation(action)
        if reason is not None:
            raise RunAbort(f"{action.name}: {reason}", source=self.hook_id)
