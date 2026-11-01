"""AndroidCapability: an Android device as one capability.

``docs/architecture_design.md`` §4.9 and §11.2. The capability owns what the
device means: its fragments (``observations.py``), its actions
(``actions.py``), when the screen counts as still (``settle.py``) and which
package an app name means (``open_app``). Below it, all in this package: the
provider (``backends/``) brings the device up and hands over its ADB endpoint;
the host transport (``adb.py``) and the device session (``session.py``) carry
every command, one job at a time; the bridge (``bridge.py``) turns what the
capability asks into device commands.

**One observation cycle is one capture** (``tests/_helpers/capability_contract.py``).
The first fragment asked for in a cycle starts it, as one job of the device's
queue: wait until the screen is still, then ask every negotiated source at
once, each within its own timeout (the screenshot, the foreground app, the
current input method), and build every negotiated fragment from that one
capture. A source that fails fails the whole capture, once: the error is kept
for the rest of the cycle and raised for every fragment asked for. The next
cycle captures again. Only the negotiated fragments are captured.

**An action is one job of the same queue**, so a capture never interleaves
with it, and it drops the capture it came after: whatever reads next sees the
device after the action. ``android.wait`` touches nothing and does not hold
the queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, cast

from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.base import BaseAction
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.android.actions import ACTIONS
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    BaseAndroidProvider,
)
from spectragent.core.capabilities.android.bridge import DeviceBridge
from spectragent.core.capabilities.android.observations import (
    CAPTURE_STATE,
    FOREGROUND_APP,
    PLATFORM,
    SCREENSHOT,
    AndroidCaptureStateObs,
    AndroidForegroundAppObs,
    AndroidPlatformObs,
    AndroidScreenshotObs,
)
from spectragent.core.capabilities.android.session import (
    DeviceBusyError,
    DeviceSession,
    Source,
    ask,
    fan_out,
)
from spectragent.core.capabilities.android.settle import Drawing, wait_until_still
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.image import image_size

logger = logging.getLogger(__name__)

#: Every action this capability offers; a YAML ``actions:`` block narrows it.
ACTION_KEYS = tuple(action.__registry_key__ for action in ACTIONS)


class _AndroidReceptors:
    """Annotation-only declaration of the ``actions`` receptor (as on the browser).

    ``ConfigComposer`` reads the class annotation to see that the slot is a
    list; it lives here because :class:`AndroidCapability` exposes ``actions``
    as a property.
    """

    actions: list[ActionBase]


@dataclass
class _Capture:
    """One cycle's capture: its fragments once taken, or why taking them failed."""

    cycle: int
    fragments: dict[str, FragmentBase] | None = None
    error: Exception | None = None


