"""What a frame no override reaches reads: the browser's default time zone and locale (§4.14).

``replayability: pinned`` overrides the time zone and the locale on every
frame target, but Chrome keeps one owner of those overrides per page process,
the frame that set them first. Another frame in the process falls back to the
browser's defaults within 0.2–0.3 s once the owner leaves (an iframe removed,
an opener that navigated to another site), and a cross-site popup that keeps
its opener reads the default locale from its first document (measured,
Chrome 155, ``docs/architecture_design.md`` §4.14 "已知限制"). The defaults are
the browser process's:

- **time zone**: ``TZ`` in the browser's environment when it names a zone
  (a leading ``:`` allowed), else the zone ``/etc/localtime`` links to — the
  rule ICU follows. ``pinned`` sets ``TZ`` at launch
  (``ReplayabilityProtocol.launch_env``), so its fallback is the pinned zone;
- **locale**: Chrome's application locale. On macOS that is the first of the
  Chrome bundle's localizations Cocoa picks for the user's preferred
  languages, as Chrome's own preference domain sees them, with ``_`` written
  ``-`` and ``en`` read as ``en-US`` (Chrome's ``OverrideLocaleWithCocoaLocale``).
  Neither ``--lang`` nor ``LANG`` / ``LC_ALL`` / ``LANGUAGE`` changes it there,
  so it cannot be pinned (measured). On other platforms it is not read before
  launch.

Two ways to know them, one per moment:

- :func:`expected_browser_defaults` reads the facts above from the OS before
  anything is launched, so a replay's consistency check refuses a host whose
  defaults differ at ``prepare()``, before the trace is opened. A value it
  cannot read is ``None``;
- :func:`measure_browser_defaults` asks Chrome right after launch, in a frame
  no override has reached yet. The environment fingerprint records what it
  measured, and a replay compares the measurement with the recording's after
  launch too, for every value the OS facts could not say.
"""

import ctypes
import ctypes.util
import json
import logging
import os
import sys
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spectragent.core.capabilities.browser.commands import BrowserCommands

logger = logging.getLogger(__name__)

#: Where ICU reads the host's zone when ``TZ`` is not set.
LOCALTIME = "/etc/localtime"
#: The isolated world the measurement runs in (``Page.createIsolatedWorld``).
DEFAULTS_WORLD = "af-browser-defaults"
#: What the measurement evaluates: the frame's own Intl defaults.
DEFAULTS_PROBE = (
    "JSON.stringify((() => { const o = Intl.DateTimeFormat().resolvedOptions();"
    " return {timezone: o.timeZone || null, locale: o.locale || null}; })())"
)


@dataclass(frozen=True)
class BrowserDefaults:
    """The time zone and locale a frame without overrides reads; ``None`` = not known."""

    timezone: str | None = None
    locale: str | None = None

    #: The values, in the order they are compared and reported.
    NAMES = ("timezone", "locale")

    def as_record(self) -> dict[str, str | None]:
        """As the environment fingerprint records it."""
        return {"timezone": self.timezone, "locale": self.locale}

    @classmethod
    def from_record(cls, record: Any) -> "BrowserDefaults | None":
        """The defaults a fingerprint recorded; ``None`` when it recorded none."""
        if not isinstance(record, Mapping):
            return None

        def value(name: str) -> str | None:
            raw = record.get(name)
            return raw if isinstance(raw, str) and raw else None

        return cls(timezone=value("timezone"), locale=value("locale"))

    def value(self, name: str) -> str | None:
        return self.timezone if name == "timezone" else self.locale


# ── before launch: the OS facts ───────────────────────────────────────


def expected_browser_defaults(
    *,
    executable: str | None,
    launch_env: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    localtime: str = LOCALTIME,
) -> BrowserDefaults:
    """The defaults the browser launched from *executable* will fall back to, read before launch.

    *launch_env* is what the launch adds to the environment the browser
    inherits (*environ*, by default this process's). Never raises: a fact
    that cannot be read is ``None``.
    """
    inherited = os.environ if environ is None else environ
    if "TZ" in launch_env:
        zone = _named_zone(launch_env["TZ"])
    elif "TZ" in inherited:
        zone = _named_zone(inherited["TZ"])
    else:
        zone = _linked_zone(localtime)
    locale = None
    if (platform or sys.platform) == "darwin" and executable:
        locale = _cocoa_locale(executable)
    return BrowserDefaults(timezone=zone, locale=locale)


def _named_zone(value: str) -> str | None:
    """``TZ``'s zone name (``:Asia/Tokyo`` → ``Asia/Tokyo``); ``None`` for a POSIX rule string.

    Chrome reads no IANA name from a rule such as ``JST-9``: Intl reports no
    time zone at all (measured, Chrome 155).
    """
    name = value[1:] if value.startswith(":") else value
    return name if _is_zone(name) else None


def _linked_zone(path: str) -> str | None:
    """The zone ``/etc/localtime`` links to, read as ICU reads it (one link, the part after ``zoneinfo/``)."""
    try:
        target = os.readlink(path)
    except OSError:
        return None
    _, sep, name = target.partition("zoneinfo/")
    return name if sep and _is_zone(name) else None


def _is_zone(name: str) -> bool:
    if not name:
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _app_bundle(executable: str) -> Path | None:
    """The ``.app`` directory *executable* lives in, if any."""
    for parent in Path(executable).parents:
        if parent.suffix == ".app":
            return parent
    return None


