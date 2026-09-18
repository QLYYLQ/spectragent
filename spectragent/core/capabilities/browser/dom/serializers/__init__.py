"""DOM serializers — convert DOMState to LLM-readable text.

Registered via conscribe for config-driven selection.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.capabilities.browser.dom.models import DOMState


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


def default_serializer() -> DOMSerializerProtocol:
    """The serializer used when no configuration named one.

    Two places have to answer this question and they must answer it the
    same way: ``BrowserCapability.__init__``, which assigns an in-Python
    default over its ``dom_serializer`` ``WiredField`` so a hand-built
    capability still works, and ``DOMWatchdog.__init__``, whose
    ``WatchdogContext.dom_serializer`` is ``None`` for a hand-built
    watchdog. Naming ``TextSerializer()`` in both is one fact in two
    files: change the capability's default and every hand-built watchdog
    silently stays on the old strategy.

    Deliberately *not* a registry lookup — ``DOMSerializerRegistrar.get(
    "text")()`` would be the very shape ``test_receptor_hygiene.py``
    forbids. This is a named constructor for the framework default, not a
    configuration surface; configuration goes through the receptor.

    Imported lazily because ``text_serializer`` imports this module for
    ``DOMSerializerBase``.
    """
    from spectragent.core.capabilities.browser.dom.serializers.text_serializer import (
        TextSerializer,
    )

    return TextSerializer()
