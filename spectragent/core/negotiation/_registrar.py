"""Conscribe sub-registry for negotiation result types.

Built-in results (``ObservationNegotiationResult``, ``ActionNegotiationResult``,
``ArchiveNegotiationResult``) register automatically. Users can define their
own custom negotiation protocols by registering additional result types here
and passing them as ``target_class`` to
:func:`spectragent.core.negotiation.negotiate`.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar


@runtime_checkable
class NegotiationResult(Protocol):
    """Marker contract for any negotiation result type."""

    result_type: str


NegotiationResultRegistrar = create_registrar(
    "negotiation_result",
    NegotiationResult,
    discriminator_field="result_type",
)


class NegotiationResultBase(metaclass=NegotiationResultRegistrar.Meta):
    """Concrete base for all negotiation result dataclasses.

    Subclasses set ``__registry_key__`` (e.g. ``"observation"``) and are
    typically ``@dataclass(frozen=True)`` value objects.
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def result_type(self) -> str:
        return self.__registry_key__
