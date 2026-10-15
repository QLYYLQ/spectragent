"""The archiver registrar: the third provides axis, shaped like the other two.

What is pinned here is the *framework* half of an archiver — identity,
declarations, knobs — and the one thing the framework half must never grow:
a capture method. Capture behaviour belongs to the providing capability's
own archiver base (M29), and the runtime and the trace hook never call it.
"""

from __future__ import annotations

import ast
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest
from conscribe.config.extractor import extract_config_schema
from conscribe.registration.registry import get_registry
from pydantic import Field

import spectragent.core.archive as archive_pkg
from spectragent.core.archive import (
    ARCHIVER_KEY_SEPARATOR,
    Archiver,
    ArchiverBase,
    ArchiverRegistrar,
)
from spectragent.core.archive.privacy import MARKER_REDACTED
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    ORPHAN_KEY,
    STEPS_KEY,
    UNPROVIDED_KEY,
    FakeBlobArchiver,
    FakeOrphanArchiver,
    FakeStepArchiver,
    FakeUnprovidedArchiver,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")

_ARCHIVE_PACKAGE = Path(archive_pkg.__file__).parent


class TestTheRegistrar:
    def test_registry_name_discriminator_and_separator(self) -> None:
        registry = get_registry("archiver")
        assert registry is not None
        assert registry.name == "archiver"
        assert registry.separator == ARCHIVER_KEY_SEPARATOR == "."
        assert ArchiverRegistrar.discriminator_field == "archiver_id"
        assert ArchiverRegistrar.protocol is Archiver

    def test_a_subclass_registers_under_its_fully_qualified_key(self) -> None:
        assert ArchiverRegistrar.get(STEPS_KEY) is FakeStepArchiver
        assert ArchiverRegistrar.get(BLOBS_KEY) is FakeBlobArchiver
        assert ArchiverRegistrar.get(UNPROVIDED_KEY) is FakeUnprovidedArchiver
        assert ArchiverRegistrar.get(ORPHAN_KEY) is FakeOrphanArchiver

    def test_the_abstract_base_is_not_registered(self) -> None:
        assert ArchiverBase not in ArchiverRegistrar.get_all().values()


class TestIdentity:
    def test_archiver_id_is_the_registry_key(self) -> None:
        assert FakeStepArchiver().archiver_id == STEPS_KEY
        assert FakeOrphanArchiver().archiver_id == ORPHAN_KEY

    def test_source_is_the_providing_capability(self) -> None:
        assert FakeStepArchiver().source == "contract_archive"
        assert FakeOrphanArchiver().source == "contract_nowhere"

    def test_every_fake_satisfies_the_protocol(self) -> None:
        for cls in (FakeStepArchiver, FakeBlobArchiver, FakeUnprovidedArchiver, FakeOrphanArchiver):
            assert isinstance(cls(), Archiver), cls.__registry_key__

    def test_configuration_is_not_an_archiver(self) -> None:
        """The name-not-instance defect: a raw config dict is refused by shape."""
        assert not isinstance({"archiver_id": STEPS_KEY}, Archiver)
        assert not isinstance(STEPS_KEY, Archiver)


class TestDeclarations:
    def test_the_default_declares_no_privacy_marker(self) -> None:
        assert FakeBlobArchiver().privacy_markers == frozenset()
        assert isinstance(FakeBlobArchiver().privacy_markers, frozenset)

    def test_a_marker_is_derived_from_the_knobs(self) -> None:
        assert FakeStepArchiver().privacy_markers == frozenset({MARKER_REDACTED})
        assert FakeStepArchiver(redact=False).privacy_markers == frozenset()

    def test_the_base_carries_identity_and_declarations_only(self) -> None:
        """No capture method on the framework base (decision 2).

        A ``capture`` / ``collect`` here would invite the runtime or the
        trace hook to call it — and the moment either did, the capability
        would no longer own what it hands over. The capability's own
        archiver base is where capture methods go.
        """
        public = {name for name in vars(ArchiverBase) if not name.startswith("_")}
        assert public == {"archiver_id", "source", "privacy_markers"}


class TestKnobsReachTheConfig:
    """An archiver's knobs are ``Annotated`` constructor parameters.

    The registrar is not collected by the Composer until the first concrete
    archiver lands (M29), so this pins the extractor directly: the knobs
    survive, with their constraints, and nothing that is not a knob leaks in.
    """

    def test_the_knob_and_its_constraint_are_extracted(self) -> None:
        schema = extract_config_schema(FakeBlobArchiver)
        assert schema is not None
        field = schema.model_fields["payload_bytes"]
        assert field.default == 32
        assert any(getattr(m, "ge", None) == 8 for m in field.metadata)

    def test_a_class_without_knobs_extracts_none(self) -> None:
        schema = extract_config_schema(FakeOrphanArchiver)
        fields = set(schema.model_fields) if schema is not None else set()
        assert fields <= {"archiver_id"}

    def test_a_plain_constructor_parameter_is_not_a_knob(self) -> None:
        """``ArchiverBase.__config_annotated_only__``: only ``Annotated`` parameters reach YAML.

        A capability's archiver base may well take an injected collaborator
        — a clock, a CDP handle. Without the flag it would leak into the
        generated config and its JSON schema, and a ``Callable`` would break
        generation outright.
        """

        class _InjectedClock(ArchiverBase):
            __abstract__ = True  # never registered: the registry is process-global
            __registry_key__ = "contract_archive.injected_clock"

            def __init__(
                self,
                knob: Annotated[int, Field(description="a real knob")] = 1,
                clock: Callable[[], float] = time.time,
            ) -> None:
                self.knob = knob
                self.clock = clock

        schema = extract_config_schema(_InjectedClock)
        assert schema is not None
        assert set(schema.model_fields) - {"archiver_id"} == {"knob"}


def _future_annotations_import(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


class TestNoPostponedAnnotations:
    """PEP 563 would drop every archiver knob from the generated config.

    A knob declared under ``from __future__ import annotations`` is a string
    resolved in the subclass's module, conscribe sees no field info, and
    ``__config_annotated_only__`` drops it — silently.
    """

    def test_no_module_of_the_package_postpones_annotations(self) -> None:
        modules = sorted(_ARCHIVE_PACKAGE.glob("*.py"))
        assert {p.name for p in modules} >= {
            "__init__.py",
            "_registrar.py",
            "types.py",
            "protocols.py",
            "privacy.py",
        }
        offenders = [p.name for p in modules if _future_annotations_import(p)]
        assert offenders == []

    def test_the_scan_would_catch_one(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted.py"
        planted.write_text('"""doc"""\n\nfrom __future__ import annotations\n', encoding="utf-8")
        assert _future_annotations_import(planted)
