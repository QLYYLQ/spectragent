"""DOM serializers — convert DOMState to LLM-readable text.

Registered via conscribe for config-driven selection.

Each serializer also says, in a few lines a model can read, what its text
looks like (:meth:`DOMSerializerProtocol.describe_format`). An agent's
prompt quotes that description instead of describing the format itself, so
the format knowledge lives with the code that writes it and a run's prompt
changes with its serializer (``docs/architecture_design.md`` §4.4
"观测格式由提供方描述", §4.12).
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

    def describe_format(self) -> str:
        """A short description, for a model, of the text :meth:`serialize` writes.

        What an element line looks like, which elements can be acted on and
        how an action addresses one by its index, and every other marker the
        text can carry. Written for this instance, its knobs included, and
        true of everything :meth:`serialize` emits: an agent quotes it in its
        prompt verbatim, so a claim here that the output does not keep is a
        claim the model is taught.
        """
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