def _cocoa_locale(executable: str) -> str | None:
    """Chrome's application locale on macOS, as Cocoa chooses it, read before launch.

    The calls Chrome's ``[OuterBundle() preferredLocalizations]`` makes, made
    by CoreFoundation on the same inputs: the bundle's localizations and
    ``AppleLanguages`` as the bundle's own preference domain sees it (its own
    value, else the user's global one).
    """
    bundle_path = _app_bundle(executable)
    if bundle_path is None:
        return None
    try:
        chosen = _CoreFoundation().preferred_localization(str(bundle_path))
    except Exception as exc:  # no CoreFoundation, an unreadable bundle …
        logger.debug("browser defaults: no Cocoa locale for %s: %s", bundle_path, exc)
        return None
    if not chosen:
        return None
    locale = chosen.replace("_", "-")
    return "en-US" if locale == "en" else locale


class _CoreFoundation:
    """The few CoreFoundation calls :func:`_cocoa_locale` needs, through ctypes."""

    _UTF8 = 0x08000100
    _POSIX_PATH_STYLE = 0

    def __init__(self) -> None:
        name = ctypes.util.find_library("CoreFoundation")
        if name is None:
            raise OSError("CoreFoundation is not available")
        cf = ctypes.cdll.LoadLibrary(name)
        void_p, c_long = ctypes.c_void_p, ctypes.c_long
        signatures: dict[str, tuple[Any, list[Any]]] = {
            "CFStringCreateWithCString": (void_p, [void_p, ctypes.c_char_p, ctypes.c_uint32]),
            "CFStringGetCString": (
                ctypes.c_bool,
                [void_p, ctypes.c_char_p, c_long, ctypes.c_uint32],
            ),
            "CFURLCreateWithFileSystemPath": (
                void_p,
                [void_p, void_p, ctypes.c_int, ctypes.c_bool],
            ),
            "CFBundleCreate": (void_p, [void_p, void_p]),
            "CFBundleGetIdentifier": (void_p, [void_p]),
            "CFBundleCopyBundleLocalizations": (void_p, [void_p]),
            "CFPreferencesCopyAppValue": (void_p, [void_p, void_p]),
            "CFBundleCopyLocalizationsForPreferences": (void_p, [void_p, void_p]),
            "CFArrayGetCount": (c_long, [void_p]),
            "CFArrayGetValueAtIndex": (void_p, [void_p, c_long]),
            "CFRelease": (None, [void_p]),
        }
        for function, (restype, argtypes) in signatures.items():
            getattr(cf, function).restype = restype
            getattr(cf, function).argtypes = argtypes
        self._cf = cf

    def preferred_localization(self, bundle_path: str) -> str | None:
        cf = self._cf
        owned: list[int] = []

        def keep(ref: int | None) -> int | None:
            if ref:
                owned.append(ref)
            return ref

        try:
            path = keep(cf.CFStringCreateWithCString(None, bundle_path.encode(), self._UTF8))
            url = keep(cf.CFURLCreateWithFileSystemPath(None, path, self._POSIX_PATH_STYLE, True))
            bundle = keep(cf.CFBundleCreate(None, url))
            if not bundle:
                return None
            identifier = cf.CFBundleGetIdentifier(bundle)  # not owned
            localizations = keep(cf.CFBundleCopyBundleLocalizations(bundle))
            key = keep(cf.CFStringCreateWithCString(None, b"AppleLanguages", self._UTF8))
            languages = keep(cf.CFPreferencesCopyAppValue(key, identifier)) if identifier else None
            if not localizations or not languages:
                return None
            chosen = keep(cf.CFBundleCopyLocalizationsForPreferences(localizations, languages))
            if not chosen or cf.CFArrayGetCount(chosen) < 1:
                return None
            return self._text(cf.CFArrayGetValueAtIndex(chosen, 0))
        finally:
            for ref in owned:
                cf.CFRelease(ref)

    def _text(self, ref: int | None) -> str | None:
        if not ref:
            return None
        buffer = ctypes.create_string_buffer(256)
        if not self._cf.CFStringGetCString(ref, buffer, len(buffer), self._UTF8):
            return None
        return buffer.value.decode()


# ── right after launch: Chrome's own answer ──────────────────────────


async def measure_browser_defaults(client: BrowserCommands, *, timeout: float) -> BrowserDefaults:
    """What a frame no override reached reads, asked of Chrome right after launch.

    In the first page target, before any override was sent to it: attached
    to on *client* (flatten), read in an isolated world of its main frame (no
    page script sees the read or changes it), detached again. A value Chrome
    does not report is ``None``.

    Raises:
        RuntimeError: No page target to measure in.
        TimeoutError, RuntimeError: A command failed.
    """
    reply = await client.send("Target.getTargets", {}, None, timeout=timeout)
    pages = [
        info
        for info in (reply or {}).get("targetInfos", [])
        if info.get("type") == "page" and info.get("targetId")
    ]
    if not pages:
        raise RuntimeError("no page target to measure the browser's defaults in")
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
            {"frameId": frame["id"], "worldName": DEFAULTS_WORLD},
            session_id,
            timeout=timeout,
        )
        evaluated = await client.send(
            "Runtime.evaluate",
            {
                "expression": DEFAULTS_PROBE,
                "returnByValue": True,
                "contextId": (world or {})["executionContextId"],
            },
            session_id,
            timeout=timeout,
        )
    finally:
        try:
            await client.send(
                "Target.detachFromTarget", {"sessionId": session_id}, None, timeout=timeout
            )
        except Exception as exc:
            logger.debug("browser defaults: detaching %s failed: %s", session_id, exc)
    value = ((evaluated or {}).get("result") or {}).get("value")
    measured = json.loads(value) if isinstance(value, str) else {}
    return BrowserDefaults.from_record(measured) or BrowserDefaults()
