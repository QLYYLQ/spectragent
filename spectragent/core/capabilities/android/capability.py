"""AndroidCapability: an Android device as one capability.

``docs/architecture_design.md`` §4.9 and §11.2. The capability owns what the
device means: its fragments (``observations.py``), its actions
(``actions.py``), when the screen counts as still (``settle.py``) and which
package an app name means (``open_app``). Below it, all in this package: the
provider (``backends/``) brings the device up and hands over its ADB endpoint;
the host transport (``adb.py``) and the device session (``session.py``) carry
every command, one job at a time; the device side (``device/``), our
accessibility service and shell helper, takes the screenshots and delivers
touches, keys and text; the bridge (``bridge.py``) is what the capability asks
of all of it.

**The device side runs for the whole run** (``device/lifecycle.py``). Setup
installs the configured APK (``service_apk``), starts the helper, enables the
service and confirms it with a ping, under §11.2's four hard constraints;
teardown takes the service out of the setting and ends the helper, never
force-stopping our package. ``check_setup`` refuses an APK without its sidecar,
one whose bytes the sidecar does not describe, and one of another protocol
before the trace opens, and setup refuses a device that runs another build than
the configured one. The APK's identity (its SHA-256 measured on the device, its
version, ``isAccessibilityTool`` as the system read it, the certificate that
signed it) rides on every ``android.platform`` fragment (§11.2, "实验身份").

**Two strategies choose how input reaches the device**, and a run names both:
the tap injector delivers every touch, the typing backend enters text
(``input/``). The screenshot is not a choice: the helper's where it works (API
31-33), else the service's, decided once at setup by asking the helper, and
recorded with every screenshot.

**Three strategies make the UI tree** (``ui_tree/``): the source captures the
forest (one today, our service, so a block may leave it out), the cleaner
picks and numbers the elements, the serializer prints them and describes what
it prints (``fragment_formats``). A run names the cleaner and the serializer.

**One observation cycle is one capture** (``tests/_helpers/capability_contract.py``).
The first fragment asked for in a cycle starts it, as one job of the device's
queue: wait until the screen is still (``settle.py``), then ask every
negotiated source at once, each within its own timeout (the screenshot, the
foreground app, the current input method, the UI tree), and build every
negotiated fragment from that one capture; the tree is cleaned once every
source answered, against the screen size the capture's screenshot measured. A
source that fails fails the whole capture, once: the error is kept for the
rest of the cycle and raised for every fragment asked for. The next cycle
captures again. Only the negotiated fragments are captured, so a run whose
agent reads no tree never asks the service for one.

**An action is one job of the same queue**, so a capture never interleaves
with it, and it drops the capture it came after: whatever reads next sees the
device after the action. ``android.wait`` touches nothing and does not hold
the queue. A job that gave up on a device call confirms it done before the
device is handed on (``session.py``).

**An element index names an element of the tree the agent last saw**: the
latest capture's, until the next action starts (:meth:`AndroidCapability.element_centre`).
An index into an older tree, or one the tree does not have, is refused, and
nothing is touched.

**The service is kept bound.** Something on the device can unbind it during a
run: AndroidWorld's own service writes its accessibility forwarder over the
setting when it starts, and a task may force-stop every app. Each job first
pings the service and, when it does not answer, enables it again and confirms
it (``DeviceSide.ensure_bound``), then subscribes afresh; setup does the same
once the task oracle has started. A ping costs about 3 ms.

**The task oracle** (``oracle/``, the ``task_oracle`` receptor; ``none``
selects no benchmark) prepares, scores and ends a benchmark's task on the
device. The capability hands the provider what the oracle needs of the device
(``device_needs``), passes it the task's ``oracle_task`` unread, starts it once
the device is up, stops it first at teardown, and implements ``TaskOracle`` and
``SetupCheck`` by asking it (§11.2 "任务裁判与 ``AndroidSetup``").

**Archivers** (``archive/``, the archive axis of §4.5) are bound before setup
and chosen only by the trace hook's ``archive`` list. Setup assembles the
recording's context and opens a session per bound archiver; a capture that
succeeded hands its UI tree to the context while a bound archiver needs it;
``collect_archive`` asks every session for what it has. A run that binds none
records nothing, and an archiver that needs the tree is refused in a run that
captures none.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, cast

from pydantic import Field as PydanticField
from pydantic import JsonValue

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.base import BaseAction
from spectragent.core.archive import ArchiveBatch, ArchiveFile, ArchiveGap, ArchiverBase
from spectragent.core.capabilities._registrar import CapabilityBase, negotiated_fragment_keys
from spectragent.core.capabilities.android.actions import ACTIONS
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.archive import ui_tree_raw  # noqa: F401 — registration
from spectragent.core.capabilities.android.archive.base import (
    AndroidArchiveContext,
    AndroidArchiverBase,
    AndroidArchiveSession,
    CommittedTree,
    TreeStore,
    UnmetArchiverNeeds,
    android_archivers,
)
from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    BaseAndroidProvider,
    UnmetDeviceNeeds,
)
from spectragent.core.capabilities.android.bridge import (
    AdbQueries,
    DeviceBridge,
    DeviceInput,
    ProgramScreen,
)
from spectragent.core.capabilities.android.device.apk import ServiceApk
from spectragent.core.capabilities.android.device.channel import ChannelError, RemoteError
from spectragent.core.capabilities.android.device.clients import (
    ScreenshotProducer,
    ServiceEvents,
    TreeCapture,
)
from spectragent.core.capabilities.android.device.lifecycle import DeviceSide, DeviceSideError
from spectragent.core.capabilities.android.input import tap, text  # noqa: F401 — registration
from spectragent.core.capabilities.android.input._registrar import (
    AndroidTapInjector,
    AndroidTypingBackend,
    TypingBackendUnsupported,
)
from spectragent.core.capabilities.android.observations import (
    CAPTURE_STATE,
    FOREGROUND_APP,
    PLATFORM,
    SCREENSHOT,
    UI_TREE,
    AndroidCaptureStateObs,
    AndroidForegroundAppObs,
    AndroidPlatformObs,
    AndroidScreenshotObs,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.oracle import (  # noqa: F401 — registration
    androidworld,
    none,
)
from spectragent.core.capabilities.android.oracle._registrar import (
    AndroidTaskOracle,
    default_task_oracle,
)
from spectragent.core.capabilities.android.session import (
    DeviceBusyError,
    DeviceSession,
    Source,
    ask,
    fan_out,
)
from spectragent.core.capabilities.android.settle import EventsEnded, ScreenPoll, wait_until_still
from spectragent.core.capabilities.android.ui_tree import (  # noqa: F401 — registration
    cleaning,
    serializers,
    source,
)
from spectragent.core.capabilities.android.ui_tree._registrar import (
    AndroidUITreeCleaner,
    AndroidUITreeSerializer,
    AndroidUITreeSource,
    ScreenSize,
    default_ui_tree_source,
)
from spectragent.core.capabilities.android.ui_tree.models import ElementIndexError, UITree
from spectragent.core.trace.redaction import trace_safe
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.oracle_score import OracleReading

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
    """An Android device: its screen, input and foreground app, through our device side and ADB."""

    __registry_key__: ClassVar[str] = "android"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": {
            "registry": "observation",
            "keys": [SCREENSHOT, FOREGROUND_APP, PLATFORM, CAPTURE_STATE, UI_TREE],
            "declare_only": True,
        },
        # The attribute the Composer fills (``self.actions``) from the action
        # registry: every action offered; YAML narrows it, never widens it.
        "actions": ("action", list(ACTION_KEYS)),
        "backend": "android_provider",
        # Required: which program delivers a touch, and which enters text,
        # changes what the device receives, so a run names both (§4.2.1).
        "tap_injector": ("android_tap_injector", ["accessibility_gesture", "shell_injection"]),
        "typing_backend": ("android_typing_backend", ["set_text", "commit_text"]),
        # Omitted in YAML: ``none``, the oracle ``__init__`` installs. The
        # strategy is the switch: what the device must offer comes from it,
        # never from a provider setting.
        "task_oracle": {
            "registry": "android_task_oracle",
            "keys": ["none", "androidworld"],
            "default": "none",
        },
        # Omitted in YAML: our accessibility service, the only source there is
        # (``uiautomator dump`` is deferred, §11.2), which ``__init__`` installs.
        "ui_tree_source": {
            "registry": "android_ui_tree_source",
            "keys": ["accessibility_service"],
            "default": "accessibility_service",
        },
        # Required: which nodes become elements and how they are printed is
        # what the agent sees, so a run names both (§4.2.1).
        "ui_tree_cleaner": ("android_ui_tree_cleaner", ["androidworld"]),
        "ui_tree_serializer": ("android_ui_tree_serializer", ["m3a", "t3a"]),
        # Archive provides (§4.5), declared like ``observation``: what a run
        # records is the trace hook's ``archive`` list, bound here by
        # ``bind_archivers``. The classes are imported above, so the keys
        # resolve when conscribe reads this entry.
        "archiver": {
            "registry": "archiver",
            "keys": ["android.ui_tree_raw"],
            "declare_only": True,
        },
    }

    backend: BaseAndroidProvider  # type: ignore[misc]
    tap_injector: AndroidTapInjector  # type: ignore[misc]
    typing_backend: AndroidTypingBackend  # type: ignore[misc]
    task_oracle: AndroidTaskOracle  # type: ignore[misc]
    ui_tree_source: AndroidUITreeSource  # type: ignore[misc]
    ui_tree_cleaner: AndroidUITreeCleaner  # type: ignore[misc]
    ui_tree_serializer: AndroidUITreeSerializer  # type: ignore[misc]

    service_apk: str
    settle_quiet_ms: int
    settle_max_wait_s: float
    screenshot_timeout_s: float
    query_timeout_s: float
    ui_tree_timeout_s: float
    action_timeout_s: float
    queue_wait_s: float
    service_start_timeout_s: float

    _actions_by_name: dict[str, ActionBase]
    _apk: ServiceApk | None
    _session: AndroidProviderSession | None
    _side: DeviceSide | None
    _device: DeviceSession | None
    _bridge: DeviceBridge | None
    #: The service's accessibility events; ``None`` once the stream ended.
    _events: ServiceEvents | None
    #: When the newest event so far happened, in the device's uptime ms.
    _newest_event_ms: int | None
    #: The app the last poll of a still screen found in front.
    _polled_package: str | None
    #: The screen in the pixels touches take: from ``wm size`` at setup, then each screenshot's.
    _screen_size: tuple[int, int] | None
    _fingerprint: str | None
    _cycle: int
    _capture: _Capture | None
    #: The cleaned tree of the latest capture that took one, and how many
    #: actions started since: an element index is good for the first one only.
    _ui_tree: UITree | None
    _actions_since_tree: int
    #: The bound archivers (``bind_archivers``), in binding order.
    _archivers: list[AndroidArchiverBase]
    #: The recording's context, from setup to teardown, and the bound archivers' sessions.
    _archive: AndroidArchiveContext | None
    _archive_sessions: list[AndroidArchiveSession]

    def __init__(
        self,
        service_apk: Annotated[
            str,
            trace_safe(
                "a build's path, under a directory named by its SHA-256; not a credential",
                description=(
                    "The device-side APK to install for the run: the path that `uv run python -m "
                    "spectragent.core.capabilities.android.device.apk build` prints, with its "
                    "sidecar beside it. The run refuses an APK whose sidecar is missing or does "
                    "not describe it, that was built for another protocol, or that is not in the "
                    "directory its build names (the first 16 characters of its SHA-256)."
                ),
            ),
        ],
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
                    "How long nothing may change before a capture counts the screen as still, "
                    "in ms: no accessibility event, the same focused app and window, no new "
                    "frame. A focused text field's cursor draws a frame about every 500 ms, so a "
                    "longer period waits for the limit there."
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
                    "Seconds the screenshot may take before the capture fails; the helper took "
                    "about 0.12 s on the emulator, the service about 0.2 s."
                ),
            ),
        ] = 10.0,
        query_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds each device query may take: those of a capture (the foreground "
                    "app, the input method, one poll of the wait for a still screen), and the "
                    "ping that confirms a device call a job gave up on has finished."
                ),
            ),
        ] = 5.0,
        ui_tree_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds the UI tree's capture may take before the whole capture fails. "
                    "Settings' main page took about 0.1 s on the API 33 emulator; the first "
                    "walk of a long web page after it loaded took up to 8.4 s, later walks "
                    "about 1.1 s at the source's 5000-node limit."
                ),
            ),
        ] = 10.0,
        action_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds one action may run on the device before it is cancelled and fails."
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
        service_start_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0.0,
                description=(
                    "Seconds the device side may take to come up at setup (the APK installed, "
                    "the helper started, the service enabled and answering a ping; about 12 s "
                    "after a read-only boot on the emulator), and to come back when something "
                    "on the device unbound the service."
                ),
            ),
        ] = 60.0,
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
        self.service_apk = service_apk
        self.settle_quiet_ms = settle_quiet_ms
        self.settle_max_wait_s = settle_max_wait_s
        self.screenshot_timeout_s = screenshot_timeout_s
        self.query_timeout_s = query_timeout_s
        self.ui_tree_timeout_s = ui_tree_timeout_s
        self.action_timeout_s = action_timeout_s
        self.queue_wait_s = queue_wait_s
        self.service_start_timeout_s = service_start_timeout_s
        self._actions_by_name = {}
        self._apk = None
        self._session = None
        self._side = None
        self._device = None
        self._bridge = None
        self._events = None
        self._newest_event_ms = None
        self._polled_package = None
        self._screen_size = None
        self._fingerprint = None
        self._cycle = 0
        self._capture = None
        self._ui_tree = None
        self._actions_since_tree = 0
        self._archivers = []
        self._archive = None
        self._archive_sessions = []
        self.actions = actions
        self.task_oracle = default_task_oracle()
        self.ui_tree_source = default_ui_tree_source()

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
        """The screen's width and height in the pixels touches take, as last measured."""
        if self._screen_size is None:
            raise RuntimeError("the Android device is not set up")
        return self._screen_size

    def element_centre(self, index: int) -> tuple[int, int]:
        """The centre of element *index*'s bounds on the screen (:meth:`element_bounds`).

        So an element partly off the screen is touched where it shows.

        Raises:
            ElementIndexError: As :meth:`element_bounds`.
        """
        left, top, right, bottom = self.element_bounds(index)
        return (left + right) // 2, (top + bottom) // 2

    def element_bounds(self, index: int) -> tuple[int, int, int, int]:
        """Element *index*'s bounds cut to the screen: ``(left, top, right, bottom)``.

        The element is the one of the UI tree the agent last saw: the latest
        capture's, while the action asking is the first to start since. After
        another action the screen may have changed, and the numbers with it.
        The bounds are cut to the screen, as AndroidWorld cuts them before it
        scrolls inside an element.

        Raises:
            ElementIndexError: The run captures no UI tree, none was captured
                yet, an action started since it was, it has no element *index*,
                or the element is not on the screen; the message says which.
        """
        tree = self._ui_tree
        if UI_TREE not in self.provided_fragments:
            raise ElementIndexError(
                f"Element {index} names nothing: this run does not capture the UI tree."
            )
        if tree is None:
            raise ElementIndexError(
                f"Element {index} names nothing: no UI tree was captured yet in this run."
            )
        if self._actions_since_tree > 1:
            raise ElementIndexError(
                f"Element {index} is from the UI tree captured before the last action, and the "
                "screen may have changed since; the next observation numbers the elements again."
            )
        node = tree.node(index)
        if node is None:
            raise ElementIndexError(
                f"The screen's UI tree has no element {index}; its elements are "
                f"{_numbers(number for number, _ in tree.element_nodes())}."
            )
        box = node.bounds_in_screen
        width, height = self.screen_size
        left, top = max(box.left, 0), max(box.top, 0)
        right, bottom = min(box.right, width), min(box.bottom, height)
        if left >= right or top >= bottom:
            raise ElementIndexError(f"Element {index} is not on the screen.")
        return left, top, right, bottom

    # ── how the fragments read as text (FragmentFormats, §4.4) ────────

    def fragment_formats(self) -> Mapping[str, str]:
        """``android.ui_tree``'s format, in its configured serializer's own words.

        The text is the serializer's, so is its description; nothing here
        restates it. Reads configuration only. A capability built by hand
        without a serializer describes nothing (and its setup refuses it).
        """
        try:
            serializer = self.ui_tree_serializer
        except AttributeError:
            return {}
        return {UI_TREE: serializer.describe_format()}

    # ── lifecycle ─────────────────────────────────────────────────────

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
    ) -> None:
        """Refuse now what setup would: an archiver the run's capture cannot feed, a device the
        provider cannot give the oracle or the typing backend, the APK, then the task.

        Raises:
            TypeError: An archiver that is not an Android archiver.
            UnmetArchiverNeeds: An archiver that needs the UI tree, in a run
                whose negotiation leaves ``android.ui_tree`` out; names both.
            UnmetDeviceNeeds: The provider cannot meet what the oracle needs
                of the device; names the oracle, the needs and the provider.
            TypingBackendUnsupported: The device's API level, as the provider
                reads it from configuration, cannot run the typing backend.
            ServiceApkError: ``service_apk`` names no APK with a sidecar that
                describes it, one built for another protocol or package, or one
                outside the directory named by its SHA-256.
            Exception: The oracle's own refusal of its installation or task.
        """
        _check_archivers(android_archivers(archivers), negotiated_fragment_keys(negotiation))
        oracle = self.task_oracle
        try:  # setup refuses a capability without a backend or a typing backend itself
            backend: BaseAndroidProvider | None = self.backend
        except AttributeError:
            backend = None
        try:
            typing_backend: AndroidTypingBackend | None = self.typing_backend
        except AttributeError:
            typing_backend = None
        if backend is not None:
            needs = oracle.device_needs
            unmet = backend.unmet_needs(needs)
            if unmet:
                raise UnmetDeviceNeeds(
                    f"task_oracle {oracle.oracle_id!r} needs {', '.join(needs.named())} of the "
                    f"device, and the {backend.provider_id!r} provider cannot meet: "
                    + "; ".join(unmet)
                )
            level = backend.api_level()
            if typing_backend is not None and level is not None:
                _check_typing(
                    typing_backend, level, f"the {backend.provider_id!r} provider's device"
                )
        ServiceApk.load(self.service_apk)
        oracle.check_setup(context.get("oracle_task"))

    async def setup(
        self, *, negotiation: Any = None, oracle_task: Mapping[str, JsonValue] | None = None
    ) -> None:
        """Bring the device and its device side up, read the facts that do not change, start the oracle.

        *oracle_task* is the task's data for its oracle (``AndroidSetup``),
        handed over unread. The recording's archive context and the bound
        archivers' sessions open first: pure construction.
        """
        await super().setup(negotiation=negotiation)
        oracle = self.task_oracle
        backend = self._backend()
        tap_injector, typing_backend, *_ = self._strategies()
        self._ui_tree, self._actions_since_tree = None, 0
        self.check_setup(
            {"oracle_task": oracle_task}, negotiation=negotiation, archivers=self._archivers
        )
        self._archive = AndroidArchiveContext(trees=TreeStore())
        self._archive_sessions = [archiver.open(self._archive) for archiver in self._archivers]
        self._apk = apk = ServiceApk.load(self.service_apk)
        session = await backend.open(oracle.device_needs)
        self._session = session
        try:
            adb = AdbClient(session.endpoint.host, session.endpoint.port)
            side = await DeviceSide.start(
                adb, session.serial, apk, timeout_s=self.service_start_timeout_s
            )
            self._side = side
            _check_build(side, apk, typing_backend)
            device = DeviceSession(
                session.serial, paths=side.clients, confirm_s=self.query_timeout_s
            )
            queries = AdbQueries(adb, session.serial)

            async def read_facts() -> ScreenshotProducer:
                self._screen_size = await queries.screen_size()
                self._fingerprint = await queries.fingerprint()
                return await _fastest_screen(side)

            producer = await device.run(
                read_facts,
                what="reading the device's facts",
                wait_s=self.queue_wait_s,
                run_s=3 * self.query_timeout_s + self.screenshot_timeout_s,
            )
            if self._reads_events:
                self._events = await side.service.events()
                self._newest_event_ms = None
            await oracle.start(session, oracle_task)
            await device.run(
                self._keep_service,
                what="confirming the service once the task oracle started",
                wait_s=self.queue_wait_s,
                run_s=self.service_start_timeout_s + self.query_timeout_s,
            )
            self._bridge = DeviceBridge(
                screen=ProgramScreen(side, producer),
                input=DeviceInput(side, tap_injector, typing_backend),
                apps=queries,
                launcher=queries,
                facts=queries,
            )
            self._device = device
        except BaseException:
            await self.teardown()
            raise

    async def teardown(self) -> None:
        """Stop the oracle, drain the device's queue, stop the device side, release the device. Safe to repeat.

        The recording's archive closes too: the runtime collects for the last
        time before it tears down.
        """
        device, side, session = self._device, self._side, self._session
        self._device = self._bridge = None
        self._side = self._session = None
        self._events = None
        self._capture = None
        self._archive, self._archive_sessions = None, []
        try:
            await self.task_oracle.stop()
        finally:
            try:
                if device is not None:
                    try:
                        await device.flush(wait_s=self.queue_wait_s)
                    except DeviceBusyError:
                        logger.warning(
                            "releasing device %s while a job of it still runs", device.serial
                        )
                if side is not None:
                    try:
                        await side.stop()
                    except DeviceSideError as exc:
                        logger.warning("stopping the device side: %s", exc)
            finally:
                if session is not None:
                    await self._backend().close(session)

    async def read_score(self, *, answer: str | None) -> OracleReading | None:
        """The task oracle's score, given the run's *answer*; ``None`` with ``none`` (``TaskOracle``)."""
        return await self.task_oracle.read_score(answer=answer)

    @property
    def is_alive(self) -> bool:
        return self._device is not None

    # ── archive (§4.5, §4.9) ──────────────────────────────────────────

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Replace the binding (an empty one unbinds). Acquires nothing.

        Bound before setup, which is the runtime's order, the archivers are
        given sessions at setup. Bound again between setup and teardown,
        sessions are opened for the new binding; a tree committed and not yet
        collected stays with the recording until a session collects it.

        Raises:
            TypeError: An archiver that is not an Android archiver.
        """
        self._archivers = android_archivers(archivers)
        if self._archive is not None:
            self._archive_sessions = [archiver.open(self._archive) for archiver in self._archivers]

    async def collect_archive(self) -> ArchiveBatch:
        """Hand over what every bound archiver's session has, in binding order (§4.9).

        A session that cannot collect is named in the batch's gaps, and the
        others' evidence is handed over. Never set up, or nothing bound: an
        empty batch.
        """
        files: list[ArchiveFile] = []
        gaps: list[ArchiveGap] = []
        for session in self._archive_sessions:
            try:
                files.extend(await session.collect())
            except Exception as exc:
                gaps.append(ArchiveGap.of(session.archiver_id, exc))
        return ArchiveBatch(files=tuple(files), gaps=tuple(gaps))

    def _archive_tree(self, tree: CommittedTree) -> None:
        """Hand a successful capture's tree to the recording, while a bound archiver needs it."""
        if self._archive is not None and any("ui_tree" in a.needs for a in self._archivers):
            self._archive.trees.commit(tree)

    def _backend(self) -> BaseAndroidProvider:
        try:
            return self.backend
        except AttributeError:
            raise RuntimeError(
                "AndroidCapability has no backend: configure an android_provider "
                "(provider_id: local_emulator)"
            ) from None

    def _strategies(
        self,
    ) -> tuple[
        AndroidTapInjector, AndroidTypingBackend, AndroidUITreeCleaner, AndroidUITreeSerializer
    ]:
        """The strategies a run names: the tap injector, the typing backend, the tree's cleaner and serializer."""
        try:
            return (
                self.tap_injector,
                self.typing_backend,
                self.ui_tree_cleaner,
                self.ui_tree_serializer,
            )
        except AttributeError:
            raise RuntimeError(
                "AndroidCapability needs a tap_injector (injector_id: accessibility_gesture or "
                "shell_injection), a typing_backend (typing_id: set_text or commit_text), a "
                "ui_tree_cleaner (cleaner_id: androidworld) and a ui_tree_serializer "
                "(serializer_id: m3a or t3a)"
            ) from None

    @property
    def _reads_events(self) -> bool:
        """Whether a capture reads the service's events: only the wait for a still screen does."""
        return self.settle_max_wait_s > 0

    async def _keep_service(self) -> None:
        """Confirm the service with a ping, bind it again when it does not answer, and subscribe afresh.

        A job's first step, inside the job (module docstring): one ping when
        all is well. A stream that ended on an open channel is closed with
        its channel, since a channel subscribes once. With the wait for a
        still screen off nothing reads events, so none is subscribed to: they
        would pile up unread until the stream ended.
        """
        side = self._side
        if side is None:
            return
        if self._reads_events and self._events is None and side.service_connected:
            await side.service.close()
        rebound = await side.ensure_bound(timeout_s=self.service_start_timeout_s)
        if self._reads_events and (rebound or self._events is None):
            self._events = await side.service.events()
            self._newest_event_ms = None

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
        self._actions_since_tree += 1  # and only this action may still name the tree's elements
        executor = cast(BaseAction, impl)

        async def act() -> ActionResult:
            if not impl.only_waits:
                await self._keep_service()
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
        """Serve *fragment_key* from this cycle's capture, taking it on the first call.

        A capture that succeeded hands its UI tree to the recording, so the
        archive holds only trees whose fragments were served.
        """
        capture = self._capture
        if capture is None or capture.cycle != self._cycle:
            cycle = self._cycle
            try:
                fragments, tree = await self._capture_now(cycle)
            except Exception as exc:  # kept: the rest of the cycle fails with it, uncaptured
                capture = _Capture(cycle, error=exc)
            else:
                capture = _Capture(cycle, fragments=fragments)
                if tree is not None:
                    self._archive_tree(tree)
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

    async def _capture_now(
        self, cycle: int
    ) -> tuple[dict[str, FragmentBase], CommittedTree | None]:
        device = self._device
        if device is None:
            raise RuntimeError("the Android device is not set up")
        # Each part is bounded on its own (confirming the service, the wait's
        # last poll, then the slowest source); this only stops a runaway capture.
        sources_s = max(self.screenshot_timeout_s, self.query_timeout_s, self.ui_tree_timeout_s)
        limit = (
            self.service_start_timeout_s
            + self.settle_max_wait_s
            + 3 * self.query_timeout_s
            + sources_s
            + 1
        )
        return await device.run(
            lambda: self._capture_job(cycle), what="capture", wait_s=self.queue_wait_s, run_s=limit
        )

    async def _capture_job(
        self, cycle: int
    ) -> tuple[dict[str, FragmentBase], CommittedTree | None]:
        """Every negotiated fragment of one capture, and its UI tree as the source returned it."""
        await self._keep_service()
        wanted = self.provided_fragments
        settle = None
        if self.settle_max_wait_s > 0:
            settle = await wait_until_still(
                self._poll_screen,
                quiet_s=self.settle_quiet_ms / 1000,
                max_wait_s=self.settle_max_wait_s,
            )
        sources: dict[str, Source[FragmentBase | TreeCapture]] = {}
        if SCREENSHOT in wanted:
            sources[SCREENSHOT] = Source(self._screenshot, self.screenshot_timeout_s)
        if FOREGROUND_APP in wanted:
            sources[FOREGROUND_APP] = Source(self._foreground_app, self.query_timeout_s)
        if PLATFORM in wanted:
            sources[PLATFORM] = Source(self._platform, self.query_timeout_s)
        if UI_TREE in wanted:
            sources[UI_TREE] = Source(self._read_tree, self.ui_tree_timeout_s)
        fragments: dict[str, FragmentBase] = {}
        tree: CommittedTree | None = None
        for key, answer in (await fan_out(sources)).items():
            if not isinstance(answer, TreeCapture):
                fragments[key] = answer
                continue
            # Cleaned once every source answered: against the screen this
            # capture's screenshot measured, when it took one.
            screen = self.screen_size
            fragment = self._ui_tree_fragment(answer, screen)
            fragments[key] = fragment
            tree = CommittedTree(
                capture=answer,
                producer=self.ui_tree_source.source_id,
                screen=screen,
                cycle=cycle,
                captured_at=fragment.captured_at,
            )
        if CAPTURE_STATE in wanted:
            fragments[CAPTURE_STATE] = AndroidCaptureStateObs(settle=settle)
        return fragments, tree

    async def _poll_screen(self) -> ScreenPoll:
        """One poll of the wait for a still screen (``settle.py``).

        Its three questions go out at once, each on its own path: a ping on
        the service's channel, the window manager's focus, and the frame count
        of the app the last poll found in front, which counts as not drawn
        yet when another app is in front now. A poll took 200-500 ms under
        load when they went one after another.
        """
        side, events = self._side, self._events
        if side is None or events is None:
            raise EventsEnded("the service's event stream is not running")
        apps, limit, asked = self.bridge.apps, self.query_timeout_s, self._polled_package

        async def frames_of_last() -> int | None:
            return None if asked is None else await apps.frames_drawn(asked)

        ping, front, frames = await asyncio.gather(
            ask(Source(side.service.ping, limit)),
            ask(Source(apps.foreground, limit)),
            ask(Source(frames_of_last, limit)),
            return_exceptions=True,
        )
        if isinstance(ping, BaseException):
            raise ping
        if isinstance(front, BaseException):
            raise front
        if isinstance(frames, BaseException):
            raise frames
        self._polled_package = front.package
        if front.package != asked:
            frames = None  # another app's count
        # Every event the service received before it answered the ping is in.
        try:
            while (event := events.get_nowait()) is not None:
                newest = self._newest_event_ms
                self._newest_event_ms = (
                    event.time_ms if newest is None else max(newest, event.time_ms)
                )
        except ChannelError as exc:
            self._events = None
            raise EventsEnded(f"the service's event stream ended: {exc}") from None
        newest = self._newest_event_ms
        return ScreenPoll(
            since_event_ms=None if newest is None else ping.uptime_ms - newest,
            package=front.package,
            window=front.window,
            frames=frames,
        )

    async def _screenshot(self) -> FragmentBase:
        screen = self.bridge.screen
        shot = await screen.capture()
        if shot.width and shot.height:
            self._screen_size = (shot.width, shot.height)
        return AndroidScreenshotObs(
            image_bytes=shot.png,
            image_width=shot.width,
            image_height=shot.height,
            viewport_width=shot.width,
            viewport_height=shot.height,
            producer=shot.producer,
            capture_ms=shot.capture_ms,
            encode_ms=shot.encode_ms,
            waited_ms=shot.waited_ms,
        )

    async def _read_tree(self) -> TreeCapture:
        side = self._side
        if side is None:
            raise RuntimeError("the Android device is not set up")
        return await self.ui_tree_source.capture(side)

    def _ui_tree_fragment(self, capture: TreeCapture, screen: ScreenSize) -> AndroidUITreeObs:
        """Clean *capture* on a screen of *screen* pixels and print it; keep its tree for the element
        indexes of the next action."""
        tree = self.ui_tree_cleaner.clean(capture.forest, screen)
        self._ui_tree, self._actions_since_tree = tree, 0
        return AndroidUITreeObs(
            producer=self.ui_tree_source.source_id,
            forest=tree.forest,
            elements=tree.elements,
            serialized=self.ui_tree_serializer.serialize(tree),
            node_count=capture.node_count,
            omitted_offscreen=capture.omitted_offscreen,
            capture_ms=capture.capture_ms,
        )

    async def _foreground_app(self) -> FragmentBase:
        front = await self.bridge.apps.foreground()
        return AndroidForegroundAppObs(
            package=front.package, activity=front.activity, window=front.window
        )

    async def _platform(self) -> FragmentBase:
        side, apk = self._side, self._apk
        if side is None or apk is None:
            raise RuntimeError("the Android device is not set up")
        hello = side.helper.hello
        return AndroidPlatformObs(
            fingerprint=self._fingerprint,
            sdk_int=hello.sdk_int,
            input_method=await self.bridge.facts.input_method(),
            faketouch=side.faketouch,
            gestures=side.gestures,
            service_apk_sha256=hello.apk_sha256,
            service_apk_version=f"{hello.version_name} ({hello.version_code})",
            accessibility_tool=side.service.hello.accessibility_tool
            if side.service_connected
            else None,
            service_apk_signing_certificate_sha256=apk.info.signing_certificate_sha256,
        )


