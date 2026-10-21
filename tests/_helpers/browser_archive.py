"""A BrowserCapability recording against the fake browser — for the archive contract and tests.

``browser_archive_harness`` is the browser's entry in the archiving contract's
``HARNESS_FACTORIES`` (``tests/_helpers/archive_contract.py``): a real
:class:`BrowserCapability` with the four recording archivers (network,
storage, raw DOM, frames), set up against
:class:`~tests._helpers.fake_cdp.FakeBrowser` through ``_inject_cdp_factory``
(the transport only: ownership is still decided by a backend that declares
``NETWORK_CAPTURE``), so every clause runs in milliseconds with no Chrome.

:class:`BrowserDriver` is what ``produce`` does for one step of activity:

- one committed observation cycle with ``browser.dom`` negotiated — the DOM
  resolver's capture reaches ``browser.dom_raw`` through the raw-capture sink;
- one network exchange — ``requestWillBeSent``, a Response-stage
  ``Fetch.requestPaused`` whose body the network session reads, the response,
  its ExtraInfo and ``loadingFinished`` — fired on the recorder's client the
  way its receive loop would;
- a cookie and a localStorage item whose values change, so ``browser.storage``
  has a new state to record;
- one of the agent's actions (``browser.wait`` for no time) — its action
  boundary gives ``browser.frames`` a frame to capture before it, and
  supersedes the cycle's ``after_observation`` frame.

The setup is lazy (the first ``produce``) and opens every capture channel:
the contract binds and unbinds archivers after setup, which changes who
receives events and who is collected, never which channels exist (§4.14).
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.archive import (
    BrowserArchiverBase,
    DomRawArchiver,
    FramesArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.backends.capabilities import (
    CDP,
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
    NETWORK_ISOLATION,
)
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.provider_session import ProviderSession
from tests._helpers.fake_cdp import FakeBrowser, FakeClient


class OwnedBrowser:
    """A provider that launches a browser the capability owns (no real Chrome)."""

    __capabilities__: ClassVar[set[str]] = {CDP, NETWORK_CAPTURE, LAUNCH_ARGS}
    provider_id = "fake_local"
    #: The provider's own switches (what ``launch_flags`` records before ``launch_args``).
    BASE_FLAGS: ClassVar[tuple[str, ...]] = ("--headless=new", "--user-data-dir=<user-data-dir>")
    #: The locale this fake host's OS says Chrome falls back to (``FakeBrowser.default_locale``).
    LOCALE: ClassVar[str | None] = "en-US"

    def __init__(self, *, profile: str | None = None) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.closed = 0
        self.profile = profile

    async def open(self, **kwargs: Any) -> ProviderSession:
        self.open_calls.append(kwargs)
        flags = self.planned_launch_flags(kwargs.get("launch_args", ()), kwargs.get("launch_env"))
        return ProviderSession(
            session_id="s",
            provider_id=self.provider_id,
            cdp_url="ws://127.0.0.1:9222/devtools/browser/fake",
            metadata={"launch_flags": flags},
        )

    async def close(self, session: ProviderSession | None = None) -> None:
        self.closed += 1

    def launch_arg_conflicts(self, launch_args: Any) -> list[str]:
        return []

    def planned_launch_flags(self, launch_args: Any, launch_env: Any = None) -> list[str]:
        env = [f"{name}={value}" for name, value in sorted((launch_env or {}).items())]
        return [*env, *self.BASE_FLAGS, *launch_args]

    def planned_browser_defaults(self, launch_env: Any = None) -> BrowserDefaults:
        """``TZ`` when the launch sets it, else ``UTC``; :attr:`LOCALE` (as ``FakeBrowser`` answers)."""
        return BrowserDefaults(timezone=(launch_env or {}).get("TZ", "UTC"), locale=self.LOCALE)

    def persistent_profile(self) -> str | None:
        return self.profile


class IsolatingBrowser(OwnedBrowser):
    """An :class:`OwnedBrowser` that can also be cut off from the network (M30)."""

    __capabilities__: ClassVar[set[str]] = {CDP, NETWORK_CAPTURE, LAUNCH_ARGS, NETWORK_ISOLATION}
    provider_id = "fake_isolating"

    def __init__(self, *, conflicts: list[str] | None = None, profile: str | None = None) -> None:
        super().__init__(profile=profile)
        self.conflicts = list(conflicts or [])

    def isolation_conflicts(self) -> list[str]:
        return list(self.conflicts)


async def settle(rounds: int = 30) -> None:
    """Let spawned tasks and scope loops run."""
    for _ in range(rounds):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


class BrowserDriver:
    """Drives one capability's activity against the fake browser."""

    def __init__(
        self,
        capability: BrowserCapability,
        browser: FakeBrowser,
        archivers: tuple[BrowserArchiverBase, ...],
        *,
        fragments: frozenset[str] = frozenset({"browser.dom"}),
    ) -> None:
        self.capability = capability
        self.browser = browser
        self.archivers = archivers
        self.fragments = fragments
        self.cycle = 0
        self.requests = 0
        self._set_up = False

    @property
    def recorder(self) -> FakeClient:
        return self.browser.recorder

    def page_session(self) -> str:
        """The recorder's session for the initial page target."""
        recorder = self.capability._recorder
        assert recorder is not None, "not set up"
        session = recorder.session_for_target("page-1")
        assert session is not None
        return session

    async def ensure_setup(self) -> None:
        """Set up once, with every archiver bound, then restore the binding."""
        if self._set_up:
            return
        self._set_up = True
        bound = list(self.capability.bound_archivers)
        self.capability.bind_archivers(list(self.archivers))
        await self.capability.setup(negotiation=set(self.fragments))
        self.capability.bind_archivers(bound)

    async def start(self) -> None:
        """Bind every archiver and set up — the runtime's order, as a test wants it."""
        self.capability.bind_archivers(list(self.archivers))
        await self.ensure_setup()

    async def observe(self) -> None:
        """One committed observation cycle."""
        self.cycle += 1
        await self.capability.begin_observation(self.cycle)
        for fragment in sorted(self.fragments):
            await self.capability.resolve_fragment(fragment)

    async def act(self, name: str = "browser.wait", **params: Any) -> ActionResult:
        """One of the agent's actions on the focused tab (default: wait for no time)."""
        if name == "browser.wait":
            params.setdefault("seconds", 0)
        return await self.capability.execute_action(Action(name=name, params=params))

    async def exchange(
        self,
        *,
        url: str | None = None,
        body: bytes | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        finish: bool = True,
    ) -> str:
        """One request through the recorder's events and a Response-stage pause; its id."""
        self.requests += 1
        n = self.requests
        request_id = f"net-{n}"
        url = url or f"http://shop.test/item/{n}"
        session = self.page_session()
        interception = f"interception-{n}"
        self.browser.response_bodies[interception] = (
            body if body is not None else f"body of {url}".encode()
        )
        request = {
            "url": url,
            "method": "GET",
            "headers": {"Accept": "*/*", "Cookie": "sid=planted-cookie"},
        }
        response_headers = {"Content-Type": "text/plain", **(headers or {})}
        recorder = self.recorder
        recorder.fire(
            "Network.requestWillBeSent",
            {
                "requestId": request_id,
                "loaderId": "loader-1",
                "documentURL": "http://shop.test/",
                "request": request,
                "timestamp": 100.0 + n,
                "wallTime": 1_700_000_000.0 + n,
                "initiator": {"type": "script", "stack": {"callFrames": []}},
                "type": "XHR",
                "frameId": "frame-1",
            },
            session,
        )
        recorder.fire(
            "Fetch.requestPaused",
            {
                "requestId": interception,
                "request": request,
                "frameId": "frame-1",
                "resourceType": "XHR",
                "responseStatusCode": status,
                "responseHeaders": [{"name": k, "value": v} for k, v in response_headers.items()],
                "networkId": request_id,
            },
            session,
        )
        await settle()
        recorder.fire(
            "Network.responseReceived",
            {
                "requestId": request_id,
                "type": "XHR",
                "frameId": "frame-1",
                "hasExtraInfo": True,
                "response": {
                    "url": url,
                    "status": status,
                    "statusText": "OK",
                    "headers": response_headers,
                    "mimeType": "text/plain",
                    "protocol": "http/1.1",
                },
            },
            session,
        )
        recorder.fire(
            "Network.responseReceivedExtraInfo",
            {
                "requestId": request_id,
                "statusCode": status,
                "headers": {**response_headers, "Set-Cookie": f"seen={n}; Path=/"},
                "blockedCookies": [],
            },
            session,
        )
        if finish:
            recorder.fire(
                "Network.loadingFinished",
                {"requestId": request_id, "timestamp": 101.0 + n, "encodedDataLength": 10},
                session,
            )
        return request_id

    def change_storage(self) -> None:
        """New cookie and localStorage values: a state ``browser.storage`` has not seen."""
        n = self.requests + self.cycle
        self.browser.cookies = [
            {
                "name": "session",
                "value": f"cookie-value-{n}",
                "domain": "shop.test",
                "path": "/",
                "expires": -1,
                "size": 20,
                "httpOnly": True,
                "secure": False,
                "session": True,
            }
        ]
        self.browser.local_storage = {
            self.browser.origin: {"cart": f"{n} items", "auth_token": "planted-token"}
        }

    async def produce(self, step: int) -> None:
        """One step of activity, for whichever archivers are bound."""
        await self.ensure_setup()
        await self.observe()
        await self.act()
        await self.exchange()
        self.change_storage()
        await settle()

    def break_collect(self) -> None:
        """The recorder connection drops: every collection from now on raises."""
        self.recorder.lose(ConnectionError("recorder socket gone"))

    def change_run_record(self) -> None:
        """A new document whose page reports another user agent: a new fingerprint version.

        A new document, because the fingerprint measures each document once
        (the values are the document's).
        """
        self.browser.user_agent = f"{self.browser.user_agent}+1"
        self.browser.loader_id = f"{self.browser.loader_id}+1"

    async def close(self) -> None:
        await self.capability.teardown()


def archiving_capability(
    *,
    archivers: tuple[BrowserArchiverBase, ...] | None = None,
    browser: FakeBrowser | None = None,
    backend: OwnedBrowser | None = None,
    fragments: frozenset[str] = frozenset({"browser.dom"}),
) -> tuple[BrowserCapability, BrowserDriver]:
    """A capability on the fake browser with *archivers* (default: all four recording ones, none tier)."""
    browser = browser or FakeBrowser()
    capability = BrowserCapability()
    capability.backend = backend or OwnedBrowser()  # type: ignore[assignment]
    capability._inject_cdp_factory(browser.client)
    chosen = archivers or (
        NetworkArchiver(require_replayable=False, drain_timeout_s=0.2),
        StorageArchiver(),
        DomRawArchiver(),
        FramesArchiver(),
    )
    return capability, BrowserDriver(capability, browser, chosen, fragments=fragments)
