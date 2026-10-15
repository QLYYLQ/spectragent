"""Public, immutable index of the configuration space built by Conscribe.

Discovery belongs to the caller. The catalog builds the schema once and keeps
the implementation and its final inline configuration model together. Both
composition and authoring tools consume this index instead of inspecting one
another's private caches.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from conscribe import build_composed_config
from pydantic import BaseModel


@dataclass(frozen=True)
class ComponentDefinition:
    """One registered implementation and the model that validates its block."""

    layer: str
    key: str
    implementation: type
    config_model: type[BaseModel]


@dataclass(frozen=True)
class ConfigurationCatalog:
    """Schema references with immutable membership and dependency ordering.

    Model and implementation classes retain their normal Python identity; the
    catalog does not wrap or copy class definitions. Mapping membership cannot
    mutate the Composer's lookup tables.
    """

    top_level_type: type[BaseModel]
    top_level_layers: tuple[str, ...]
    dependency_order: tuple[str, ...]
    components: Mapping[str, Mapping[str, ComponentDefinition]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_level_layers", tuple(self.top_level_layers))
        object.__setattr__(self, "dependency_order", tuple(self.dependency_order))
        object.__setattr__(
            self,
            "components",
            MappingProxyType(
                {
                    layer: MappingProxyType(dict(entries))
                    for layer, entries in self.components.items()
                }
            ),
        )


def build_configuration_catalog(registrars: Mapping[str, Any]) -> ConfigurationCatalog:
    """Build the one run schema from already discovered registrars."""
    composed = build_composed_config(dict(registrars), inline_wiring=True, top_level_extra="forbid")
    return ConfigurationCatalog(
        top_level_type=composed.top_level_type,
        top_level_layers=tuple(composed.top_level_layers),
        dependency_order=tuple(composed.dependency_order),
        components={
            layer: {
                key: ComponentDefinition(layer, key, registrars[layer].get(key), model)
                for key, model in result.per_key_models.items()
            }
            for layer, result in composed.layer_results.items()
        },
    )
