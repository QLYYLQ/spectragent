"""BrowserCapability's archive half, against the fake browser (§4.9, §4.14).

The archiving contract's clauses that hang on the capability's own timing —
a collection never blocks for long, the hand-over is atomic, an exchange in
flight at a collection is handed over exactly once, later — plus the
cooperation rows that binding archivers decides (each line names both
parties; everything is reported at once; nothing is changed or launched on
a refusal), and how the capability wires the archive side.

The shared, capability-agnostic clauses run in
``tests/test_core/test_archive/test_archive_contract.py`` (``-k browser``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser import capability as capability_module
from spectragent.core.capabilities.browser.archive import (
    DomRawArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.backends.browser_defaults import DEFAULTS_WORLD
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.profiles import BUILTIN_PROFILES, BrowserProfileConfig
from spectragent.core.capabilities.browser.recorder import RecorderLostError
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.schemas.action import Action
from tests._helpers.browser_archive import OwnedBrowser, archiving_capability, settle
from tests._helpers.fake_cdp import FakeBrowser

pytestmark = pytest.mark.usefixtures("archive_fakes")


def _index(batch: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for item in batch.files:
        if item.name == "index.jsonl":
            lines.extend(json.loads(line) for line in item.data.decode().splitlines())
    return lines


# ── cooperation: what binding decides ─────────────────────────────────


class TestCooperation:
    def test_require_replayable_vs_off(self) -> None:
        cap = BrowserCapability()
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([NetworkArchiver()])
        (problem,) = info.value.problems
        assert problem.startswith(
            "archiver browser.network require_replayable=true ↔ replayability: none"
        )
        assert cap.bound_archivers == (), "refused before anything changed"
        cap.replayability = PinnedReplayability()
        cap.bind_archivers([NetworkArchiver()])
        cap.bind_archivers([])
        cap.replayability = PinnedReplayability()
        cap.bind_archivers([NetworkArchiver(require_replayable=False)])

    @pytest.mark.parametrize(
        "provider_id", ["lexmount", "browser_use", "hyperbrowser", "agentbay", "airtop", "skyvern"]
    )
    def test_archiver_on_cloud_provider(self, provider_id: str) -> None:
        from spectragent.core.capabilities.browser.backends._registrar import (
            BrowserProviderRegistrar,
        )
        from tests.test_core.test_capabilities.test_browser_backend_capabilities import (
            _shipped_providers,
        )

        _shipped_providers()
        cap = BrowserCapability()
        cap.backend = BrowserProviderRegistrar.get(provider_id)()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([StorageArchiver(), DomRawArchiver()])
        assert info.value.problems == [
            f"archiver {key} ↔ backend provider_id={provider_id!r}: the provider does not "
            f"declare NETWORK_CAPTURE (it does not launch a browser this capability owns), "
            f"and the recorder connection needs one"
            for key in ("browser.storage", "browser.dom_raw")
        ]

    def test_session_state_needs_allow(self) -> None:
        cap = BrowserCapability()
        cap.backend = OwnedBrowser(profile="user_data_dir='/home/me/profile'")  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers(
                [StorageArchiver(), DomRawArchiver(allow_session_state=True), StorageArchiver()]
            )
        assert (
            info.value.problems
            == [
                "archiver browser.storage allow_session_state=false ↔ backend "
                "provider_id='fake_local' user_data_dir='/home/me/profile': the browser does not "
                "start from a fresh profile, so the evidence may carry logged-in session state"
            ]
            * 2
        )
        cap.bind_archivers([StorageArchiver(allow_session_state=True)])

    def test_an_owned_browser_that_cannot_report_its_profile_is_refused(self) -> None:
        """Fail closed: profile freshness is part of what NETWORK_CAPTURE promises (§4.9.x)."""

        class _Mute(OwnedBrowser):
            persistent_profile = None  # type: ignore[assignment]  # declares, cannot answer

        cap = BrowserCapability()
        cap.backend = _Mute()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([StorageArchiver(allow_session_state=True)])
        (problem,) = info.value.problems
        assert problem.startswith(
            "archiver browser.storage ↔ backend provider_id='fake_local': the provider "
            "declares NETWORK_CAPTURE but does not implement NetworkCaptureProvider"
        )
        assert cap.bound_archivers == ()

    def test_cdp_url_or_injected_client_refused(self) -> None:
        cap = BrowserCapability(cdp_url="ws://elsewhere:9222/devtools/browser/x")
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([StorageArchiver()])
        assert info.value.problems[0].startswith("archiver browser.storage ↔ cdp_url:")
        injected = BrowserCapability()
        injected.backend = OwnedBrowser()  # type: ignore[assignment]
        injected._inject_cdp_client(object())  # type: ignore[arg-type]
        with pytest.raises(BrowserCooperationError, match="injected control client"):
            injected.bind_archivers([StorageArchiver()])

    async def test_a_task_cdp_url_is_refused_at_setup_and_nothing_launches(self) -> None:
        cap, driver = archiving_capability(archivers=(StorageArchiver(),))
        backend = cap.backend
        cap.bind_archivers([StorageArchiver()])
        with pytest.raises(BrowserCooperationError, match="archiver browser.storage ↔ cdp_url"):
            await cap.setup(cdp_url="ws://task/devtools/browser/x")
        assert backend.open_calls == []  # type: ignore[attr-defined]
        assert driver.browser.clients == []

    def test_profile_excluding_archive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            BUILTIN_PROFILES, "no_archive", BrowserProfileConfig(watchdogs_exclude=["archive"])
        )
        cap = BrowserCapability(profile="no_archive")
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([StorageArchiver()])
        assert info.value.problems == [
            "archiver browser.storage ↔ profile 'no_archive' watchdogs_exclude 'archive': "
            "nothing would drain the archive scope"
        ]

    def test_every_problem_at_once(self) -> None:
        cap = BrowserCapability(cdp_url="ws://x/devtools/browser/y")
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        with pytest.raises(BrowserCooperationError) as info:
            cap.bind_archivers([NetworkArchiver(), StorageArchiver()])
        assert len(info.value.problems) == 3
        assert "3 problem(s)" in str(info.value)

    def test_only_browser_archivers_bind(self) -> None:
        from tests._helpers.archive_fakes import FakeStepArchiver

        cap = BrowserCapability()
        with pytest.raises(TypeError, match="browser archivers"):
            cap.bind_archivers([FakeStepArchiver()])

    async def test_binding_after_a_setup_without_archivers_is_refused(self) -> None:
        cap, driver = archiving_capability(archivers=(StorageArchiver(),))
        await driver.ensure_setup()  # binds, sets up, restores the (empty) binding
        try:
            cap.bind_archivers([])
            assert cap._archive is not None, "set up with archivers: rebinding is fine"
        finally:
            await cap.teardown()
        plain = BrowserCapability()
        plain.backend = OwnedBrowser()  # type: ignore[assignment]
        plain._inject_cdp_factory(FakeBrowser().client)
        await plain.setup()
        try:
            with pytest.raises(RuntimeError, match="Bind before setup"):
                plain.bind_archivers([StorageArchiver()])
        finally:
            await plain.teardown()


# ── the timing clauses of the archiving contract (critic 36) ──────────


async def _network_capability(**knobs: Any) -> tuple[Any, Any]:
    cap, driver = archiving_capability(
        archivers=(NetworkArchiver(require_replayable=False, **knobs),)
    )
    await driver.start()
    return cap, driver


class TestTiming:
    async def test_collect_is_bounded(self) -> None:
        cap, driver = await _network_capability(drain_timeout_s=0.2, body_read_timeout_s=30)
        try:
            driver.browser.hold("Fetch.getResponseBody")
            task = asyncio.ensure_future(driver.exchange())
            await settle()
            started = time.monotonic()
            batch = await cap.collect_archive()
            elapsed = time.monotonic() - started
            assert elapsed < 0.2 + 1.0, f"a collection waited {elapsed:.2f}s on a held body read"
            assert [line["kind"] for line in _index(batch)] == ["pending"]
            assert _index(batch)[0]["waiting_for"] == "body"
            task.cancel()
        finally:
            await cap.teardown()

    async def test_inflight_exchange_once_in_next_batch(self) -> None:
        cap, driver = await _network_capability(drain_timeout_s=0.05)
        try:
            release = driver.browser.hold("Fetch.getResponseBody")
            task = asyncio.ensure_future(driver.exchange())
            await settle()
            first = _index(await cap.collect_archive())
            assert [(line["kind"], line["waiting_for"]) for line in first] == [("pending", "body")]
            release.set()
            await task
            second = _index(await cap.collect_archive())
            assert [line["kind"] for line in second] == ["exchange"]
            assert second[0]["record_id"] == first[0]["record_id"]
            assert second[0]["body_status"] == "ok"
            third = _index(await cap.collect_archive())
            assert third == [], "an exchange is handed over exactly once"
        finally:
            await cap.teardown()

    async def test_swap_is_atomic(self) -> None:
        """What settles while a collection waits lands in exactly one batch."""
        cap, driver = await _network_capability(drain_timeout_s=0.3)
        try:
            release = driver.browser.hold("Fetch.getResponseBody", session_id=driver.page_session())
            held = asyncio.ensure_future(driver.exchange(url="http://shop.test/held"))
            await settle()
            collecting = asyncio.ensure_future(cap.collect_archive())
            await asyncio.sleep(0.05)  # the collection is draining now
            release.set()
            await held
            others = [await driver.exchange() for _ in range(3)]
            first = _index(await collecting)
            second = _index(await cap.collect_archive())
            third = _index(await cap.collect_archive())
            exchanges = [
                line["network_id"] for line in first + second + third if line["kind"] == "exchange"
            ]
            assert sorted(exchanges) == sorted(["net-1", *others])
            assert len(set(exchanges)) == len(exchanges)
        finally:
            await cap.teardown()


# ── how the capability wires its archive side ─────────────────────────


class TestBrowserDefaults:
    """What a frame no override reaches falls back to, measured right after launch (§4.14)."""

    @staticmethod
    def _run_record(batch: Any) -> dict[str, Any]:
        (record,) = [item for item in batch.files if item.scope == "run"]
        return json.loads(record.data)

    async def test_measured_before_the_recorder_prepares_anything_and_recorded(self) -> None:
        browser = FakeBrowser()
        browser.default_timezone, browser.default_locale = "Asia/Tokyo", "zh-CN"
        # A fake host whose OS says what this browser falls back to.
        backend = OwnedBrowser()
        backend.LOCALE = "zh-CN"  # type: ignore[misc]
        cap, driver = archiving_capability(
            archivers=(StorageArchiver(),), browser=browser, backend=backend
        )
        await driver.start()
        try:
            sent = [(s.method, s.session_id, s.params) for s in browser.recorder.sent]
            measured_at = next(
                i
                for i, (method, _sid, params) in enumerate(sent)
                if method == "Page.createIsolatedWorld" and params["worldName"] == DEFAULTS_WORLD
            )
            auto_attach_at = sent.index(
                (
                    "Target.setAutoAttach",
                    None,
                    {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                )
            )
            assert measured_at < auto_attach_at, "measured before any override was sent"
            document = self._run_record(await cap.collect_archive())
            assert document["browser_defaults"] == {"timezone": "Asia/Tokyo", "locale": "zh-CN"}
        finally:
            await cap.teardown()

    async def test_nothing_is_measured_without_an_archiver(self) -> None:
        browser = FakeBrowser()
        cap = BrowserCapability()
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        cap.replayability = PinnedReplayability()
        cap._inject_cdp_factory(browser.client)
        await cap.setup()
        try:
            assert cap._recorder is not None, "shim-only: the recorder runs"
            # The client hints are read (the tier overrides the user agent); the defaults are not.
            assert browser.defaults_contexts == set()
            assert browser.client_hints_contexts
            assert cap._browser_defaults is None
        finally:
            await cap.teardown()

    async def test_a_default_chrome_contradicts_is_logged_and_chrome_s_is_recorded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The check before launch read the OS; Chrome's own answer says otherwise."""
        browser = FakeBrowser()
        browser.default_locale = "fr-FR"  # the fake host's OS says en-US (OwnedBrowser.LOCALE)
        cap, driver = archiving_capability(archivers=(StorageArchiver(),), browser=browser)
        with caplog.at_level(logging.WARNING, logger=capability_module.__name__):
            await driver.start()
        try:
            warned = [
                r.getMessage() for r in caplog.records if "read before launch" in r.getMessage()
            ]
            assert len(warned) == 1 and "'en-US' (read before launch) vs 'fr-FR'" in warned[0]
            document = self._run_record(await cap.collect_archive())
            assert document["browser_defaults"]["locale"] == "fr-FR"
        finally:
            await cap.teardown()

    async def test_a_failed_measurement_records_no_defaults(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        browser = FakeBrowser()
        browser.targets.clear()  # no page to measure in (setup then creates the first tab)
        cap, driver = archiving_capability(archivers=(StorageArchiver(),), browser=browser)
        with caplog.at_level(logging.WARNING, logger=capability_module.__name__):
            await driver.start()
        try:
            assert any("could not be measured" in r.getMessage() for r in caplog.records)
            assert cap._browser_defaults is None
            document = self._run_record(await cap.collect_archive())
            assert document["browser_defaults"] is None
        finally:
            await cap.teardown()


class TestWiring:
    def test_provides_the_four_archivers(self) -> None:
        cap = BrowserCapability()
        assert cap.provided_archivers == frozenset(
            {"browser.network", "browser.storage", "browser.dom_raw", "browser.replay_log"}
        )

    async def test_archivers_bound_open_the_archive_scope_and_its_watchdog(self) -> None:
        cap, driver = archiving_capability(archivers=(StorageArchiver(),))
        await driver.start()
        try:
            assert cap._scope_group is not None
            assert "__archive__" in cap._scope_group.scope_ids
            assert [type(w).__name__ for w in cap._shared_watchdogs].count("ArchiveWatchdog") == 1
            recorder = cap._recorder
            assert recorder is not None and recorder.channel is not None
            assert recorder.channel.handled == recorder.channel.emitted, (
                "the watchdog consumed the archive scope from the recorder's first event"
            )
            # storage needs no raw DOM: the DOM resolver keeps its no-op sink.
            ctx = cap._watchdog_context(cap.default_session_id)
            assert ctx.raw_capture_sink.__name__ == "_discard_raw_capture"
        finally:
            await cap.teardown()

    async def test_dom_raw_bound_gives_the_resolver_a_real_sink(self) -> None:
        cap, driver = archiving_capability(archivers=(DomRawArchiver(),))
        await driver.start()
        try:
            ctx = cap._watchdog_context(cap.default_session_id)
            assert ctx.raw_capture_sink.__name__ == "sink"
        finally:
            await cap.teardown()

    async def test_the_control_connection_sends_the_same_commands_with_archiving_on_and_off(
        self,
    ) -> None:
        async def control_methods(archivers: tuple[Any, ...]) -> list[str]:
            cap, driver = archiving_capability(archivers=archivers)
            if archivers:
                await driver.start()
            else:
                await driver.ensure_setup()
            try:
                await driver.observe()
                await cap.execute_action(Action(name="browser.wait", params={"seconds": 0}))
                return [s.method for s in driver.browser.control.sent]
            finally:
                await cap.teardown()

        on = await control_methods(
            (NetworkArchiver(require_replayable=False), StorageArchiver(), DomRawArchiver())
        )
        off = await control_methods(())
        assert on == off

    async def test_a_lost_recorder_fails_the_collection_naming_it(self) -> None:
        cap, driver = archiving_capability(archivers=(StorageArchiver(),))
        await driver.start()
        try:
            driver.break_collect()
            with pytest.raises(RecorderLostError, match="recorder connection lost"):
                await cap.collect_archive()
        finally:
            await cap.teardown()

    async def test_teardown_closes_the_sessions_and_forgets_the_run(self) -> None:
        cap, driver = archiving_capability()
        await driver.start()
        archive = cap._archive
        assert archive is not None
        await cap.teardown()
        assert cap._archive is None
        assert (await cap.collect_archive()).is_empty
        assert archive.sessions == ()

    async def test_never_set_up_or_nothing_bound_is_an_empty_batch(self) -> None:
        cap = BrowserCapability()
        assert (await cap.collect_archive()).is_empty
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        cap.bind_archivers([StorageArchiver()])
        assert (await cap.collect_archive()).is_empty


# ── the archive scope carries metadata only (§4.14, §5.6) ─────────────

_SECRET = "planted-secret-Q7"
_FRAME = "frame-payload-W3"


class TestNoBytesInTheArchiveScope:
    async def test_bodies_payloads_and_credentials_never_enter_the_scope(self) -> None:
        """The scope's history keeps its last 1000 events: what is in them must be small and safe.

        One POST with a body (both shapes Chrome sends), credential headers,
        ExtraInfo cookie lists and a WebSocket frame go through the recorder's
        real dispatch. Nothing of the body, the frame or a credential is in
        any event the archive scope saw — and the archive still records the
        redacted body and the frame (§4.14 privacy residual), from the side store.
        """
        import base64
        import gzip

        cap, driver = archiving_capability(
            archivers=(NetworkArchiver(require_replayable=False, drain_timeout_s=0.2),)
        )
        await driver.start()
        try:
            session = driver.page_session()
            fire = driver.recorder.fire
            body = f"user=alice&password={_SECRET}&note=hello"
            fire(
                "Network.requestWillBeSent",
                {
                    "requestId": "post-1",
                    "loaderId": "loader-1",
                    "type": "XHR",
                    "frameId": "frame-1",
                    "timestamp": 1.0,
                    "wallTime": 1_700_000_000.0,
                    "request": {
                        "url": "http://shop.test/login",
                        "method": "POST",
                        "headers": {
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Authorization": f"Bearer {_SECRET}",
                        },
                        "hasPostData": True,
                        "postData": body,
                        "postDataEntries": [{"bytes": base64.b64encode(body.encode()).decode()}],
                    },
                },
                session,
            )
            fire(
                "Network.requestWillBeSentExtraInfo",
                {
                    "requestId": "post-1",
                    "headers": {"Cookie": f"sid={_SECRET}", "Host": "shop.test"},
                    "associatedCookies": [{"cookie": {"name": "sid", "value": _SECRET}}],
                },
                session,
            )
            fire(
                "Network.responseReceived",
                {
                    "requestId": "post-1",
                    "type": "XHR",
                    "hasExtraInfo": False,
                    "response": {
                        "url": "http://shop.test/login",
                        "status": 204,
                        "statusText": "No Content",
                        "headers": {},
                        "requestHeaders": {"Cookie": f"sid={_SECRET}"},
                    },
                },
                session,
            )
            fire(
                "Network.responseReceivedExtraInfo",
                {
                    "requestId": "post-1",
                    "statusCode": 204,
                    "headers": {},
                    "blockedCookies": [{"cookie": {"name": "b", "value": _SECRET}}],
                    "exemptedCookies": [{"cookie": {"name": "e", "value": _SECRET}}],
                },
                session,
            )
            fire("Network.loadingFinished", {"requestId": "post-1", "timestamp": 2.0}, session)
            fire(
                "Network.webSocketCreated",
                {"requestId": "ws-1", "url": "ws://shop.test/echo"},
                session,
            )
            fire(
                "Network.webSocketFrameReceived",
                {
                    "requestId": "ws-1",
                    "timestamp": 3.0,
                    "response": {"opcode": 1, "mask": False, "payloadData": _FRAME},
                },
                session,
            )
            batch = await cap.collect_archive()

            recorder = cap._recorder
            assert recorder is not None and recorder.channel is not None
            from spectragent.core.capabilities.browser.recorder import RecorderEvent

            travelled = json.dumps(
                [
                    (e.method, e.params)
                    for e in recorder.channel.scope.event_history
                    if isinstance(e, RecorderEvent)
                ]
            )
            assert "Network.requestWillBeSent" in travelled, "the events did travel"
            for absent in (_SECRET, _FRAME, "postData", "payloadData", "Cookies"):
                assert absent not in travelled, f"{absent!r} entered the archive scope"

            on_disk = b"".join(
                gzip.decompress(item.data) if item.data[:2] == b"\x1f\x8b" else item.data
                for item in batch.files
            )
            assert _SECRET.encode() not in on_disk
            (line,) = [
                x for x in _index(batch) if x.get("kind") == "exchange" and x["method"] == "POST"
            ]
            assert line["request"]["post"]["source"] == "requestWillBeSent.postDataEntries"
            assert b"user=alice&password=<redacted>&note=hello" in on_disk
            stripped = {(e["location"], e["field"]) for e in line["redaction"]["entries"]}
            assert {
                ("/request/headers", "Authorization"),
                ("/request/raw_headers", "Cookie"),
            } <= stripped
            assert _FRAME.encode() in on_disk, "the frame is still recorded, from the side store"
        finally:
            await cap.teardown()