def _check_build(side: DeviceSide, apk: ServiceApk, typing_backend: AndroidTypingBackend) -> None:
    """Refuse a device that runs another build than the configured one, or cannot take the typing backend.

    The typing backend's check is the backstop for a provider that could not
    say the device's API level before it was up (``check_setup``).
    """
    hello = side.service.hello
    if hello.apk_sha256 != apk.info.sha256:
        raise DeviceSideError(
            f"the service on the device runs the APK with SHA-256 {hello.apk_sha256}, and the "
            f"run installed {apk.path} with SHA-256 {apk.info.sha256}"
        )
    _check_typing(typing_backend, hello.sdk_int, "the device")


def _check_archivers(
    archivers: Sequence[AndroidArchiverBase], negotiated: frozenset[str] | None
) -> None:
    """Refuse an archiver that needs the UI tree in a run whose negotiation leaves the tree out.

    Such a run never captures a tree (an agent that reads only the screen
    negotiates none), so the archiver would record nothing. *negotiated* is
    ``None`` before negotiation narrowed anything: every fragment is captured.
    """
    if negotiated is None or UI_TREE in negotiated:
        return
    unfed = [archiver.archiver_id for archiver in archivers if "ui_tree" in archiver.needs]
    if unfed:
        raise UnmetArchiverNeeds(
            f"This run captures no UI tree: its negotiation leaves {UI_TREE!r} out (an agent "
            f"that reads only the screen negotiates none), so {', '.join(map(repr, unfed))}, "
            "which records the tree of each capture, would record nothing. Leave it out of "
            "trace_writer.archive, or run an agent that reads the UI tree."
        )


def _check_typing(typing_backend: AndroidTypingBackend, level: int, device: str) -> None:
    """Refuse *typing_backend* on *device* of API *level* when it cannot enter text there."""
    problem = typing_backend.unsupported(level)
    if problem is not None:
        raise TypingBackendUnsupported(
            f"typing_backend {typing_backend.typing_id!r} cannot enter text on {device}: {problem}"
        )


def _numbers(numbers: Iterable[int]) -> str:
    """Increasing *numbers* as runs, for a message: ``0-12, 14, 16-20``; ``none`` when there are none."""
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs) or "none"


async def _fastest_screen(side: DeviceSide) -> ScreenshotProducer:
    """The helper's screenshot where the helper takes one (API 31-33), else the service's.

    Only the helper's ``unsupported`` picks the service: any other failure of
    the probe (``screenshot_failed``) is the device's, and fails setup rather
    than moving the whole run to the slower program.
    """
    try:
        await side.helper.screenshot()
    except RemoteError as exc:
        if exc.code != "unsupported":
            raise
        logger.info("screenshots come from the accessibility service: the helper said %s", exc)
        return "accessibility_service"
    return "shell_helper"
