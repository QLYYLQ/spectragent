"""``pinned`` — a recording that can be replayed with the same inputs (§4.14).

Rule zero: recording and replay use the identical shim, launch switches,
seed, user agent and viewport. This tier is the **only** source of each:

- **Launch switches** (:meth:`PinnedReplayability.launch_args`), built from
  its own knobs and handed to a provider declaring ``LAUNCH_ARGS``:
  ``--js-flags=--random-seed=<seed>``, ``--lang=<locale>``,
  ``--force-device-scale-factor=<dpr>``, ``--disable-back-forward-cache``,
  ``--disable-features=HttpsUpgrades`` (Chrome 153 first tries https for an
  http navigation to a registrable host; off at record and replay), and the
  three Chrome defaults ``ignore_default_args`` can drop
  (``--disable-background-networking``, ``--disable-component-update``,
  ``--disable-field-trial-config``). With ``user_agent`` set, also
  ``--user-agent=<ua>``, so workers and the network stack see the pinned
  string too and a provider's own ``user_agent`` is caught as a conflict.
  No ``--disable-site-isolation-trials``: it is not needed for coverage and
  changes how out-of-process iframes are observed (measured, arch §4.14).
- **The launch environment** (:meth:`PinnedReplayability.launch_env`),
  handed to the provider with the switches: ``TZ=<timezone>``, the browser
  process's default time zone. Chrome keeps one owner of the time zone
  override per page process; a frame the owner left behind falls back to the
  process default within 0.2–0.3 s, which is the pinned zone only because of
  ``TZ`` (measured, Chrome 155). The locale has no such mechanism on macOS
  (``--lang`` and ``LANG`` are ignored there), so a frame without an owner
  reads the host's: the environment fingerprint records it
  (``browser_defaults``) and a replay on a host whose default differs is
  refused.
- **Overrides on the recorder connection** (the plan's commands): the shim
  (frames: ``Page.enable`` first, then ``Page.addScriptToEvaluateOnNewDocument``;
  workers: ``Runtime.evaluate`` before resume), time zone, locale, user agent
  + ``Accept-Language`` (frames: ``Emulation.setUserAgentOverride``; workers:
  ``Network.setUserAgentOverride``), reduced motion, cache disabled and
  service workers bypassed (both behind ``Network.enable``). There is no
  per-run ``Network.clearBrowserCache``: the profile is fresh for every run
  (the cooperation check refuses any other), so the cache starts empty, and
  clearing an empty cache measured 3.9–9.3 s on Chrome 153 at every setup.
  The user agent is overridden without
  ``userAgentMetadata``, so ``navigator.userAgentData.brands`` is empty and no
  ``Sec-CH-UA`` header is sent — identically at record and replay — except on
  the first request of a tab that cannot reach its opener, which carries
  Chrome's default brands even with ``--user-agent`` at launch (the header is
  computed before the tab is attached; measured, Chrome 155).
- **The viewport DPR** (the ``device_scale_factor`` knob itself),
  which the control connection applies with ``setDeviceMetricsOverride``.

``base_time: from_archive`` (T0 taken from the archive being replayed) is
valid only with the ``archive`` network source; the capability's
cooperation check refuses it with a ``live`` one at ``prepare()``.
"""

import re
import zoneinfo
from typing import Annotated, Any, Literal

from pydantic import Field as PydanticField

from spectragent.core.capabilities.browser.recorder import CDPCommand
from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityBase,
    ReplayabilityPlan,
)
from spectragent.core.capabilities.browser.replay.shims import SHIM_SHA256, render_shim

