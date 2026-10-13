"""Local Chrome browser provider.

Launches a local ``google-chrome`` / ``chromium`` subprocess with
``--remote-debugging-port`` and returns a CDP WebSocket URL.

All Chrome CLI flags are exposed as configurable ``__init__`` parameters
(Tier 2 conscribe config via ``Annotated[..., Field(...)]``).  This gives
full control over Chrome's behavior from YAML config files.

Env var: ``CHROME_EXECUTABLE_PATH``. The profile directory is ``user_data_dir``
in YAML only: an environment variable would decide whether a run starts from a
fresh profile without the configuration or ``config.json`` showing it
(``docs/architecture_design.md`` §4.14).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

import httpx
from pydantic import Field

from spectragent.core.capabilities.browser.backends._registrar import BrowserProviderBase
from spectragent.core.capabilities.browser.backends.browser_defaults import (
    BrowserDefaults,
    expected_browser_defaults,
)
from spectragent.core.capabilities.browser.backends.capabilities import (
    CDP,
    CUSTOM_PROXY,
    EXTENSIONS,
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
    NETWORK_ISOLATION,
    PROFILE,
    SCREEN_SIZE,
)
from spectragent.core.capabilities.browser.backends.chrome_switches import (
    NETWORK_ISOLATION_ARGS,
    NETWORK_ROUTING_SWITCHES,
    contradicting_switch,
    merge_chrome_switches,
    split_switch,
)
from spectragent.schemas.provider_session import ProviderSession

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Chrome default arguments — based on browser-use + Puppeteer best practices
# ═══════════════════════════════════════════════════════════════════════════

# Minimal stable defaults that disable non-essential Chrome features for
# automation reliability.  Users can override / extend via ``extra_args``.
CHROME_DEFAULT_ARGS: tuple[str, ...] = (
    # Automation fundamentals
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions-except=",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-search-engine-choice-screen",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--export-tagged-pdf",
    "--generate-pdf-document-outline",
    "--force-color-profile=srgb",
    "--disable-field-trial-config",
    # Network
    "--enable-features=NetworkService,NetworkServiceInProcess",
    # Anti-detection (hide automation)
    "--disable-blink-features=AutomationControlled",
    # Disable features that interfere with automation
    "--disable-features="
    "AcceptCHFrame,"
    "AutoExpandDetailsElement,"
    "AvoidUnnecessaryBeforeUnloadCheckSync,"
    "CertificateTransparencyComponentUpdater,"
    "Translate,"
    "InterestFeedContentSuggestions,"
    "PrivacySandboxSettings4,"
    "AutofillServerCommunication,"
    "MediaRouter,"
    "DialMediaRouteProvider",
)

CHROME_HEADLESS_ARGS: tuple[str, ...] = ("--headless=new",)

CHROME_DOCKER_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-gpu-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-zygote",
)

CHROME_DISABLE_SECURITY_ARGS: tuple[str, ...] = (
    "--disable-web-security",
    "--allow-running-insecure-content",
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--disable-features=IsolateOrigins,site-per-process",
)

CHROME_DETERMINISTIC_RENDERING_ARGS: tuple[str, ...] = (
    "--deterministic-mode",
    "--js-flags=--random-seed=1157259159",
    "--font-render-hinting=none",
)

#: Switches whose values change every launch. ``launch_flags`` (the
#: fingerprintable form of the command line) writes a placeholder instead.
_VOLATILE_SWITCH_PLACEHOLDERS: dict[str, str] = {
    "--user-data-dir": "<user-data-dir>",
    "--remote-debugging-port": "<port>",
}

# Known Chrome executable search paths by platform.
_LINUX_PATHS: tuple[str, ...] = (
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
)

# macOS ships Chrome inside an .app bundle, so nothing on PATH matches the
# Linux names — without these entries every macOS run needs the env override.
_MACOS_PATHS: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_WINDOWS_PATHS: tuple[str, ...] = (
    "chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

_PLATFORM_PATHS: dict[str, tuple[str, ...]] = {
    "darwin": _MACOS_PATHS,
    "win32": _WINDOWS_PATHS,
}

_INSTALL_HINT: dict[str, str] = {
    "darwin": "  brew install --cask google-chrome",
    "win32": "  winget install Google.Chrome",
}


def _search_paths() -> tuple[str, ...]:
    """Executable candidates for the running platform."""
    return _PLATFORM_PATHS.get(sys.platform, _LINUX_PATHS)


def find_chrome() -> str:
    """Find a Chrome / Chromium executable on the system."""
    # Env override. An override that does not resolve is a configuration
    # mistake worth naming — falling through to the search would report the
    # generic "cannot find Chrome" and hide the bad path the user set.
    env_path = os.environ.get("CHROME_EXECUTABLE_PATH")
    if env_path:
        resolved = shutil.which(os.path.expanduser(env_path))
        if resolved:
            return resolved
        raise FileNotFoundError(
            f"CHROME_EXECUTABLE_PATH is set to {env_path!r}, which is not an "
            "executable file. Fix the path or unset it to search the default "
            "install locations."
        )

    for candidate in _search_paths():
        found = shutil.which(os.path.expanduser(candidate))
        if found:
            return found

    hint = _INSTALL_HINT.get(sys.platform, "  apt install google-chrome-stable")
    raise FileNotFoundError(
        f"Cannot find Chrome / Chromium on {sys.platform}. Install with:\n"
        f"{hint}\n"
        "Or set CHROME_EXECUTABLE_PATH."
    )


def _fingerprintable_flags(
    chrome_args: Sequence[str], launch_env: Mapping[str, str] | None = None
) -> list[str]:
    """The launch as the fingerprint digests it: ``NAME=value`` per variable set, then the switches.

    The environment comes first and sorted, as a shell command line writes
    it (``TZ=UTC chrome --flag``); in the switches every per-launch value is
    replaced by a placeholder.
    """
    flags = [f"{name}={value}" for name, value in sorted((launch_env or {}).items())]
    for arg in chrome_args:
        name, value = split_switch(arg)
        placeholder = _VOLATILE_SWITCH_PLACEHOLDERS.get(name)
        flags.append(f"{name}={placeholder}" if placeholder and value is not None else arg)
    return flags


def _find_free_port() -> int:
    """Bind to OS to find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ═══════════════════════════════════════════════════════════════════════════
