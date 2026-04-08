"""Tests for the browser_provider conscribe registrar.

BrowserCapability's ``__wiring__`` references the ``browser_provider``
registry. This test verifies the registry exists and at least the
critical providers (local_chrome) are registered. Other providers
(lexmount, browseruse_lite, ...) require optional cloud SDKs to import,
so we don't fail the test if they're missing.
"""

from __future__ import annotations

import pytest

from spectragent.core.providers.browser._registrar import (
    BrowserProviderBase,
    BrowserProviderRegistrar,
)


class TestRegistrar:
    def test_registrar_layer_name(self) -> None:
        # Hard-coded sanity check: registrar name must match what
        # BrowserCapability.__wiring__ references. Ensure at least one
        # provider is registered first so build_config has something
        # to discriminate over.
        import spectragent.core.providers.browser.local_chrome  # noqa: F401

        result = BrowserProviderRegistrar.build_config()
        assert result.layer_name == "browser_provider"

    def test_local_chrome_registered(self) -> None:
        # Triggers registration via direct module import
        import spectragent.core.providers.browser.local_chrome  # noqa: F401

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
        from spectragent.core.providers.browser.local_chrome import (
            LocalChromeProvider,
        )

        provider = LocalChromeProvider()
        assert provider.provider_id == "local_chrome"
        assert isinstance(provider, BrowserProviderBase)


class TestRegistrarKeysSnapshot:
    def test_at_least_one_provider_registered(self) -> None:
        # Triggers registration of any importable providers
        try:
            import spectragent.core.providers.browser  # noqa: F401
        except ImportError as exc:
            pytest.skip(f"browser providers package failed to import: {exc}")

        keys = BrowserProviderRegistrar.keys()
        assert len(keys) >= 1, (
            f"Expected at least one browser provider registered, got: {keys}"
        )
