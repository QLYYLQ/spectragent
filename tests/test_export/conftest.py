"""Fixtures of the training-export tests."""

from __future__ import annotations

import pytest

from spectragent.core.composer import ConfigComposer
from tests._helpers.export_runs import new_composer


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    """Every package imported, every layer collected: export documents and student configs alike."""
    return new_composer()
