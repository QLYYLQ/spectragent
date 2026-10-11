"""``ActionTranslatorRegistrar``: registered, collected, configured from YAML (arch §4.15, §6)."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.composer import ConfigComposer
from spectragent.export import (
    ActionTranslator,
    ActionTranslatorRegistrar,
    IdentityTranslator,
    IndexToCoordinateTranslator,
    default_translator,
)
from spectragent.scripts.generate_all import _collect_registrars


def _compose(block: dict[str, object]) -> object:
    run = ConfigComposer(registrars=_collect_registrars()).compose({"action_translator": [block]})
    return run.single("action_translator")


def test_the_shipped_translators() -> None:
    assert sorted(ActionTranslatorRegistrar.keys()) == ["identity", "index_to_coordinate"]
    assert _collect_registrars()["action_translator"] is ActionTranslatorRegistrar
    assert isinstance(default_translator(), IdentityTranslator)
    assert isinstance(IndexToCoordinateTranslator(), ActionTranslator)


def test_knobs_arrive_from_yaml() -> None:
    translator = _compose(
        yaml.safe_load(
            'translator_id: index_to_coordinate\nocclusion: "off"\n'
            "occlusion_unknown: untranslatable\nuntranslatable: keep_as_text\n"
        )
    )
    assert isinstance(translator, IndexToCoordinateTranslator)
    assert (translator.occlusion, translator.occlusion_unknown, translator.untranslatable) == (
        "off",
        "untranslatable",
        "keep_as_text",
    )
    identity = _compose({"translator_id": "identity", "untranslatable": "fail"})
    assert isinstance(identity, IdentityTranslator) and identity.untranslatable == "fail"


@pytest.mark.parametrize(
    "block",
    [
        {"translator_id": "index_to_coordinates"},
        {"translator_id": "index_to_coordinate", "occlusion": "maybe"},
        # A bare ``off`` is YAML 1.1's false: refused, not silently "check".
        yaml.safe_load("translator_id: index_to_coordinate\nocclusion: off\n"),
        {"translator_id": "identity", "occlusion": "off"},
    ],
)
def test_a_wrong_block_is_refused(block: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _compose(block)
