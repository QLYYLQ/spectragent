"""Unit tests for BrowserCapability — static contract + lifecycle.

These tests do not require a real CDP connection. Tests that exercise
the cache and lazy-batch resolution live in ``test_browser_lazy_batch.py``.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities.base import BaseCapability
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.schemas.action import ActionDescriptor


class TestStaticContract:
    def test_satisfies_base_capability_protocol(self) -> None:
        cap = BrowserCapability()
        assert isinstance(cap, BaseCapability)

    def test_capability_id(self) -> None:
        cap = BrowserCapability()
        assert cap.capability_id == "browser"

    def test_provided_fragments(self) -> None:
        cap = BrowserCapability()
        assert cap.provided_fragments == frozenset(
            {"browser.dom", "browser.screenshot", "browser.browser_meta"}
        )

    def test_wiring_observation(self) -> None:
        # Wiring declares the three fragments under the "observation" registry
        wiring = BrowserCapability.__wiring__
        assert "observation" in wiring
        registry, frags = wiring["observation"]
        assert registry == "observation"
        assert set(frags) == {
            "browser.dom",
            "browser.screenshot",
            "browser.browser_meta",
        }

    def test_wiring_backend(self) -> None:
        # Wiring declares browser_provider as the backend registry
        wiring = BrowserCapability.__wiring__
        assert wiring.get("backend") == "browser_provider"


class TestActionDescriptors:
    def test_returns_list_of_action_descriptors(self) -> None:
        cap = BrowserCapability()
        descriptors = cap.get_action_descriptors()
        assert isinstance(descriptors, list)
        assert all(isinstance(d, ActionDescriptor) for d in descriptors)
        assert len(descriptors) > 0

    def test_includes_core_actions(self) -> None:
        cap = BrowserCapability()
        names = {d.name for d in cap.get_action_descriptors()}
        # The agent must have at least these actions
        assert {
            "browser.click",
            "browser.type",
            "browser.scroll",
            "browser.navigate",
            "browser.done",
        } <= names

    def test_done_action_has_extracted_content_param(self) -> None:
        cap = BrowserCapability()
        done = next(
            d for d in cap.get_action_descriptors() if d.name == "browser.done"
        )
        assert "extracted_content" in done.params

    def test_descriptors_are_a_copy(self) -> None:
        # Mutating the returned list must not affect future calls.
        cap = BrowserCapability()
        first = cap.get_action_descriptors()
        first.clear()
        second = cap.get_action_descriptors()
        assert len(second) > 0


class TestPropertiesBeforeSetup:
    def test_is_alive_false_before_setup(self) -> None:
        cap = BrowserCapability()
        assert cap.is_alive is False

    def test_default_session_id_raises_before_setup(self) -> None:
        cap = BrowserCapability()
        try:
            _ = cap.default_session_id
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError before setup()")

    def test_cdp_property_raises_before_setup(self) -> None:
        cap = BrowserCapability()
        try:
            _ = cap.cdp
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError before setup()")


class TestInitialization:
    def test_default_config(self) -> None:
        cap = BrowserCapability()
        assert cap.profile == "standard"
        assert cap.viewport_width == 1280
        assert cap.viewport_height == 720
        assert cap.cdp_url is None

    def test_custom_config(self) -> None:
        cap = BrowserCapability(
            cdp_url="ws://localhost:9222/devtools/browser/abc",
            profile="standard",
            viewport_width=1920,
            viewport_height=1080,
        )
        assert cap.cdp_url == "ws://localhost:9222/devtools/browser/abc"
        assert cap.viewport_width == 1920
        assert cap.viewport_height == 1080

    def test_internal_state_starts_empty(self) -> None:
        cap = BrowserCapability()
        assert cap._scopes == {}
        assert cap._selector_maps == {}
        assert cap._cycle_cache == {}
        assert cap._default_session_id is None


# ─── M13-Rev additions ────────────────────────────────────────────────


class TestActionWiringAndProvidedActions:
    """``__wiring__["action"]`` feeds the symmetric ``provided_actions``."""

    def test_provided_actions_covers_full_default_set(self) -> None:
        cap = BrowserCapability()
        assert cap.provided_actions == frozenset(
            {
                "browser.click",
                "browser.click_coordinate",
                "browser.type",
                "browser.navigate",
                "browser.screenshot",
                "browser.scroll",
                "browser.send_keys",
                "browser.go_back",
                "browser.go_forward",
                "browser.switch_tab",
                "browser.select_dropdown",
                "browser.extract_content",
                "browser.wait",
                "browser.done",
            }
        )

    def test_wiring_action_tuple_matches_provided(self) -> None:
        wiring = BrowserCapability.__wiring__
        registry, provides = wiring["action"]
        assert registry == "action"
        assert set(provides) == BrowserCapability().provided_actions


class TestDefaultActionInstantiation:
    """``__post_init__`` materialises actions from wiring + overrides."""

    def test_default_actions_instantiated_when_no_overrides(self) -> None:
        cap = BrowserCapability()
        assert len(cap._actions_by_name) == 14
        assert set(cap._actions_by_name) == cap.provided_actions

    def test_action_overrides_apply_kwargs(self) -> None:
        cap = BrowserCapability(
            action_overrides={
                "browser.scroll": {"default_amount": 777, "max_amount": 9999}
            }
        )
        scroll = cap._actions_by_name["browser.scroll"]
        assert scroll.default_amount == 777  # type: ignore[attr-defined]
        assert scroll.max_amount == 9999  # type: ignore[attr-defined]
        # All 14 actions still materialised — overrides are additive.
        assert len(cap._actions_by_name) == 14

    def test_unknown_override_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="not in __wiring__"):
            BrowserCapability(action_overrides={"rogue_action": {"x": 1}})

    def test_provided_actions_matches_dispatch_map(self) -> None:
        """Core invariant: provided_actions == _actions_by_name.keys()."""
        cap = BrowserCapability()
        assert cap.provided_actions == frozenset(cap._actions_by_name)

    def test_provided_actions_matches_with_overrides(self) -> None:
        cap = BrowserCapability(
            action_overrides={"browser.navigate": {"timeout_ms": 5000}}
        )
        assert cap.provided_actions == frozenset(cap._actions_by_name)

    def test_get_action_descriptors_derives_from_dispatch_map(self) -> None:
        cap = BrowserCapability()
        names = {d.name for d in cap.get_action_descriptors()}
        assert names == set(cap._actions_by_name)


class TestExecuteActionDispatch:
    """``execute_action`` routes through the registered action instance."""

    def test_unknown_action_returns_error(self) -> None:
        cap = BrowserCapability()
        cap._default_session_id = "sid_x"
        cap._scopes["sid_x"] = object()  # type: ignore[assignment]
        cap._selector_maps["sid_x"] = {}

        import asyncio

        from spectragent.schemas.action import Action

        result = asyncio.run(
            cap.execute_action(
                Action(name="does_not_exist"), session_id="sid_x",
            )
        )
        assert result.success is False
        assert "Unknown action" in (result.error or "")

    def test_done_flows_through_capability(self) -> None:
        cap = BrowserCapability()
        cap._default_session_id = "sid_x"
        cap._scopes["sid_x"] = object()  # type: ignore[assignment]
        cap._selector_maps["sid_x"] = {}

        import asyncio

        from spectragent.schemas.action import Action

        result = asyncio.run(
            cap.execute_action(
                Action(name="browser.done", params={"extracted_content": "answer42"}),
                session_id="sid_x",
            )
        )
        assert result.is_done is True
        assert result.extracted_content == "answer42"


# ─── Backend IoC closure ──────────────────────────────────────────────


class _FakeProvider:
    """Minimal stand-in for a registered ``BrowserProviderBase`` instance.

    Records ``open()`` / ``close()`` calls so tests can assert IoC wiring
    actually drove the lifecycle. Returns a ``ProviderSession`` carrying
    a synthetic CDP URL — :class:`BrowserCapability.setup` is then
    expected to construct a CDPClient against it.
    """

    def __init__(self, cdp_url: str | None = "ws://fake-backend/devtools") -> None:
        self.provider_id = "fake_provider"
        self._cdp_url = cdp_url
        self.open_calls: list[dict] = []
        self.close_calls: list[object] = []

    async def open(self, **kwargs):
        from spectragent.schemas.provider_session import ProviderSession

        self.open_calls.append(kwargs)
        return ProviderSession(
            session_id="fake-session-1",
            provider_id=self.provider_id,
            cdp_url=self._cdp_url,
        )

    async def close(self, session=None):
        self.close_calls.append(session)


class TestBackendIoC:
    """ConfigComposer setattr-injects ``backend`` → setup() consumes it.

    These tests stub the CDPClient so we never touch a real WebSocket; the
    focus is the wiring contract between BrowserCapability, the wired
    provider, and the cdp_url resolution priority documented on
    :meth:`BrowserCapability.setup`.
    """

    @staticmethod
    def _patch_heavy_setup(monkeypatch) -> list[str]:
        """Stub the CDP / agent-cdp internals that need a live browser.

        Returns a list that will be appended to with each constructed
        CDP URL so tests can assert which URL drove the connection.
        """
        cdp_clients_constructed: list[str] = []

        class _StubCDPClient:
            def __init__(self, url: str) -> None:
                cdp_clients_constructed.append(url)

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            async def send(self, *args, **kwargs):  # noqa: ARG002
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "t0", "url": ""}
                    ]
                }

        class _StubScopeGroup:
            """Drop-in replacement for agent_cdp ``ScopeGroup``.

            Real ScopeGroup spawns an asyncio Task tied to the running
            event loop; replaying it across two ``asyncio.run()`` calls
            (setup + teardown) raises CancelledError. The stub just
            records calls so we can verify lifecycle behaviour without
            crossing event loops.
            """

            def __init__(self, _name: str) -> None:
                self.created: list[str] = []
                self.closed = False

            async def create_scope(self, scope_id, **_kwargs):
                self.created.append(scope_id)
                return object()

            async def close_all(self) -> None:
                self.closed = True

        monkeypatch.setattr(
            "spectragent.core.capabilities.browser.capability.CDPClient",
            _StubCDPClient,
        )
        monkeypatch.setattr(
            "spectragent.core.capabilities.browser.capability.ScopeGroup",
            _StubScopeGroup,
        )

        async def _fake_attach_tab(_self, _tid, _url=""):
            return "fake-cdp-session"

        monkeypatch.setattr(
            BrowserCapability, "_attach_tab", _fake_attach_tab, raising=True
        )
        monkeypatch.setattr(
            BrowserCapability,
            "_attach_browser_level_watchdogs",
            lambda _self, _scope: None,
            raising=True,
        )
        return cdp_clients_constructed

    def test_setup_consumes_backend_open(self, monkeypatch) -> None:
        """No kwarg + no field → ``backend.open()`` is called for cdp_url."""
        import asyncio

        cap = BrowserCapability()
        cap.backend = _FakeProvider(cdp_url="ws://from-backend/x")  # type: ignore[assignment]
        urls = self._patch_heavy_setup(monkeypatch)

        async def _drive():
            await cap.setup()
            assert len(cap.backend.open_calls) == 1, (  # type: ignore[union-attr]
                "Expected backend.open() to be invoked exactly once"
            )
            assert urls == ["ws://from-backend/x"]
            assert cap._session is not None
            assert cap._session.cdp_url == "ws://from-backend/x"

            await cap.teardown()
            assert len(cap.backend.close_calls) == 1  # type: ignore[union-attr]
            assert cap._session is None

        asyncio.run(_drive())

    def test_setup_priority_kwarg_over_backend(self, monkeypatch) -> None:
        """Explicit ``cdp_url`` kwarg wins → backend.open() is NOT called."""
        import asyncio

        cap = BrowserCapability()
        cap.backend = _FakeProvider()  # type: ignore[assignment]
        urls = self._patch_heavy_setup(monkeypatch)

        async def _drive():
            await cap.setup(cdp_url="ws://from-kwarg/y")
            assert cap.backend.open_calls == [], (  # type: ignore[union-attr]
                "kwarg cdp_url must short-circuit backend.open()"
            )
            assert urls == ["ws://from-kwarg/y"]
            assert cap._session is None
            await cap.teardown()
            # No backend session was opened, so close should NOT be called.
            assert cap.backend.close_calls == []  # type: ignore[union-attr]

        asyncio.run(_drive())

    def test_setup_raises_when_no_cdp_no_backend(self) -> None:
        """No kwarg, no field, no backend → explicit RuntimeError."""
        import asyncio

        import pytest

        cap = BrowserCapability()
        # backend defaults to None.
        with pytest.raises(RuntimeError, match="requires a CDP URL"):
            asyncio.run(cap.setup())


# ─── Page lifecycle wiring (M13.5) ────────────────────────────────────


class _RecordingCDPClient:
    """CDP client stub that records every send() call.

    Returns plausible result payloads for the methods _attach_tab uses
    (Target.attachToTarget / Page.enable / Page.setLifecycleEventsEnabled
    / Page.getFrameTree). Implements on_event/off_event so CDPEventBridge
    can register without crashing.
    """

    def __init__(self, frame_id: str = "MAIN_FRAME_ID_42") -> None:
        self.is_connected = True
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.event_callbacks: dict[str, list[Any]] = {}
        self._frame_id = frame_id

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Target.attachToTarget":
            return {"sessionId": "cdp-sid-1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": self._frame_id}}}
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.getTargets":
            return {"targetInfos": []}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        self.event_callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Any) -> None:
        cbs = self.event_callbacks.get(method, [])
        if callback in cbs:
            cbs.remove(callback)


class _RecordingScope:
    """Stand-in for agent_cdp.EventScope — captures connect() registrations."""

    def __init__(self, scope_id: str = "scope") -> None:
        self.scope_id = scope_id
        self.connections: list[tuple[type, Any]] = []
        self.metadata: dict[str, Any] = {}

    def connect(self, event_type: type, handler: Any, **_: Any) -> Any:
        self.connections.append((event_type, handler))
        class _Conn:
            def disconnect(self) -> None:
                pass
        return _Conn()

    def emit(self, _event: Any) -> None:
        pass


class _RecordingScopeGroup:
    def __init__(self, _name: str) -> None:
        self.scopes: list[_RecordingScope] = []
        self.closed = False

    async def create_scope(self, scope_id: str, **_: Any) -> _RecordingScope:
        scope = _RecordingScope(scope_id)
        self.scopes.append(scope)
        return scope

    async def close_scope(self, _scope_id: str) -> None:
        return None

    async def close_all(self) -> None:
        self.closed = True


class TestPageLifecycleWiring:
    """`_attach_tab` enables Page domain + builds CDP→scope bridge.

    The Page-domain wiring is what makes ``NavigateToUrlEvent.wait_until``
    actually mean something — without ``Page.enable`` /
    ``setLifecycleEventsEnabled``, ``loadEventFired`` and
    ``lifecycleEvent`` never fire.
    """

    @staticmethod
    def _make_cap_with_stub() -> tuple[
        BrowserCapability, _RecordingCDPClient, _RecordingScopeGroup,
    ]:
        cap = BrowserCapability()
        cdp = _RecordingCDPClient()
        scope_group = _RecordingScopeGroup("test")
        # Use the injected-client path so teardown() skips ``cdp.stop()``
        # — our stub doesn't speak the full CDPClient lifecycle.
        cap._inject_cdp_client(cdp)  # type: ignore[arg-type]
        cap._cdp = cdp  # type: ignore[assignment]
        cap._scope_group = scope_group  # type: ignore[assignment]
        # _create_watchdogs_for_scope reads ``_resolved_profile``.
        from spectragent.core.capabilities.browser.profiles import (
            resolve_profile,
        )
        cap._resolved_profile = resolve_profile("standard")

        # Skip real watchdog construction — the dom_cleaner/serializer
        # registries are not relevant to the lifecycle wiring under test
        # and require importing optional modules with heavy deps.
        cap._create_watchdogs_for_scope = (  # type: ignore[method-assign]
            lambda level="tab", cdp_session_id=None: []
        )
        return cap, cdp, scope_group

    def test_attach_tab_sends_page_enable_and_lifecycle(self) -> None:
        import asyncio

        cap, cdp, _ = self._make_cap_with_stub()

        async def _drive() -> None:
            sid = await cap._attach_tab("T1", "https://example.com")
            assert sid == "cdp-sid-1"

        asyncio.run(_drive())

        methods = [m for m, _, _ in cdp.sent]
        # Page.enable + setLifecycleEventsEnabled must precede
        # _create_watchdogs_for_scope (and any subsequent navigate).
        assert "Page.enable" in methods
        assert "Page.setLifecycleEventsEnabled" in methods
        assert "Page.getFrameTree" in methods
        # Each was sent against the new session id.
        for m, _params, sid in cdp.sent:
            if m in {
                "Page.enable",
                "Page.setLifecycleEventsEnabled",
                "Page.getFrameTree",
            }:
                assert sid == "cdp-sid-1"

    def test_attach_tab_records_main_frame_id(self) -> None:
        import asyncio

        cap, _, _ = self._make_cap_with_stub()
        asyncio.run(cap._attach_tab("T1"))
        assert cap._main_frame_ids["cdp-sid-1"] == "MAIN_FRAME_ID_42"

    def test_attach_tab_builds_bridge(self) -> None:
        import asyncio

        cap, cdp, _ = self._make_cap_with_stub()
        asyncio.run(cap._attach_tab("T1"))

        assert "cdp-sid-1" in cap._bridges
        # Bridge subscribed to all three Page-domain events.
        registered = set(cdp.event_callbacks)
        assert {
            "Page.loadEventFired",
            "Page.domContentEventFired",
            "Page.lifecycleEvent",
        } <= registered

    def test_close_tab_closes_bridge_first(self) -> None:
        import asyncio

        cap, cdp, _ = self._make_cap_with_stub()
        asyncio.run(cap._attach_tab("T1"))
        cap._default_session_id = "another-tab"
        asyncio.run(cap.close_tab("cdp-sid-1"))

        # Bridge entry removed and its CDP callbacks unregistered.
        assert "cdp-sid-1" not in cap._bridges
        assert "cdp-sid-1" not in cap._main_frame_ids
        # Each CDP method is either absent OR its callback list emptied.
        for method in (
            "Page.loadEventFired",
            "Page.domContentEventFired",
            "Page.lifecycleEvent",
        ):
            cbs = cdp.event_callbacks.get(method, [])
            assert cbs == [], (
                f"Expected {method} callbacks emptied after close_tab, "
                f"got {cbs}"
            )

    def test_teardown_closes_all_bridges(self) -> None:
        import asyncio

        cap, cdp, scope_group = self._make_cap_with_stub()
        asyncio.run(cap._attach_tab("T1"))
        cap._default_session_id = "cdp-sid-1"  # so teardown clears state

        asyncio.run(cap.teardown())

        assert cap._bridges == {}
        assert cap._main_frame_ids == {}
        assert scope_group.closed is True
        for method in (
            "Page.loadEventFired",
            "Page.domContentEventFired",
            "Page.lifecycleEvent",
        ):
            assert cdp.event_callbacks.get(method, []) == []
