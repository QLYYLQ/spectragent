"""Immutable facts from one configuration assembly, without configuration values.

Input presence, Python declaration inheritance and runtime injection are separate
questions. These records answer the first and third; Conscribe owns declaration
evidence. They contain no runtime objects or configuration values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

InputSource = Literal["explicit", "default"]
WiringSource = Literal["inline", "pool", "owner_default", "empty", "declaration_only"]


@dataclass(frozen=True)
class ConfiguredInput:
    path: str
    source: InputSource


@dataclass(frozen=True)
class ConfiguredComponent:
    # Owner-created defaults have no source configuration block.
    path: str | None
    layer: str
    key: str
    implementation: str
    instance_id: str | None
    identity_source: Literal["explicit", "ordinal", "owner_default"]


@dataclass(frozen=True)
class WiringDecision:
    path: str
    registry: str
    source: WiringSource
    via: Literal["init", "setattr", "retained"] | None
    shape: Literal["scalar", "list", "dict"] | None
    targets: tuple[ConfiguredComponent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True)
class CompositionEvidence:
    inputs: tuple[ConfiguredInput, ...] = ()
    components: tuple[ConfiguredComponent, ...] = ()
    wiring: tuple[WiringDecision, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "wiring", tuple(self.wiring))


@dataclass(frozen=True)
class CompositionIssue:
    """A safe contract diagnosis, separate from the original exception chain."""

    code: str
    path: str
    message: str
    remedy: str


def configured_inputs(config: BaseModel) -> tuple[ConfiguredInput, ...]:
    """Record presence before assembly; an omitted parent's children are defaults.

    The Pydantic presence set preserves explicit null/empty/default-equal values.
    An internal default factory cannot turn its constructed fields into YAML input.
    This also accepts a validated model supplied directly by a Python caller.
    """
    found: list[ConfiguredInput] = []

    def walk(value: object, path: str, supplied: bool) -> None:
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                child = f"{path}.{name}" if path else name
                explicit = supplied and name in value.model_fields_set
                found.append(ConfiguredInput(child, "explicit" if explicit else "default"))
                walk(getattr(value, name), child, explicit)
        elif isinstance(value, list | tuple):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", supplied)
        elif isinstance(value, dict):
            for name, child in value.items():
                walk(child, f"{path}[{name}]", supplied)

    walk(config, "", True)
    return tuple(found)


def implementation_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"