#: A BCP 47 language tag, loosely: ``en``, ``en-US``, ``zh-Hans-CN``.
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class PinnedReplayability(ReplayabilityBase):
    """Seeded randomness, a stepped clock from T0, fixed tz / locale / UA / DPR."""

    __registry_key__ = "pinned"

    def __init__(
        self,
        *,
        base_time: Annotated[
            Literal["run_start", "from_archive"],
            PydanticField(
                description=(
                    "Where the shim's clock starts. 'run_start': the moment "
                    "setup ran (a recording). 'from_archive': the T0 recorded "
                    "in the archive being replayed — valid only with the "
                    "'archive' network source; prepare() refuses it with a 'live' one."
                ),
            ),
        ] = "run_start",
        random_seed: Annotated[
            int,
            PydanticField(
                ge=1,
                le=2**31 - 1,
                description=(
                    "Seed for the shim's Math.random / crypto streams and for "
                    "V8 (--js-flags=--random-seed). Zero is refused: V8 reads "
                    "it as 'seed from the system'."
                ),
            ),
        ] = 1,
        timezone: Annotated[
            str,
            PydanticField(
                description=(
                    "IANA time zone for Emulation.setTimezoneOverride on every "
                    "frame target, e.g. 'UTC', 'Asia/Shanghai'."
                ),
            ),
        ] = "UTC",
        locale: Annotated[
            str,
            PydanticField(
                description=(
                    "Language tag for --lang, Emulation.setLocaleOverride and "
                    "Accept-Language, e.g. 'en-US'."
                ),
            ),
        ] = "en-US",
        device_scale_factor: Annotated[
            float,
            PydanticField(
                gt=0,
                le=10,
                description=(
                    "Device pixel ratio: --force-device-scale-factor and the "
                    "control connection's viewport override."
                ),
            ),
        ] = 1.0,
        user_agent: Annotated[
            str | None,
            PydanticField(
                description=(
                    "User agent to pin. None keeps the browser's own user "
                    "agent and pins only Accept-Language."
                ),
            ),
        ] = None,
        reduced_motion: Annotated[
            bool,
            PydanticField(
                description=(
                    "Emulate prefers-reduced-motion: reduce, so pages that "
                    "honour it skip their animations."
                ),
            ),
        ] = True,
    ) -> None:
        try:
            zoneinfo.ZoneInfo(timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"pinned: unknown time zone {timezone!r}") from exc
        if not _LOCALE.match(locale):
            raise ValueError(f"pinned: {locale!r} is not a language tag such as 'en-US'")
        self.base_time: Literal["run_start", "from_archive"] = base_time
        self.random_seed = random_seed
        self.timezone = timezone
        self.locale = locale
        # The knob is the Protocol member: the control connection reads it.
        self.device_scale_factor = float(device_scale_factor)
        self.user_agent = user_agent
        self.reduced_motion = reduced_motion

    # ── ReplayabilityProtocol ─────────────────────────────────────────

    @property
    def needs_archive_clock(self) -> bool:
        return self.base_time == "from_archive"

    def launch_args(self) -> tuple[str, ...]:
        args = [
            f"--js-flags=--random-seed={self.random_seed}",
            f"--lang={self.locale}",
            f"--force-device-scale-factor={self.device_scale_factor:g}",
            "--disable-back-forward-cache",
            "--disable-features=HttpsUpgrades",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-field-trial-config",
        ]
        if self.user_agent:
            args.append(f"--user-agent={self.user_agent}")
        return tuple(args)

    def launch_env(self) -> dict[str, str]:
        """``TZ``: the zone a frame without an override owner falls back to (module docstring)."""
        return {"TZ": self.timezone}

    def plan(self, *, t0_ms: int, browser_user_agent: str) -> ReplayabilityPlan:
        shim = render_shim(t0_ms=t0_ms, seed=self.random_seed)
        user_agent: dict[str, Any] = {
            "userAgent": self.user_agent or browser_user_agent,
            "acceptLanguage": self.accept_language(),
        }
        frame = [
            CDPCommand("Page.enable"),
            # ``runImmediately``: the document already there when the target
            # is prepared (the initial tab's about:blank) is pinned too.
            CDPCommand(
                "Page.addScriptToEvaluateOnNewDocument", {"source": shim, "runImmediately": True}
            ),
            CDPCommand("Emulation.setTimezoneOverride", {"timezoneId": self.timezone}),
            CDPCommand("Emulation.setLocaleOverride", {"locale": self.locale}),
            CDPCommand("Emulation.setUserAgentOverride", user_agent),
        ]
        if self.reduced_motion:
            frame.append(
                CDPCommand(
                    "Emulation.setEmulatedMedia",
                    {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
                )
            )
        # ``setCacheDisabled`` / ``setBypassServiceWorker`` take effect only on
        # a session whose Network domain is enabled (measured: without it the
        # cache and a service worker still answer). No Network handler is
        # registered for it in shim-only mode; the events are dropped.
        frame.extend(
            (
                CDPCommand("Network.enable"),
                CDPCommand("Network.setCacheDisabled", {"cacheDisabled": True}),
                CDPCommand("Network.setBypassServiceWorker", {"bypass": True}),
            )
        )
        # A worker inherits the page's time zone and locale (renderer-wide
        # overrides) and its user agent, but not its Accept-Language: without
        # its own override ``navigator.language`` in a worker is the host's.
        worker = (
            CDPCommand("Runtime.evaluate", {"expression": shim}),
            CDPCommand("Network.enable"),
            CDPCommand("Network.setUserAgentOverride", user_agent),
            CDPCommand("Network.setCacheDisabled", {"cacheDisabled": True}),
        )
        return ReplayabilityPlan(
            tier=self.replayability_id,
            params={
                "base_time": self.base_time,
                "random_seed": self.random_seed,
                "timezone": self.timezone,
                "locale": self.locale,
                "device_scale_factor": self.device_scale_factor,
                "user_agent": self.user_agent,
                "reduced_motion": self.reduced_motion,
            },
            t0_ms=t0_ms,
            shim_sha256=SHIM_SHA256,
            replayability_args=self.launch_args(),
            replayability_env=self.launch_env(),
            frame_commands=tuple(frame),
            worker_commands=worker,
        )

    def accept_language(self) -> str:
        """``en-US`` → ``en-US,en``; ``en`` → ``en``.

        A plain list: Chrome adds the q-values itself (``en-US,en;q=0.9`` on
        the wire) and exposes the list as ``navigator.languages``; a list
        that already carries them comes out doubled (``fr;q=0.9;q=0.9``).
        """
        primary = self.locale.split("-", 1)[0]
        return self.locale if primary == self.locale else f"{self.locale},{primary}"
