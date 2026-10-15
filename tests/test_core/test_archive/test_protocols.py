"""The two Protocols either side of the pull, checked structurally.

The runtime finds the consumer through ``ArchiveConsumer`` and nothing else,
and the environment finds an archiving capability through
``ArchivingCapability`` and nothing else — so what satisfies each one, and
what does not, is the whole contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.archive import ArchiveBatch, ArchiveConsumer, ArchivingCapability
from spectragent.core.capabilities.base import BaseCapability
from spectragent.core.hooks.metrics_collector import MetricsCollector
from tests._helpers.archive_fakes import (
    ArchiveConsumerHook,
    FakeArchivingCapability,
    FakeStepArchiver,
    QuietCapability,
    StructuralCapability,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")


class _StructuralConsumer:
    def __init__(self) -> None:
        self.archive: list[Any] = []


class _StructuralArchiver:
    """The archive surface, by shape, on something that is no capability base."""

    @property
    def provided_archivers(self) -> frozenset[str]:
        return frozenset({"structural.x"})

    def bind_archivers(self, archivers: Any) -> None:
        pass

    async def collect_archive(self) -> ArchiveBatch:
        return ArchiveBatch()


class TestArchiveConsumer:
    def test_a_hook_with_an_archive_list_is_a_consumer(self) -> None:
        assert isinstance(ArchiveConsumerHook([FakeStepArchiver()]), ArchiveConsumer)

    def test_an_empty_archive_still_satisfies_the_shape(self) -> None:
        """The runtime filters on *non-empty*; the Protocol is only the shape."""
        hook = ArchiveConsumerHook()
        assert isinstance(hook, ArchiveConsumer)
        assert hook.archive == []

    def test_a_hook_without_one_is_not(self) -> None:
        assert not isinstance(MetricsCollector(), ArchiveConsumer)

    def test_any_object_with_the_attribute_is(self) -> None:
        assert isinstance(_StructuralConsumer(), ArchiveConsumer)


class TestArchivingCapability:
    def test_the_fake_archiving_capability(self) -> None:
        assert isinstance(FakeArchivingCapability(), ArchivingCapability)

    def test_every_capability_base_subclass_satisfies_it_with_defaults(self) -> None:
        assert isinstance(QuietCapability(), ArchivingCapability)

    def test_a_structural_capability_without_the_surface_does_not(self) -> None:
        cap = StructuralCapability()
        assert isinstance(cap, BaseCapability)
        assert not isinstance(cap, ArchivingCapability)

    def test_a_structural_one_with_the_surface_does(self) -> None:
        assert isinstance(_StructuralArchiver(), ArchivingCapability)
