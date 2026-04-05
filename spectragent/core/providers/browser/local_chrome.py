"""Local Chrome browser provider.

Launches a local ``google-chrome`` / ``chromium`` subprocess with
``--remote-debugging-port`` and returns a CDP WebSocket URL.

All Chrome CLI flags are exposed as configurable ``__init__`` parameters
(Tier 2 conscribe config via ``Annotated[..., Field(...)]``).  This gives
full control over Chrome's behavior from YAML config files.

Env vars: ``CHROME_EXECUTABLE_PATH``, ``CHROME_USER_DATA_DIR``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

import httpx
from pydantic import Field

from spectragent.core.providers.browser._registrar import BrowserProviderBase
from spectragent.core.providers.browser.capabilities import (
    CDP,
    CUSTOM_PROXY,
    EXTENSIONS,
    PROFILE,
    SCREEN_SIZE,
)
from spectragent.core.providers.session import ProviderSession

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

CHROME_HEADLESS_ARGS: tuple[str, ...] = (
    "--headless=new",
)

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


def _find_chrome() -> str:
    """Find a Chrome / Chromium executable on the system."""
    # Env override.
    env_path = os.environ.get("CHROME_EXECUTABLE_PATH")
    if env_path and shutil.which(env_path):
        return env_path

    for candidate in _LINUX_PATHS:
        found = shutil.which(candidate)
        if found:
            return found

    raise FileNotFoundError(
        "Cannot find Chrome / Chromium. Install with:\n"
        "  apt install google-chrome-stable\n"
        "Or set CHROME_EXECUTABLE_PATH."
    )


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
        PROFILE,
        SCREEN_SIZE,
    }

    # ── executable ─────────────────────────────────────────────────────
    executable_path: Annotated[
        str | None,
        Field(description="Chrome / Chromium binary path (auto-detected if None). Env: CHROME_EXECUTABLE_PATH"),
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
        Field(description="Chrome profile directory. A temp dir is created if None. Env: CHROME_USER_DATA_DIR"),
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
        Field(description="Proxy server URL, e.g. 'socks5://127.0.0.1:1080' or 'http://proxy:8080'"),
    ] = None

    proxy_bypass_list: Annotated[
        str | None,
        Field(description="Comma-separated hosts to bypass proxy (e.g. 'localhost,127.0.0.1')"),
    ] = None

    # ── security ───────────────────────────────────────────────────────
    disable_security: Annotated[
        bool,
        Field(description="Disable web security (--disable-web-security, --ignore-certificate-errors). Use for testing only!"),
    ] = False

    no_sandbox: Annotated[
        bool,
        Field(description="Disable Chrome sandbox (--no-sandbox). Required in Docker / rootless."),
    ] = False

    # ── extensions ─────────────────────────────────────────────────────
    load_extensions: Annotated[
        list[str] | None,
        Field(description="Paths to unpacked Chrome extensions to load"),
    ] = None

    # ── performance ────────────────────────────────────────────────────
    disable_gpu: Annotated[
        bool,
        Field(description="Disable GPU acceleration (--disable-gpu)"),
    ] = False

    disable_dev_shm_usage: Annotated[
        bool,
        Field(description="Disable /dev/shm usage (--disable-dev-shm-usage). Recommended in Docker."),
    ] = False

    # ── user agent ─────────────────────────────────────────────────────
    user_agent: Annotated[
        str | None,
        Field(description="Custom user-agent string"),
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
        Field(description="Hide automation flag from navigator.webdriver (--disable-blink-features=AutomationControlled)"),
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
        Field(description="Force deterministic rendering (fixed seed, font hinting off). Useful for visual regression."),
    ] = False

    force_device_scale_factor: Annotated[
        float | None,
        Field(description="Override device scale factor (e.g. 1.0, 2.0)"),
    ] = None

    # ── advanced ───────────────────────────────────────────────────────
    remote_debugging_port: Annotated[
        int | None,
        Field(description="Fixed CDP debugging port. Auto-allocated if None."),
    ] = None

    extra_args: Annotated[
        list[str] | None,
        Field(description="Additional raw Chrome CLI flags (e.g. ['--disable-features=Foo', '--enable-logging'])"),
    ] = None

    ignore_default_args: Annotated[
        bool,
        Field(description="If True, skip all CHROME_DEFAULT_ARGS and only use flags from explicit config"),
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
    _processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict, init=False, repr=False)
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
            args.extend((
                "--deterministic-mode",
                "--js-flags=--random-seed=1157259159",
                "--font-render-hinting=none",
            ))

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

    # -------------------------------------------------------------------
    # Provider interface
    # -------------------------------------------------------------------

    async def open(self, **kwargs: Any) -> ProviderSession:
        """Launch Chrome subprocess and return CDP URL."""
        session_id = str(uuid.uuid4())

        # Resolve port.
        port = kwargs.get("remote_debugging_port") or self.remote_debugging_port or _find_free_port()

        # Resolve user data dir.
        udd = kwargs.get("user_data_dir") or self.user_data_dir or os.environ.get("CHROME_USER_DATA_DIR")
        temp_dir: str | None = None
        if udd is None:
            temp_dir = tempfile.mkdtemp(prefix="chrome-profile-")
            udd = temp_dir

        # Resolve executable.
        executable = kwargs.get("executable_path") or self.executable_path or _find_chrome()

        # Build args.
        chrome_args = self._build_args(port, udd)
        cmd = [executable, *chrome_args, self.startup_url]

        logger.info("Launching Chrome: %s (port=%d)", executable, port)
        logger.debug("Chrome args: %s", " ".join(cmd))

        # Launch subprocess.
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            },
        )

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

        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        ws_url = data.get("webSocketDebuggerUrl", "")
                        if ws_url:
                            logger.info("Chrome CDP ready: %s", ws_url)
                            return ws_url
                except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
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
            except asyncio.TimeoutError:
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
