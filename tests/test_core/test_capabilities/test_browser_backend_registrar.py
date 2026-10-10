"""Tests for the browser_provider conscribe registrar.

BrowserCapability's ``__wiring__`` references the ``browser_provider``
registry. This test verifies the registry exists and at least the
critical providers (local_chrome) are registered. Other providers
(lexmount, browseruse_lite, ...) require optional cloud SDKs to import,
so we don't fail the test if they're missing.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.backends._registrar import (
    BrowserProviderBase,
    BrowserProviderRegistrar,
)


class TestRegistrar:
    def test_registrar_layer_name(self) -> None:
        # Hard-coded sanity check: registrar name must match what
        # BrowserCapability.__wiring__ references. Ensure at least one
        # provider is registered first so build_config has something
        # to discriminate over.
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401

        result = BrowserProviderRegistrar.build_config()
        assert result.layer_name == "browser_provider"

    def test_local_chrome_registered(self) -> None:
        # Triggers registration via direct module import
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401

        keys = BrowserProviderRegistrar.keys()
        assert "local_chrome" in keys

    def test_browser_capability_wiring_matches_registrar(self) -> None:
        from spectragent.core.capabilities.browser import BrowserCapability

        wiring = BrowserCapability.__wiring__
        backend_wiring = wiring.get("backend")
        # Wiring may be Mode 1 (string) or Mode 2/3 (tuple)
        if isinstance(backend_wiring, str):
            referenced_registry = backend_wiring
        else:
            referenced_registry = backend_wiring[0]
        assert referenced_registry == "browser_provider"

    def test_provider_base_is_abstract(self) -> None:
        # The base class itself must not appear in the registry.
        keys = BrowserProviderRegistrar.keys()
        assert "browserproviderbase" not in {k.lower() for k in keys}


class TestProviderInstantiation:
    def test_local_chrome_provider_can_construct(self) -> None:
        # Don't actually launch Chrome — just verify the class accepts
        # zero-config construction (all fields have defaults).
        from spectragent.core.capabilities.browser.backends.local_chrome import (
            LocalChromeProvider,
        )

        provider = LocalChromeProvider()
        assert provider.provider_id == "local_chrome"
        assert isinstance(provider, BrowserProviderBase)


class TestRegistrarKeysSnapshot:
    def test_at_least_one_provider_registered(self) -> None:
        # Triggers registration of any importable providers
        try:
            import spectragent.core.capabilities.browser.backends  # noqa: F401
        except ImportError as exc:
            pytest.skip(f"browser providers package failed to import: {exc}")

        keys = BrowserProviderRegistrar.keys()
        assert len(keys) >= 1, f"Expected at least one browser provider registered, got: {keys}"


class TestComposerInjectsBackend:
    """ConfigComposer end-to-end: YAML-shaped dict → cap.backend is real."""

    def test_compose_injects_local_chrome_backend(self) -> None:
        # Trigger registration of every concrete provider.
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
        from spectragent.agents._registrar import AgentRegistrar
        from spectragent.agents.memory._registrar import MemoryRegistrar
        from spectragent.core.actions._registrar import ActionRegistrar
        from spectragent.core.capabilities._registrar import CapabilityRegistrar
        from spectragent.core.capabilities.browser.backends.local_chrome import (
            LocalChromeProvider,
        )
        from spectragent.core.capabilities.browser.dom.cleaning import (
            DOMCleanerRegistrar,
        )
        from spectragent.core.capabilities.browser.dom.serializers import (
            DOMSerializerRegistrar,
        )
        from spectragent.core.capabilities.browser.input import (
            MouseTrajectoryRegistrar,
            TypingRhythmRegistrar,
        )
        from spectragent.core.capabilities.browser.replay import (
            MissPolicyRegistrar,
            NetworkSourceRegistrar,
            ReplayabilityRegistrar,
        )
        from spectragent.core.composer import ConfigComposer
        from spectragent.core.environments._registrar import EnvironmentRegistrar
        from spectragent.core.hooks._registrar import HookRegistrar
        from spectragent.core.llm._registrar import LLMRegistrar
        from spectragent.core.loops._registrar import AgentLoopRegistrar

        composer = ConfigComposer(
            registrars={
                "browser_provider": BrowserProviderRegistrar,
                "mouse_trajectory": MouseTrajectoryRegistrar,
                "typing_rhythm": TypingRhythmRegistrar,
                "replayability": ReplayabilityRegistrar,
                "miss_policy": MissPolicyRegistrar,
                "network_source": NetworkSourceRegistrar,
                "dom_cleaner": DOMCleanerRegistrar,
                "dom_serializer": DOMSerializerRegistrar,
                "action": ActionRegistrar,
                "capability": CapabilityRegistrar,
                "agent_loop": AgentLoopRegistrar,
                "llm": LLMRegistrar,
                "memory": MemoryRegistrar,
                "environment": EnvironmentRegistrar,
                "hook": HookRegistrar,
                "agent": AgentRegistrar,
            },
        )

        # Minimum config that satisfies Pydantic schema validation.
        # ``observation`` and ``action`` are inlined by conscribe as
        # discriminated unions on ``BrowserCapabilityConfig`` (declared via
        # ``__wiring__``) but they are declaration-only and *optional*:
        # omitting them is the supported shape, and populating them is a
        # hard error — see
        # ``test_populating_a_negotiation_only_block_is_rejected``.
        raw = {
            "capability": [
                {
                    "capability_id": "browser",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "off"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "dom_serializer": {"serializer_type": "text"},
                    "backend": {"provider_id": "local_chrome"},
                }
            ],
        }
        run = composer.compose(raw)

        cap = run.single("capability")
        # Read via instance dict — bypass conscribe descriptor unwrapping.
        backend = cap.__dict__.get("backend")
        assert isinstance(backend, LocalChromeProvider), (
            f"Composer must setattr-inject a real provider; got {type(backend)}"
        )
        assert backend.provider_id == "local_chrome"

    def test_compose_injects_mode1_wired_receptors(self) -> None:
        """Mode 1 wiring (bare ``"llm"`` / ``"memory"`` strings) on AgentBase.

        ``AgentReceptors`` declares ``agent_loop`` / ``llm`` / ``memory``
        slots, so ConfigComposer setattr-injects the resolved instances and
        ``agent.llm`` stops raising the WiredField "not injected" error.

        ``capability`` is deliberately *not* a slot: it is a
        negotiation-only wiring entry read straight off ``__wiring__``, so
        it is omitted here — conscribe attaches a ``WiredField`` descriptor
        to every wiring entry alike, which is why descriptor presence
        cannot be the test for receptor-ness.
        """
        # Trigger discovery of every concrete impl across the layers we wire.
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
        from spectragent.agents._registrar import AgentRegistrar
        from spectragent.agents.memory._registrar import MemoryRegistrar
        from spectragent.core.actions._registrar import ActionRegistrar
        from spectragent.core.capabilities._registrar import CapabilityRegistrar
        from spectragent.core.capabilities.browser.dom.cleaning import (
            DOMCleanerRegistrar,
        )
        from spectragent.core.capabilities.browser.dom.serializers import (
            DOMSerializerRegistrar,
        )
        from spectragent.core.capabilities.browser.input import (
            MouseTrajectoryRegistrar,
            TypingRhythmRegistrar,
        )
        from spectragent.core.capabilities.browser.replay import (
            MissPolicyRegistrar,
            NetworkSourceRegistrar,
            ReplayabilityRegistrar,
        )
        from spectragent.core.composer import ConfigComposer
        from spectragent.core.environments._registrar import EnvironmentRegistrar
        from spectragent.core.hooks._registrar import HookRegistrar
        from spectragent.core.llm._registrar import LLMRegistrar
        from spectragent.core.loops._registrar import AgentLoopRegistrar

        composer = ConfigComposer(
            registrars={
                "browser_provider": BrowserProviderRegistrar,
                "mouse_trajectory": MouseTrajectoryRegistrar,
                "typing_rhythm": TypingRhythmRegistrar,
                "replayability": ReplayabilityRegistrar,
                "miss_policy": MissPolicyRegistrar,
                "network_source": NetworkSourceRegistrar,
                "dom_cleaner": DOMCleanerRegistrar,
                "dom_serializer": DOMSerializerRegistrar,
                "action": ActionRegistrar,
                "capability": CapabilityRegistrar,
                "agent_loop": AgentLoopRegistrar,
                "llm": LLMRegistrar,
                "memory": MemoryRegistrar,
                "environment": EnvironmentRegistrar,
                "hook": HookRegistrar,
                "agent": AgentRegistrar,
            },
        )

        # All wired children inlined into the agent entry — Mode 1 makes
        # llm/memory full unions of all registered configs.
        raw = {
            "agent": [
                {
                    "agent_id": "browser_use",
                    "agent_loop": {"loop_id": "react"},
                    "llm": {
                        "model_type": "openai",
                        "model": "gpt-4o-mini",
                        "provider": {"name": "direct"},
                    },
                    "memory": {"memory_id": "simple", "max_messages": 50},
                }
            ],
        }
        run = composer.compose(raw)

        # Mode 1 wired fields land via setattr on the agent instance.
        # Reading should NOT raise the WiredField "not injected" error.
        # ``ComposedRun`` has no singular ``.agent`` accessor — a run may
        # hold several agents, so instances are addressed per layer.
        agent = run.instances("agent")[0]
        assert agent.llm is not None
        assert getattr(agent.llm, "model", None) == "gpt-4o-mini"
        assert agent.memory is not None
        # The chosen memory implementation matches the YAML choice.
        assert type(agent.memory).__name__ == "SimpleMemory"

    def test_populating_a_negotiation_only_block_is_rejected(self) -> None:
        """A filled-in ``action:`` under a capability must not vanish.

        ``BrowserCapability.__wiring__`` declares ``action`` so conscribe
        can constrain the schema, but the class has no slot to receive one
        — Pass A skipped it for having no slot and Pass B skipped it for
        being a wiring name, so a user tuning ``ScrollAction.default_amount``
        from YAML got a clean ``compose()``, no action instance, and a run
        that silently used the built-in default.
        """
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
        from spectragent.core.composer import ConfigComposer, UnresolvedWiringError
        from spectragent.scripts.generate_all import _collect_registrars

        composer = ConfigComposer(registrars=_collect_registrars())
        raw = {
            "capability": [
                {
                    "capability_id": "browser",
                    "mouse_trajectory": {"trajectory_type": "instant"},
                    "typing_rhythm": {"rhythm_type": "instant"},
                    "replayability": {"replayability_id": "off"},
                    "network_source": {"source_id": "live"},
                    "dom_cleaner": {"cleaner_type": "browser_use"},
                    "dom_serializer": {"serializer_type": "text"},
                    "backend": {"provider_id": "local_chrome"},
                    "observation": "browser.dom",
                }
            ],
        }

        with pytest.raises(UnresolvedWiringError) as exc_info:
            composer.compose(raw)

        message = str(exc_info.value)
        assert "capability[0].observation" in message
        assert "silently dropped" in message
