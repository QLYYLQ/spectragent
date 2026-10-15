"""Shared lifetime configuration is validated before Chrome starts."""

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.replay.archive_source import ArchiveNetworkSource


def test_shared_lifetime_refuses_a_persistent_profile():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(lifetime="experiment", user_data_dir="unused-profile")
    with pytest.raises(BrowserCooperationError, match="do not inherit"):
        cap.check_setup({})
    assert not cap.backend._launches


def test_shared_lifetime_cannot_claim_an_arbitrary_external_browser():
    cap = BrowserCapability(cdp_url="ws://unused")
    cap.backend = LocalChromeProvider(lifetime="experiment")
    with pytest.raises(BrowserCooperationError, match="cannot borrow an explicit"):
        cap.check_setup({})


def test_archive_replay_requires_its_own_process():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(lifetime="experiment")
    cap.network_source = ArchiveNetworkSource()
    with pytest.raises(BrowserCooperationError, match="requires a dedicated Chrome"):
        cap.check_setup({})


async def test_direct_python_shared_setup_requires_an_explicit_scope():
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(lifetime="experiment")
    cap.check_setup({})  # configuration inspection remains pure and valid
    try:
        with pytest.raises(RuntimeError, match="explicitly bound ExperimentScope"):
            await cap.setup()
        assert not cap.backend._launches
    finally:
        await cap.teardown()
