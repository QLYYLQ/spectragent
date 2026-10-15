"""The ``network_source`` axis: where the browser's network comes from (§4.14 "回放").

A network source is a ``BrowserCapability`` receptor
(``__wiring__["network_source"]``), with the shape of ``replayability``: the
strategy is the switch, ``live`` is the default and the no-op, and nothing
beside it can disagree with it.

- ``live`` (``live.py``) — the network is the network: nothing is intercepted.
- ``archive`` (``archive_source.py``) — the network is a recorded run's
  archive: every request is answered at the Fetch Request stage from what that
  run recorded, and **never** continued to the network. Whether the browser
  must also be cut off from the network (``isolation``) is the source's own
  property — there is no provider boolean beside it.

A source owns two run-scoped steps, both pure construction: opening the
recorded run (:meth:`NetworkSourceProtocol.open_replay`, at setup, before the
browser starts — the source run directory arrives as the task context's
``replay_archive`` and is opened through ``core/archive``'s ``open_archive``)
and building the run's replay server (:meth:`NetworkSourceProtocol.serve`, a
recorder participant). The configured instance holds knobs only.
"""

from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.replay.archive_index import RecordedArchive
from spectragent.core.capabilities.browser.replay.server import ReplayContext, ReplayServer

#: Whether an intercepting source needs the browser cut off from the network:
#: ``required`` (the default) or ``none_acknowledged`` (written out: run with
#: Fetch interception alone, which leaks connections Fetch never sees).
Isolation = Literal["required", "none_acknowledged"]


@runtime_checkable
class NetworkSourceProtocol(Protocol):
    """A network source (``live`` / ``archive``)."""

    @property
    def intercepts(self) -> bool:
        """``True`` when the source answers the browser's requests itself."""
        ...

    @property
    def isolation(self) -> Isolation | None:
        """What an intercepting source needs of the provider; ``None`` when it intercepts nothing."""
        ...

    def check_config(self, reader: ArchiveReader | None = None) -> None:
        """Validate source-owned experiment inputs and recorded request coverage before a run."""
        ...

    def open_replay(self, reader: ArchiveReader) -> RecordedArchive:
        """Load the recorded run *reader* reads (setup, before the browser starts)."""
        ...

    def serve(self, recorded: RecordedArchive, ctx: ReplayContext) -> ReplayServer:
        """This run's replay server for *recorded*: a recorder participant (pure construction).

        *ctx* carries the run's recorder handle, observation cycle and clock.
        """
        ...

    def fingerprint(self) -> dict[str, Any]:
        """The configured source as the environment fingerprint records it.

        A replaying run records its server's block instead (this plus the
        source run, the digests and the warnings — ``ReplayServer.fingerprint``).
        """
        ...


NetworkSourceRegistrar = create_registrar(
    "network_source",
    NetworkSourceProtocol,
    discriminator_field="source_id",
)


class NetworkSourceBase(metaclass=NetworkSourceRegistrar.Meta):
    """Base for network sources — subclass to auto-register.

    Each source sets ``__registry_key__`` explicitly (the YAML-facing
    ``source_id``) and takes its knobs as ``__init__`` keyword parameters.
    The safe defaults are the live network's: nothing intercepted, no
    isolation, no archive to open and nothing to serve.
    """

    __abstract__ = True
    __registry_key__: str
    #: Receptors of this source; a source with knobs of its own strategy
    #: (``archive``: ``miss_policy``) overrides it.
    __wiring__: ClassVar[dict[str, Any]] = {}

    @property
    def source_id(self) -> str:
        return self.__registry_key__

    @property
    def intercepts(self) -> bool:
        return False

    @property
    def isolation(self) -> Isolation | None:
        return None

    def check_config(self, reader: ArchiveReader | None = None) -> None:
        """A source without extra inputs has nothing to check."""

    def open_replay(self, reader: ArchiveReader) -> RecordedArchive:
        raise NotImplementedError(
            f"network source {self.source_id!r} intercepts nothing: it opens no recorded "
            f"archive (replay_archive is for an intercepting source such as 'archive')"
        )

    def serve(self, recorded: RecordedArchive, ctx: ReplayContext) -> ReplayServer:
        raise NotImplementedError(
            f"network source {self.source_id!r} intercepts nothing: it builds no replay server"
        )

    def fingerprint(self) -> dict[str, Any]:
        return {"source_id": self.source_id}


def default_network_source() -> NetworkSourceProtocol:
    """The source used when no configuration named one: ``live``.

    Not a registry lookup — the receptor is the configuration surface; this
    is the framework's answer for a hand-built capability.
    """
    from spectragent.core.capabilities.browser.replay.live import LiveNetworkSource

    return LiveNetworkSource()
