"""A capability's ``archiver:`` field: generated, optional — and never fillable.

``__wiring__["archiver"]`` on a capability is a *provides* declaration with no
slot to receive a value, exactly like ``__wiring__["observation"]``. conscribe
therefore emits it as an optional ``archiver: Optional[Literal[...]] = None``
on the capability's config, and the Composer refuses a YAML that populates
it: there is nothing to receive the value, and dropping it silently is the
defect ``UnresolvedWiringError`` exists to stop. Archivers are chosen on the
trace hook (``hook[].archive``, from M29), never on the capability
(``docs/architecture_design.md`` §4.5, §4.2.1).
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from conscribe.config.extractor import extract_config_schema
from pydantic import ValidationError

from spectragent.core.capabilities._registrar import CapabilityRegistrar
from spectragent.core.composer import ConfigComposer, UnresolvedWiringError
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    STEPS_KEY,
    FakeArchivingCapability,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")


@pytest.fixture(scope="module")
def composer() -> ConfigComposer:
    return ConfigComposer(registrars={"capability": CapabilityRegistrar})


def _flatten(annotation: Any) -> set[Any]:
    """Every leaf type / literal value inside an annotation."""
    args = get_args(annotation)
    if not args:
        return {annotation}
    leaves: set[Any] = set()
    for arg in args:
        leaves |= _flatten(arg)
    return leaves


class TestTheGeneratedField:
    def test_it_is_optional_and_defaults_to_none(self) -> None:
        schema = extract_config_schema(FakeArchivingCapability)
        assert schema is not None
        field = schema.model_fields["archiver"]
        assert field.default is None
        assert not field.is_required()

    def test_its_literal_names_exactly_the_provided_archivers(self) -> None:
        schema = extract_config_schema(FakeArchivingCapability)
        assert schema is not None
        leaves = _flatten(schema.model_fields["archiver"].annotation)
        assert {leaf for leaf in leaves if isinstance(leaf, str)} == {STEPS_KEY, BLOBS_KEY}


class TestTheComposer:
    def test_unpopulated_it_composes(self, composer: ConfigComposer) -> None:
        run = composer.compose({"capability": [{"capability_id": CAPABILITY_ID}]})
        (cap,) = run.by_layer["capability"].values()
        assert isinstance(cap, FakeArchivingCapability)
        assert cap.provided_archivers == frozenset({STEPS_KEY, BLOBS_KEY})

    def test_populated_it_is_refused_not_dropped(self, composer: ConfigComposer) -> None:
        with pytest.raises(UnresolvedWiringError, match="negotiation-only entry") as excinfo:
            composer.compose(
                {"capability": [{"capability_id": CAPABILITY_ID, "archiver": STEPS_KEY}]}
            )
        assert "capability[0].archiver" in str(excinfo.value)

    def test_a_key_the_capability_does_not_provide_fails_validation(
        self, composer: ConfigComposer
    ) -> None:
        with pytest.raises(ValidationError):
            composer.compose(
                {
                    "capability": [
                        {"capability_id": CAPABILITY_ID, "archiver": "contract_archive.bogus"}
                    ]
                }
            )
