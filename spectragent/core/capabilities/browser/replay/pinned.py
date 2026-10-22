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
- **The client hints the user agent override carries** (``userAgentMetadata``,
  :meth:`PinnedReplayability._client_hints`): the browser's own, read right
  after launch (``client_hints.py``), when the identifier is Chrome's own
  form — ``user_agent: None`` (the browser's own string) or a normal Chrome
  identifier — so ``navigator.userAgentData`` and ``Sec-CH-UA`` say what that
  Chrome says by itself under this launch, on every request, the first
  request of a tab that cannot reach its opener included (Chrome sends its
  own hints there before the tab is attached; measured, Chrome 155). A
  Chrome identifier naming another version, platform or form than the
  browser's own hints is a plan problem: setup refuses the run. Any other
  identifier carries none: brands empty, no ``Sec-CH-UA`` — except on that
  first request, which carries Chrome's own.
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

from spectragent.core.capabilities.browser.client_hints import (
    ClientHints,
    chrome_identifier,
    identifier_problems,
)
from spectragent.core.capabilities.browser.color_scheme import ColorScheme, color_scheme_from_record
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
                    "setup ran. 'from_archive': the T0 recorded "
                    "in the archive being replayed — valid only with the "
                    "'archive' network source and an available source clock. "
                    "Independent of the network source's shift_dates option."
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
                    "agent and pins only Accept-Language. Either way, when "
                    "the identifier is in Chrome's own form, pages also get "
                    "the browser's own client hints (navigator.userAgentData, "
                    "Sec-CH-UA); a Chrome identifier naming another version, "
                    "platform or form than the browser is refused at setup."
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

    @property
    def needs_client_hints(self) -> bool:
        """``True``: every frame and worker gets a user agent override (module docstring)."""
        return True

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

    @property
    def needs_color_scheme(self) -> bool:
        return True

    def plan(
        self,
        *,
        t0_ms: int,
        browser_user_agent: str,
        run_started_ms: int | None = None,
        client_hints: ClientHints | None = None,
        color_scheme: ColorScheme | None = None,
    ) -> ReplayabilityPlan:
        if color_scheme is not None and color_scheme_from_record(color_scheme) is None:
            raise ValueError(f"pinned: invalid measured color scheme {color_scheme!r}")
        shim = render_shim(
            t0_ms=t0_ms,
            run_started_ms=t0_ms if run_started_ms is None else run_started_ms,
            seed=self.random_seed,
        )
        hints, problems = self._client_hints(client_hints)
        user_agent: dict[str, Any] = {
            "userAgent": self.user_agent or browser_user_agent,
            "acceptLanguage": self.accept_language(),
        }
        if hints is not None:
            user_agent["userAgentMetadata"] = hints.as_metadata()
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
        media_features = []
        if color_scheme is not None:
            media_features.append({"name": "prefers-color-scheme", "value": color_scheme})
        if self.reduced_motion:
            media_features.append({"name": "prefers-reduced-motion", "value": "reduce"})
        if media_features:
            frame.append(
                CDPCommand(
                    "Emulation.setEmulatedMedia",
                    {"features": media_features},
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
        # Its override carries the same client hints as the frames'.
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
            client_hints=hints,
            color_scheme=color_scheme,
            problems=tuple(problems),
            frame_commands=tuple(frame),
            worker_commands=worker,
        )

    def _client_hints(self, own: ClientHints | None) -> tuple[ClientHints | None, list[str]]:
        """Which client hints the override carries, and why it cannot carry the browser's own.

        The browser's own (*own*, read after launch) for an identifier in
        Chrome's form: the browser's own string (``user_agent: None``), or a
        Chrome identifier whose version, platform and form they match — one
        they contradict is a problem and carries none. Any other identifier
        carries none. Not read (*own* ``None``): none, and no problem.
        """
        if own is None:
            return None, []
        if self.user_agent is None:
            return own, []
        if chrome_identifier(self.user_agent) is None:
            return None, []
        problems = identifier_problems(self.user_agent, own)
        return (None if problems else own), problems

    def accept_language(self) -> str:
        """``en-US`` → ``en-US,en``; ``en`` → ``en``.

        A plain list: Chrome adds the q-values itself (``en-US,en;q=0.9`` on
        the wire) and exposes the list as ``navigator.languages``; a list
        that already carries them comes out doubled (``fr;q=0.9;q=0.9``).
        """
        primary = self.locale.split("-", 1)[0]
        return self.locale if primary == self.locale else f"{self.locale},{primary}"