class AndroidCapability(_AndroidReceptors, CapabilityBase):
    """An Android device: its screen, input and foreground app, reached over ADB."""

    __registry_key__: ClassVar[str] = "android"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": {
            "registry": "observation",
            "keys": [SCREENSHOT, FOREGROUND_APP, PLATFORM, CAPTURE_STATE],
            "declare_only": True,
        },
        # The attribute the Composer fills (``self.actions``) from the action
        # registry: every action offered; YAML narrows it, never widens it.
        "actions": ("action", list(ACTION_KEYS)),
        "backend": "android_provider",
    }

    backend: BaseAndroidProvider  # type: ignore[misc]

    settle_quiet_ms: int
    settle_max_wait_s: float
    screenshot_timeout_s: float
    query_timeout_s: float
    action_timeout_s: float
    queue_wait_s: float

    _actions_by_name: dict[str, ActionBase]
    _session: AndroidProviderSession | None
    _device: DeviceSession | None
    _bridge: DeviceBridge | None
    #: The screen in the pixels taps take: from ``wm size`` at setup, then each screenshot's.
    _screen_size: tuple[int, int] | None
    _fingerprint: str | None
    _faketouch: bool | None
    _cycle: int
    _capture: _Capture | None

    def __init__(
        self,
        id: Annotated[
            str | None,
            PydanticField(
                description=(
                    "Instance identity. Unique across the whole config; defaults to "
                    "'<capability_id>#<ordinal>' when omitted."
                ),
            ),
        ] = None,
        settle_quiet_ms: Annotated[
            int,
            PydanticField(
                ge=0,
                description=(
                    "How long the focused app must draw no new frame before a capture counts "
                    "the screen as still, in ms. A focused text field's cursor draws a frame "
                    "about every 500 ms, so a longer period waits for the limit there."
                ),
            ),
        ] = 300,
        settle_max_wait_s: Annotated[
            float,
            PydanticField(
                ge=0.0,
                description=(
                    "The longest wait for a still screen before each capture, in seconds; the "
                    "capture is taken then whether or not the screen is still. 0 turns the "
                    "wait off: capture_state then reports no settle."
                ),
            ),
        ] = 2.0,
        screenshot_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds the screenshot may take before the capture fails; screencap took "
                    "about 0.7 s on the emulator."
                ),
            ),
        ] = 10.0,
        query_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds each device query of a capture (the foreground app, the input "
                    "method, one poll of the wait for a still screen) may take."
                ),
            ),
        ] = 5.0,
        action_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds one action may run on the device before it is cancelled and "
                    "fails; typing takes about 0.1 s per character."
                ),
            ),
        ] = 60.0,
        queue_wait_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds a capture or an action may wait for the device's earlier jobs "
                    "before it fails without running."
                ),
            ),
        ] = 30.0,
        actions: Annotated[
            list[ActionBase] | None,
            PydanticField(
                description=(
                    "Actions this capability instantiates, each with its own settings "
                    "(gesture durations, the open_app table, ...). Omitted or null (the "
                    "default): every action the wiring declares. Listing a subset is an "
                    "ablation: the actions left out are not built, not advertised to the "
                    "model, and fail negotiation if an agent requires one. [] builds none."
                ),
            ),
        ] = None,
    ) -> None:
        super().__init__(id=id)
        if settle_max_wait_s > 0 and settle_quiet_ms > settle_max_wait_s * 1000:
            raise ValueError(
                f"settle_quiet_ms ({settle_quiet_ms}) is longer than settle_max_wait_s "
                f"({settle_max_wait_s:g} s): the quiet period could never pass, so every capture "
                "would wait the whole limit. Shorten the period, raise the limit, or set "
                "settle_max_wait_s: 0 to turn the wait off."
            )
        self.settle_quiet_ms = settle_quiet_ms
        self.settle_max_wait_s = settle_max_wait_s
        self.screenshot_timeout_s = screenshot_timeout_s
        self.query_timeout_s = query_timeout_s
        self.action_timeout_s = action_timeout_s
        self.queue_wait_s = queue_wait_s
        self._actions_by_name = {}
        self._session = None
        self._device = None
        self._bridge = None
        self._screen_size = None
        self._fingerprint = None
        self._faketouch = None
        self._cycle = 0
        self._capture = None
        self.actions = actions

    # ── actions ───────────────────────────────────────────────────────

    @property
    def actions(self) -> list[ActionBase]:
        """The action instances this capability executes."""
        return list(self._actions_by_name.values())

    @actions.setter
    def actions(self, value: list[ActionBase] | None) -> None:
        """Adopt a configured list, or build every wired action when it is ``None``."""
        if value is None:
            value = [action() for action in ACTIONS]
        unknown = sorted({action.action_id for action in value} - set(ACTION_KEYS))
        if unknown:
            raise ValueError(
                f"AndroidCapability was configured with actions it does not provide: {unknown}. "
                f"It provides {sorted(ACTION_KEYS)}."
            )
        self._actions_by_name = {action.action_id: action for action in value}

    @property
    def provided_actions(self) -> frozenset[str]:
        return frozenset(self._actions_by_name)

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return [action.descriptor(**context) for action in self._actions_by_name.values()]

    # ── what the actions use ──────────────────────────────────────────

    @property
    def bridge(self) -> DeviceBridge:
        """The device the actions act on, between setup and teardown."""
        if self._bridge is None:
            raise RuntimeError("the Android device is not set up")
        return self._bridge

    @property
    def screen_size(self) -> tuple[int, int]:
        """The screen's width and height in the pixels taps take, as last measured."""
        if self._screen_size is None:
            raise RuntimeError("the Android device is not set up")
        return self._screen_size

    # ── lifecycle ─────────────────────────────────────────────────────

    async def setup(self, *, negotiation: Any = None) -> None:
        """Bring the device up through the backend and read the facts that do not change."""
        await super().setup(negotiation=negotiation)
        backend = self._backend()
        session = await backend.open()
        self._session = session
        try:
            bridge = DeviceBridge.over_adb(
                AdbClient(session.endpoint.host, session.endpoint.port), session.serial
            )
            device = DeviceSession(session.serial)
            facts = bridge.facts

            async def read_facts() -> tuple[tuple[int, int], str | None, bool | None]:
                return (
                    await facts.screen_size(),
                    await facts.fingerprint(),
                    await facts.has_feature("android.hardware.faketouch"),
                )

            self._screen_size, self._fingerprint, self._faketouch = await device.run(
                read_facts,
                what="reading the device's facts",
                wait_s=self.queue_wait_s,
                run_s=3 * self.query_timeout_s,
            )
            self._bridge, self._device = bridge, device
        except BaseException:
            await self.teardown()
            raise

    async def teardown(self) -> None:
        """Let the device's queue drain, then release it through the backend. Safe to repeat."""
        device, session = self._device, self._session
        self._device = self._bridge = None
        self._session = None
        self._capture = None
        if device is not None:
            try:
                await device.flush(wait_s=self.queue_wait_s)
            except DeviceBusyError:
                logger.warning("releasing device %s while a job of it still runs", device.serial)
        if session is not None:
            await self._backend().close(session)

    @property
    def is_alive(self) -> bool:
        return self._device is not None

    def _backend(self) -> BaseAndroidProvider:
        try:
            return self.backend
        except AttributeError:
            raise RuntimeError(
                "AndroidCapability has no backend: configure an android_provider "
                "(provider_id: local_emulator)"
            ) from None

    # ── actions ───────────────────────────────────────────────────────

    async def execute_action(self, action: Action) -> ActionResult:
        """Run one action as a job of the device's queue; a failure is the result, never raised."""
        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(success=False, error=f"Unknown action: {action.name}")
        device = self._device
        if device is None:
            return ActionResult(success=False, error="the Android device is not set up")
        self._capture = None  # what reads next must see the device after this action
        executor = cast(BaseAction, impl)

        async def act() -> ActionResult:
            return await executor.execute(action.params, capability=self)

        try:
            if impl.only_waits:
                return await act()
            return await device.run(
                act, what=action.name, wait_s=self.queue_wait_s, run_s=self.action_timeout_s
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ── observation ───────────────────────────────────────────────────

    async def begin_observation(self, cycle: int) -> None:
        """Open cycle *cycle*: the next fragment asked for captures again."""
        self._cycle = cycle

    async def resolve_fragment(self, fragment_key: str) -> FragmentBase:
        """Serve *fragment_key* from this cycle's capture, taking it on the first call."""
        capture = self._capture
        if capture is None or capture.cycle != self._cycle:
            cycle = self._cycle
            try:
                capture = _Capture(cycle, fragments=await self._capture_now())
            except Exception as exc:  # kept: the rest of the cycle fails with it, uncaptured
                capture = _Capture(cycle, error=exc)
            self._capture = capture
        if capture.error is not None:
            raise capture.error
        fragment = (capture.fragments or {}).get(fragment_key)
        if fragment is None:
            raise KeyError(
                f"android does not capture {fragment_key!r} in this run; it captures "
                f"{sorted(capture.fragments or {})}"
            )
        return fragment

    async def _capture_now(self) -> dict[str, FragmentBase]:
        device = self._device
        if device is None:
            raise RuntimeError("the Android device is not set up")
        # Each part is bounded on its own (the wait's last poll, then the slowest
        # source); this only stops a runaway capture.
        sources_s = max(self.screenshot_timeout_s, self.query_timeout_s)
        limit = self.settle_max_wait_s + 2 * self.query_timeout_s + sources_s + 1
        return await device.run(
            self._capture_job, what="capture", wait_s=self.queue_wait_s, run_s=limit
        )

    async def _capture_job(self) -> dict[str, FragmentBase]:
        wanted = self.provided_fragments
        settle = None
        if self.settle_max_wait_s > 0:
            settle = await wait_until_still(
                self._drawing,
                quiet_s=self.settle_quiet_ms / 1000,
                max_wait_s=self.settle_max_wait_s,
            )
        sources: dict[str, Source[FragmentBase]] = {}
        if SCREENSHOT in wanted:
            sources[SCREENSHOT] = Source(self._screenshot, self.screenshot_timeout_s)
        if FOREGROUND_APP in wanted:
            sources[FOREGROUND_APP] = Source(self._foreground_app, self.query_timeout_s)
        if PLATFORM in wanted:
            sources[PLATFORM] = Source(self._platform, self.query_timeout_s)
        fragments = await fan_out(sources)
        if CAPTURE_STATE in wanted:
            fragments[CAPTURE_STATE] = AndroidCaptureStateObs(settle=settle)
        return fragments

    async def _drawing(self) -> Drawing:
        """One poll of the wait for a still screen: the focused app and its frames so far."""
        apps = self.bridge.apps
        front = await ask(Source(apps.foreground, self.query_timeout_s))
        package = front.package
        if package is None:
            return None, None
        return package, await ask(Source(lambda: apps.frames_drawn(package), self.query_timeout_s))

    async def _screenshot(self) -> FragmentBase:
        screen = self.bridge.screen
        image = await screen.capture()
        width, height = image_size(image)
        if width and height:
            self._screen_size = (width, height)
        return AndroidScreenshotObs(
            image_bytes=image,
            image_width=width,
            image_height=height,
            viewport_width=width,
            viewport_height=height,
            producer=screen.producer,
        )

    async def _foreground_app(self) -> FragmentBase:
        front = await self.bridge.apps.foreground()
        return AndroidForegroundAppObs(
            package=front.package, activity=front.activity, window=front.window
        )

    async def _platform(self) -> FragmentBase:
        return AndroidPlatformObs(
            fingerprint=self._fingerprint,
            input_method=await self.bridge.facts.input_method(),
            faketouch=self._faketouch,
        )
