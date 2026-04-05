"""DOM serializers — convert DOMState to LLM-readable text.

Registered via conscribe for config-driven selection.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.browser.dom.models import DOMState


@runtime_checkable
class DOMSerializerProtocol(Protocol):
    """Serialize a DOMState tree to a string for LLM consumption."""

    def serialize(self, state: DOMState) -> str:
        """Return an LLM-readable text representation of the DOM tree."""
        ...


DOMSerializerRegistrar = create_registrar(
    "dom_serializer",
    DOMSerializerProtocol,
    discriminator_field="serializer_type",
)


class DOMSerializerBase(metaclass=DOMSerializerRegistrar.Meta):
    """Base for DOM serializers — subclass to auto-register."""

    __abstract__ = True
    __registry_key__: str

    @property
    def serializer_type(self) -> str:
        return self.__registry_key__
