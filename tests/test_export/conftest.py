"""Fixtures of the training-export tests."""

from __future__ import annotations

import pytest

from spectragent.core.composer import ConfigComposer
from tests._helpers.export_runs import new_composer


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    """The CLI's composer: every collected registrar, the exporter layers included."""
    return new_composer()
