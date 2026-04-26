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
        assert len(keys) >= 1, (
            f"Expected at least one browser provider registered, got: {keys}"
        )


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
        from spectragent.core.composer import ConfigComposer
        from spectragent.core.environments._registrar import EnvironmentRegistrar
        from spectragent.core.hooks._registrar import HookRegistrar
        from spectragent.core.llm._registrar import LLMRegistrar
        from spectragent.core.loops._registrar import AgentLoopRegistrar

        composer = ConfigComposer(
            registrars={
                "browser_provider": BrowserProviderRegistrar,
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

        # Minimum config that satisfies Pydantic schema validation. The
        # ``observation`` and ``action`` fields are inlined by conscribe
        # as discriminated unions on ``BrowserCapabilityConfig`` (declared
        # via ``__wiring__``). They're declarative-only — Composer skips
        # them at instantiation time but Pydantic still requires a value.
        raw = {
            "capability": [
                {
                    "capability_id": "browser",
                    "observation": "browser.dom",
                    "action": {"action_id": "browser.click"},
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

    def test_compose_injects_mode1_wired_via_descriptor(self) -> None:
        """Mode 1 wiring (bare ``"llm"`` / ``"memory"`` strings) on AgentBase.

        Conscribe's metaclass attaches ``WiredField`` descriptors for these
        receptors but they don't show up in ``get_type_hints`` (the descriptor
        is a class attribute, not an annotation). ConfigComposer must detect
        the descriptor itself so it still setattr-injects the resolved
        instances. Without that detection, ``agent.llm`` raises
        ``AttributeError("not been injected yet")`` after compose.
        """
        # Trigger discovery of every concrete impl across the layers we wire.
        import spectragent.core.capabilities.browser.backends.local_chrome  # noqa: F401
        from spectragent.agents._registrar import AgentRegistrar
        from spectragent.agents.memory._registrar import MemoryRegistrar
        from spectragent.core.actions._registrar import ActionRegistrar
        from spectragent.core.capabilities._registrar import CapabilityRegistrar
        from spectragent.core.composer import ConfigComposer
        from spectragent.core.environments._registrar import EnvironmentRegistrar
        from spectragent.core.hooks._registrar import HookRegistrar
        from spectragent.core.llm._registrar import LLMRegistrar
        from spectragent.core.loops._registrar import AgentLoopRegistrar

        composer = ConfigComposer(
            registrars={
                "browser_provider": BrowserProviderRegistrar,
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
                    "capability": {
                        "capability_id": "browser",
                        "observation": "browser.dom",
                        "action": {"action_id": "browser.click"},
                        "backend": {"provider_id": "local_chrome"},
                    },
                    "observation": "browser.dom",
                    "action": {"action_id": "browser.click"},
                }
            ],
        }
        run = composer.compose(raw)

        # Mode 1 wired fields land via setattr on the agent instance.
        # Reading should NOT raise the WiredField "not injected" error.
        agent = run.agent
        assert agent.llm is not None
        assert getattr(agent.llm, "model", None) == "gpt-4o-mini"
        assert agent.memory is not None
        # The chosen memory implementation matches the YAML choice.
        assert type(agent.memory).__name__ == "SimpleMemory"
