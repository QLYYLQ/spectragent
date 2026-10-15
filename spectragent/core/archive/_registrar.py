"""Archiver registrar — the third provides axis: environment evidence.

An *archiver* names one kind of environment evidence a capability can
hand over on pull — response bodies, storage snapshots, raw DOM captures
(``docs/architecture_design.md`` §4.5, §5.1). It is the archive-axis twin
of an action or an observation fragment, and it is shaped the same way:

- one registered class per archiver, keyed ``<capability>.<id>``
  (``browser.network``) — fully qualified, because the prefix names the
  capability that provides it;
- a capability declares what it provides in ``__wiring__["archiver"]``;
- negotiation (``core/negotiation/archive.py``) binds what a run asks for
  to the capability that provides it.

The one asymmetry is where the *demand* comes from. What a run records is
a per-run choice (the user's ruling of 2026-10-09, AGENTS.md §2.5), so no class ``__wiring__`` declares an
archiver requirement: the trace hook instance's ``archive`` list does
(:class:`~spectragent.core.archive.protocols.ArchiveConsumer`), always
fully qualified.

:class:`ArchiverBase` carries **identity and declarations only**. Capture
behaviour belongs to the providing capability's own archiver base (the
browser's archivers); the framework — runtime, environment, trace hook —
never calls a capture method on an archiver. An archiver's knobs are
``Annotated`` constructor parameters on its subclass, and the instances
travel with them: the trace hook holds them, ``bind_archivers`` hands them
to the capability.
"""

from typing import ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar

#: Separator between the providing capability and the archiver's local id.
#:
#: Same role as ``ACTION_KEY_SEPARATOR``: it tells conscribe the keys are
#: hierarchical. Unlike actions and fragments, an archive demand is never
#: written relative — the archiver instance's ``archiver_id`` *is* the
#: full key — so nothing ever needs expanding.
ARCHIVER_KEY_SEPARATOR = "."


@runtime_checkable
class Archiver(Protocol):
    """What the framework knows about an archiver: who it is, what it declares.

    Deliberately no capture method. Producing evidence is the providing
    capability's job and only on pull (``collect_archive``); a capability
    that needs per-archiver behaviour defines its own archiver base with
    whatever methods it likes, and nothing outside that capability calls
    them.
    """

    @property
    def archiver_id(self) -> str:
        """Fully-qualified registry key, ``<capability>.<id>``."""
        ...

    @property
    def privacy_markers(self) -> frozenset[str]:
        """Privacy facts this archiver's configuration declares (§5.6)."""
        ...


ArchiverRegistrar = create_registrar(
    "archiver",
    Archiver,
    discriminator_field="archiver_id",
    key_separator=ARCHIVER_KEY_SEPARATOR,
    inline_only=True,
)


class ArchiverBase(metaclass=ArchiverRegistrar.Meta):
    """Concrete base for every archiver.

    Subclasses set ``__registry_key__`` to ``"<capability>.<id>"`` — the
    capability prefix is what negotiation routes on — and expose their
    knobs as ``Annotated`` constructor parameters: either ``@dataclass``
    fields (the ``ActionBase`` shape) or the parameters of a hand-written
    ``__init__``. Both work because an archiver carries no ``__wiring__``
    receptor — the dataclass trap of ``af-add-a-receptor`` §6 is a
    receptor owner's. ``__config_annotated_only__`` keeps every other
    constructor parameter (an injected clock, a CDP handle) out of the
    generated config.

    The base holds no capture method on purpose. The providing capability
    reads the bound instances it receives in ``bind_archivers`` — knobs
    included — and decides what to capture; the trace hook only ever moves
    the batches ``collect_archive`` hands over.

    Example::

        class NetworkArchiver(BrowserArchiverBase):      # BrowserArchiverBase(ArchiverBase)
            __registry_key__ = "browser.network"

            def __init__(
                self,
                max_inline_body_bytes: Annotated[int, Field(ge=0, description="...")] = 65536,
            ) -> None:
                self.max_inline_body_bytes = max_inline_body_bytes
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def archiver_id(self) -> str:
        """Fully-qualified registry key (e.g. ``"browser.network"``)."""
        return self.__registry_key__

    @property
    def source(self) -> str:
        """The leading segment of :attr:`archiver_id` — the providing capability."""
        return self.__registry_key__.partition(ARCHIVER_KEY_SEPARATOR)[0]

    @property
    def privacy_markers(self) -> frozenset[str]:
        """Privacy facts this archiver declares, derived from its knobs.

        A *declaration*, not a guarantee: the framework records the union
        of the bound archivers' markers in ``TraceConfig.env_evidence_privacy``
        and never checks the bytes against it (the capability is what
        strips and masks, ``docs/architecture_design.md`` §5.6). Marker
        constants live in :mod:`spectragent.core.archive.privacy`.
        Default: declares nothing.
        """
        return frozenset()
