"""``CapabilityBase``'s archive defaults — inert, and loud when misused.

The archive surface is optional: a capability that records nothing should
implement nothing, which is why it lives on ``ArchivingCapability`` and not
on ``BaseCapability`` (adding members to a ``@runtime_checkable`` Protocol
changes ``isinstance`` for every structural implementer). ``CapabilityBase``
satisfies it with defaults that cost nothing — and refuse a real binding
rather than accept it and collect nothing.
"""

from __future__ import annotations

import pytest

from spectragent.core.archive import ArchiveBatch
from spectragent.core.capabilities.base import BaseCapability
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    STEPS_KEY,
    FakeArchivingCapability,
    FakeStepArchiver,
    QuietCapability,
    StructuralCapability,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")


class TestDefaults:
    def test_provided_archivers_is_derived_from_the_wiring(self) -> None:
        """The same derivation ``declared_fragments`` uses, on the third axis."""
        assert FakeArchivingCapability().provided_archivers == frozenset({STEPS_KEY, BLOBS_KEY})

    def test_no_wiring_entry_provides_nothing(self) -> None:
        assert QuietCapability().provided_archivers == frozenset()

    def test_an_empty_binding_is_a_no_op(self) -> None:
        QuietCapability().bind_archivers([])

    def test_a_real_binding_names_the_class_and_the_keys(self) -> None:
        with pytest.raises(NotImplementedError, match="QuietCapability") as excinfo:
            QuietCapability().bind_archivers([FakeStepArchiver()])
        assert STEPS_KEY in str(excinfo.value)
        assert "collect_archive" in str(excinfo.value)

    async def test_collect_archive_returns_an_empty_batch(self) -> None:
        batch = await QuietCapability().collect_archive()
        assert isinstance(batch, ArchiveBatch)
        assert batch.is_empty


class TestBaseCapabilityIsUnchanged:
    def test_a_structural_capability_without_the_archive_surface_is_still_one(self) -> None:
        """No archive member was added to ``BaseCapability``: its ``isinstance``
        answer for a structural implementer is what it was."""
        cap = StructuralCapability()
        assert isinstance(cap, BaseCapability)
        assert not hasattr(cap, "provided_archivers")
