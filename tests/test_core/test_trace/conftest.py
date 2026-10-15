"""Shared fixtures for trace tests that need the *real* composed schema.

Redaction is only meaningful against the model the Composer actually
validates against. conscribe rebuilds that model from the source classes,
so a test written against a hand-rolled Pydantic model proves nothing
about whether a mark survived the rebuild — and a fixture that
monkeypatches the schema proves less. These tests take the real one.

Building it walks every registrar, so it is session-scoped: one build for
the whole trace package.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel


@pytest.fixture(scope="session")
def composer() -> Any:
    from spectragent.core.composer import ConfigComposer
    from spectragent.core.registries import collect_registrars

    return ConfigComposer(registrars=collect_registrars())


@pytest.fixture(scope="session")
def composed_top_level_type(composer: Any) -> type[BaseModel]:
    """The composed, inline-wired top-level config model."""
    return composer.top_level_type