# Provider implementation
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class LocalChromeProvider(BrowserProviderBase):
    """Local Chrome browser — subprocess launch with full CLI flag control.

    Launches ``google-chrome`` with ``--remote-debugging-port`` and exposes
    the CDP WebSocket URL.  Every Chrome flag is configurable.
    """

    __registry_key__ = "local_chrome"
    __capabilities__: ClassVar[set[str]] = {
        CDP,
        CUSTOM_PROXY,
        EXTENSIONS,
        LAUNCH_ARGS,
        NETWORK_CAPTURE,
        NETWORK_ISOLATION,
        PROFILE,
        SCREEN_SIZE,
    }

    # ── executable ─────────────────────────────────────────────────────
    executable_path: Annotated[
        str | None,
        Field(
            description="Chrome / Chromium binary path (auto-detected if None). Env: CHROME_EXECUTABLE_PATH"
        ),
    ] = None

    # ── display & window ───────────────────────────────────────────────
    headless: Annotated[
        bool,
        Field(description="Run in headless mode (--headless=new)"),
    ] = True

    window_width: Annotated[
        int,
        Field(description="Browser window width in pixels"),
    ] = 1280

    window_height: Annotated[
        int,
        Field(description="Browser window height in pixels"),
    ] = 720

    start_maximized: Annotated[
        bool,
        Field(description="Start browser maximized (ignored in headless mode)"),
    ] = False

    # ── profile / user data ────────────────────────────────────────────
    user_data_dir: Annotated[
        str | None,
        Field(
            description=(
                "Chrome profile directory. Omitted or null (the default): a fresh "
                "temporary profile per launch, deleted at close."
            )
        ),
    ] = None

    profile_directory: Annotated[
        str,
        Field(description="Profile subdirectory name inside user_data_dir"),
    ] = "Default"

    incognito: Annotated[
        bool,
        Field(description="Launch in incognito mode (--incognito)"),
    ] = False

    # ── network / proxy ────────────────────────────────────────────────
    proxy_server: Annotated[
        str | None,
        Field(
            description=(
                "Proxy server URL, e.g. 'socks5://127.0.0.1:1080' or 'http://proxy:8080'. "
                "None (the default): no proxy switch."
            )
        ),
    ] = None

    proxy_bypass_list: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated hosts to bypass proxy (e.g. 'localhost,127.0.0.1'). "
                "None (the default): no bypass list."
            )
        ),
    ] = None

    # ── security ───────────────────────────────────────────────────────
    disable_security: Annotated[
        bool,
        Field(
            description="Disable web security (--disable-web-security, --ignore-certificate-errors). Use for testing only!"
        ),
    ] = False

    no_sandbox: Annotated[
        bool,
        Field(description="Disable Chrome sandbox (--no-sandbox). Required in Docker / rootless."),
    ] = False

    # ── extensions ─────────────────────────────────────────────────────
    load_extensions: Annotated[
        list[str] | None,
        Field(description="Paths to unpacked Chrome extensions to load. None (the default): none."),
    ] = None

    # ── performance ────────────────────────────────────────────────────
    disable_gpu: Annotated[
        bool,
        Field(description="Disable GPU acceleration (--disable-gpu)"),
    ] = False

    disable_dev_shm_usage: Annotated[
        bool,
        Field(
            description="Disable /dev/shm usage (--disable-dev-shm-usage). Recommended in Docker."
        ),
    ] = False

    # ── user agent ─────────────────────────────────────────────────────
    user_agent: Annotated[
        str | None,
        Field(description="Custom user-agent string. None (the default): Chrome's own user agent."),
    ] = None

    # ── misc Chrome flags ──────────────────────────────────────────────
    disable_background_networking: Annotated[
        bool,
        Field(description="Disable background network requests"),
    ] = True

    disable_popup_blocking: Annotated[
        bool,
        Field(description="Disable popup blocking"),
    ] = True

    disable_translate: Annotated[
        bool,
        Field(description="Disable built-in translation"),
    ] = True

    disable_sync: Annotated[
        bool,
        Field(description="Disable Chrome sync"),
    ] = True

    disable_automation_controlled: Annotated[
        bool,
        Field(
            description="Hide automation flag from navigator.webdriver (--disable-blink-features=AutomationControlled)"
        ),
    ] = True

    mute_audio: Annotated[
        bool,
        Field(description="Mute all audio output (--mute-audio)"),
    ] = True

    no_first_run: Annotated[
        bool,
        Field(description="Skip first-run experience (--no-first-run)"),
    ] = True

    disable_default_apps: Annotated[
        bool,
        Field(description="Don't install default Chrome apps (--disable-default-apps)"),
    ] = True

    # ── deterministic rendering ────────────────────────────────────────
    deterministic_rendering: Annotated[
        bool,
        Field(
            description="Force deterministic rendering (fixed seed, font hinting off). Useful for visual regression."
        ),
    ] = False

    force_device_scale_factor: Annotated[
        float | None,
        Field(
            description=(
                "Override device scale factor (e.g. 1.0, 2.0). None (the default): the "
                "display's own."
            )
        ),
    ] = None

    # ── advanced ───────────────────────────────────────────────────────
    remote_debugging_port: Annotated[
        int | None,
        Field(description="Fixed CDP debugging port. Auto-allocated if None."),
    ] = None

    extra_args: Annotated[
        list[str] | None,
        Field(
            description=(
                "Additional raw Chrome CLI flags (e.g. ['--disable-features=Foo', "
                "'--enable-logging']). None (the default): none."
            )
        ),
    ] = None

    ignore_default_args: Annotated[
        bool,
        Field(
            description="If True, skip all CHROME_DEFAULT_ARGS and only use flags from explicit config"
        ),
    ] = False

    startup_url: Annotated[
        str,
        Field(description="URL to open on startup"),
    ] = "about:blank"

    cdp_wait_timeout: Annotated[
        float,
        Field(description="Seconds to wait for Chrome CDP endpoint to become ready"),
    ] = 30.0

    shutdown_timeout: Annotated[
        float,
        Field(description="Seconds to wait for graceful Chrome shutdown before SIGKILL"),
    ] = 5.0

    # ── internal state (excluded from config) ──────────────────────────
    _processes: dict[str, asyncio.subprocess.Process] = field(
        default_factory=dict, init=False, repr=False
    )
    _temp_dirs: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    # -------------------------------------------------------------------
    # Build Chrome CLI arguments
    # -------------------------------------------------------------------

    def _build_args(self, port: int, user_data: str) -> list[str]:
        """Assemble Chrome CLI arguments from config fields."""
        args: list[str] = []

        # Base defaults.
        if not self.ignore_default_args:
            args.extend(CHROME_DEFAULT_ARGS)

        # Headless.
        if self.headless:
            args.extend(CHROME_HEADLESS_ARGS)

        # Sandbox / Docker.
        if self.no_sandbox:
            args.extend(CHROME_DOCKER_ARGS)

        # Security.
        if self.disable_security:
            args.extend(CHROME_DISABLE_SECURITY_ARGS)

        # Deterministic rendering.
        if self.deterministic_rendering:
            args.extend(CHROME_DETERMINISTIC_RENDERING_ARGS)

        # CDP port + user data.
        args.append(f"--remote-debugging-port={port}")
        args.append(f"--user-data-dir={user_data}")
        args.append(f"--profile-directory={self.profile_directory}")

        # Window.
        if self.start_maximized and not self.headless:
            args.append("--start-maximized")
        else:
            args.append(f"--window-size={self.window_width},{self.window_height}")

        if self.force_device_scale_factor is not None:
            args.append(f"--force-device-scale-factor={self.force_device_scale_factor}")

        # Proxy.
        if self.proxy_server:
            args.append(f"--proxy-server={self.proxy_server}")
        if self.proxy_bypass_list:
            args.append(f"--proxy-bypass-list={self.proxy_bypass_list}")

        # User agent.
        if self.user_agent:
            args.append(f"--user-agent={self.user_agent}")

        # Incognito.
        if self.incognito:
            args.append("--incognito")

        # GPU.
        if self.disable_gpu:
            args.append("--disable-gpu")

        # /dev/shm.
        if self.disable_dev_shm_usage:
            args.append("--disable-dev-shm-usage")

        # Audio.
        if self.mute_audio:
            args.append("--mute-audio")

        # Automation masking.
        if not self.disable_automation_controlled:
            # Remove the anti-detection flag if user wants automation visible.
            args = [a for a in args if "AutomationControlled" not in a]

        # Extensions.
        if self.load_extensions:
            args.append("--enable-extensions")
            args.append(f"--load-extension={','.join(self.load_extensions)}")

        # Extra raw args.
        if self.extra_args:
            args.extend(self.extra_args)

        return args

    def _chrome_args(self, port: int, user_data: str, launch_args: Sequence[str]) -> list[str]:
        """The switches of one launch: the provider's, then ``launch_args``, list switches merged.

        The one builder behind :meth:`open` and :meth:`planned_launch_flags`.
        """
        return merge_chrome_switches([*self._build_args(port, user_data), *launch_args])

    # -------------------------------------------------------------------
    # Provider interface
    # -------------------------------------------------------------------

    async def open(
        self,
        *,
        launch_args: Sequence[str] = (),
        launch_env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> ProviderSession:
        """Launch Chrome subprocess and return CDP URL.

        ``launch_args`` are switches another party owns for this session
        (``replayability: pinned``, through ``LAUNCH_ARGS``). They go after
        ``extra_args``, and every ``--js-flags`` / ``--disable-features`` /
        ``--enable-features`` is then merged into one switch, because Chrome
        keeps only the last occurrence of each. ``launch_env`` is added to the
        environment Chrome inherits from this process (``TZ``, the pinned
        tier's default time zone); without it Chrome inherits it unchanged.

        ``metadata["command_line"]`` is the exact argv. It may carry a proxy
        credential, so it never enters a trace; ``metadata["launch_flags"]``
        is the fingerprintable form — the variables ``launch_env`` sets as
        ``NAME=value``, then the switches without executable or startup URL,
        the per-launch profile path and port replaced by placeholders.
        ``metadata["launch_env"]`` is ``launch_env`` itself.
        """
        env = dict(launch_env or {})
        session_id = str(uuid.uuid4())

        # Resolve port.
        port = (
            kwargs.get("remote_debugging_port") or self.remote_debugging_port or _find_free_port()
        )

        # Resolve user data dir: the configuration's, or a fresh temporary one.
        udd = kwargs.get("user_data_dir") or self.user_data_dir
        temp_dir: str | None = None
        if udd is None:
            temp_dir = tempfile.mkdtemp(prefix="chrome-profile-")
            udd = temp_dir

        # Resolve executable.
        executable = kwargs.get("executable_path") or self.executable_path or find_chrome()

        # Build args.
        chrome_args = self._chrome_args(port, udd, launch_args)
        cmd = [executable, *chrome_args, self.startup_url]

        logger.info("Launching Chrome: %s (port=%d)", executable, port)
        logger.debug("Chrome args: %s", " ".join(cmd))

        # Launch subprocess.
        #
        # ``DEVNULL``, not ``PIPE``: nothing in this package ever reads
        # ``process.stdout`` / ``process.stderr``, and an undrained pipe is not
        # merely wasteful. It has a fixed kernel buffer (64 KiB on macOS), and
        # Chrome is chatty on stderr — GPU, font and DevTools notices — so a
        # long enough session fills it and Chrome then blocks in ``write()``
        # forever, with no log line to say so. The visible symptom was milder:
        # the transport keeps a pipe protocol alive past the run, so its
        # ``__del__`` fires after the loop is gone and ``proto.pipe.close()``
        # calls ``call_soon`` on a closed loop — "RuntimeError: Event loop is
        # closed", printed after an otherwise successful run. ``DEVNULL``
        # leaves ``BaseSubprocessTransport._pipes`` empty, settling both.
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
        )

        # Wait for CDP to become ready.
        try:
            cdp_url = await self._wait_for_cdp(port)
        except Exception:
            await self._kill_process(process)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        # Store for cleanup.
        self._processes[session_id] = process
        if temp_dir:
            self._temp_dirs[session_id] = temp_dir

        return ProviderSession(
            session_id=session_id,
            provider_id=self.provider_id,
            cdp_url=cdp_url,
            metadata={
                "pid": process.pid,
                "port": port,
                "executable": executable,
                "user_data_dir": udd,
                "headless": self.headless,
                "command_line": list(cmd),
                "launch_env": env,
                "launch_flags": _fingerprintable_flags(chrome_args, env),
            },
        )

    # -------------------------------------------------------------------
    # LaunchArgsProvider (LAUNCH_ARGS)
    # -------------------------------------------------------------------

    def launch_arg_conflicts(self, launch_args: Sequence[str]) -> list[str]:
        """This provider's knobs that set what ``launch_args`` sets, differently.

        One entry per knob, naming it and the switch it contradicts.
        Repeating an owned switch with the same value is fine, and so is
        adding unrelated V8 flags or features to a list switch — the merge
        keeps both. ``deterministic_rendering`` follows the same rule (the
        user's ruling of 2026-10-13): its V8 seed contradicts a tier that pins
        another one, and passes when the two agree.
        """
        owned = list(launch_args)
        if not owned:
            return []
        problems: list[str] = []
        knobs: list[tuple[str, tuple[str, ...]]] = []
        if self.deterministic_rendering:
            knobs.append(("deterministic_rendering=true", CHROME_DETERMINISTIC_RENDERING_ARGS))
        if self.force_device_scale_factor is not None:
            knobs.append(
                (
                    f"force_device_scale_factor={self.force_device_scale_factor:g}",
                    (f"--force-device-scale-factor={self.force_device_scale_factor}",),
                )
            )
        if self.user_agent:
            knobs.append(("user_agent", (f"--user-agent={self.user_agent}",)))
        knobs.extend((f"extra_args entry {arg!r}", (arg,)) for arg in self.extra_args or ())

        for knob, args in knobs:
            for arg in args:
                clash = contradicting_switch(arg, owned)
                if clash is not None:
                    problems.append(f"{knob} contradicts {clash}")
                    break
        return problems

    def planned_launch_flags(
        self, launch_args: Sequence[str], launch_env: Mapping[str, str] | None = None
    ) -> list[str]:
        """``metadata["launch_flags"]`` of an ``open(launch_args=..., launch_env=...)``, before launching.

        The same builder as :meth:`open`; the per-launch values (port,
        profile directory) are placeholders in both, so the two are equal.
        """
        return _fingerprintable_flags(self._chrome_args(0, "<planned>", launch_args), launch_env)

    def planned_browser_defaults(
        self, launch_env: Mapping[str, str] | None = None
    ) -> BrowserDefaults:
        """The time zone and locale the browser :meth:`open` starts will fall back to, read now.

        From the executable that would be launched and the environment it
        would get (``browser_defaults.expected_browser_defaults``); a fact
        that cannot be read — the locale off macOS, an executable that cannot
        be found — is ``None``.
        """
        try:
            executable: str | None = self.executable_path or find_chrome()
        except FileNotFoundError:
            executable = None
        return expected_browser_defaults(executable=executable, launch_env=launch_env or {})

    # -------------------------------------------------------------------
    # NetworkIsolatingProvider (NETWORK_ISOLATION)
    # -------------------------------------------------------------------

    def isolation_conflicts(self) -> list[str]:
        """This provider's knobs that would route traffic around the isolation switches.

        One entry per knob: a proxy (the resolver rules then cover only the
        proxy host), a routing switch in ``extra_args`` other than an exact
        repeat of an isolation switch, and a ``startup_url`` other than
        ``about:blank`` (it loads before anything is intercepted).
        """
        problems: list[str] = []
        if self.proxy_server:
            problems.append(
                f"proxy_server={self.proxy_server!r} sends traffic through a proxy, around "
                f"the isolation's resolver rules"
            )
        if self.proxy_bypass_list:
            problems.append(
                f"proxy_bypass_list={self.proxy_bypass_list!r} is a proxy routing rule; "
                f"isolation runs with --no-proxy-server"
            )
        problems.extend(
            f"extra_args entry {arg!r} sets network routing that overrides the isolation "
            f"switches {list(NETWORK_ISOLATION_ARGS)}"
            for arg in self.extra_args or ()
            if split_switch(arg)[0] in NETWORK_ROUTING_SWITCHES
            and arg not in NETWORK_ISOLATION_ARGS
        )
        if self.startup_url != "about:blank":
            problems.append(
                f"startup_url={self.startup_url!r} loads before the replay intercepts "
                f"anything; it must be 'about:blank'"
            )
        return problems

    # -------------------------------------------------------------------
    # NetworkCaptureProvider (NETWORK_CAPTURE)
    # -------------------------------------------------------------------

    def persistent_profile(self) -> str | None:
        """Name what makes this browser start from a profile that is not fresh.

        ``None`` when every run gets a new temporary ``--user-data-dir``.
        Otherwise every source that applies, all of them configuration: the
        ``user_data_dir`` knob, a ``profile_directory`` other than
        ``"Default"`` (counted conservatively: it is how an existing profile
        is selected), and an ``extra_args`` ``--user-data-dir``, which Chrome
        lets win because it comes last.
        """
        found: list[str] = []
        if self.user_data_dir:
            found.append(f"user_data_dir={self.user_data_dir!r}")
        if self.profile_directory != "Default":
            found.append(f"profile_directory={self.profile_directory!r}")
        found.extend(
            f"extra_args entry {arg!r}"
            for arg in self.extra_args or ()
            if split_switch(arg)[0] == "--user-data-dir"
        )
        return "; ".join(found) or None

    async def close(self, session: ProviderSession | None = None) -> None:
        """Shut down Chrome process(es)."""
        if session is not None:
            await self._close_session(session.session_id)
            return
        # Close all.
        for sid in list(self._processes):
            await self._close_session(sid)

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    async def _wait_for_cdp(self, port: int) -> str:
        """Poll ``/json/version`` until Chrome's CDP endpoint is ready."""
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = asyncio.get_event_loop().time() + self.cdp_wait_timeout

        # ``trust_env=False`` keeps this poll off any ambient ``http_proxy``.
        # Routing loopback through a proxy does not merely add a hop: while the
        # port is still closed the proxy accepts the connection and then stalls,
        # so the poll raises ReadTimeout instead of ConnectError. Every failure
        # mode here is "not ready yet", so the retry catches TransportError as a
        # whole rather than enumerating classes — an unlisted one used to abort
        # startup on the first iteration instead of retrying to the deadline.
        # ValueError covers a torn /json/version body during Chrome's boot.
        async with httpx.AsyncClient(trust_env=False) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        ws_url = data.get("webSocketDebuggerUrl", "")
                        if ws_url:
                            logger.info("Chrome CDP ready: %s", ws_url)
                            return ws_url
                except (httpx.TransportError, ValueError):
                    pass
                await asyncio.sleep(0.2)

        raise TimeoutError(
            f"Chrome CDP endpoint did not become ready within {self.cdp_wait_timeout}s "
            f"(port {port})"
        )

    async def _close_session(self, session_id: str) -> None:
        """Close a single Chrome session."""
        process = self._processes.pop(session_id, None)
        if process is not None:
            await self._kill_process(process)

        temp_dir = self._temp_dirs.pop(session_id, None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        """Gracefully terminate, then force-kill if needed."""
        if process.returncode is not None:
            return  # Already dead.

        try:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
                logger.debug("Chrome exited gracefully (pid=%s)", process.pid)
                return
            except TimeoutError:
                pass
        except ProcessLookupError:
            return

        # Force kill.
        try:
            process.kill()
            await process.wait()
            logger.warning("Chrome force-killed (pid=%s)", process.pid)
        except ProcessLookupError:
            pass
