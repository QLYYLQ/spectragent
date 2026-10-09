"""Observation Fragment registrar — conscribe-driven auto-registration.

Uses ``bridge(BaseModel)`` to resolve the metaclass conflict between
Pydantic's ``ModelMetaclass`` and conscribe's ``AutoRegistrar``.
Validated by conscribe's own test suite (test_pydantic_generic.py:183-239).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Literal, Self

from conscribe import create_registrar
from pydantic import BaseModel, Field, model_validator

from spectragent.schemas.observation.base import ObservationFragment

#: Separator between the fragment's source and its local id.
#:
#: Same role as ``ACTION_KEY_SEPARATOR``: it is what makes a
#: capability-relative ``__wiring__`` entry (``"screenshot"``) resolvable
#: against whichever provider the assembled environment actually holds,
#: while keys stay fully qualified everywhere else.
FRAGMENT_KEY_SEPARATOR = "."

ObservationRegistrar = create_registrar(
    "observation",
    ObservationFragment,
    discriminator_field="fragment_id",
    key_separator=FRAGMENT_KEY_SEPARATOR,
)


def fragment_field_name(fragment_key: str) -> str:
    """Attribute name a fragment key takes on the negotiated observation.

    ``browser.dom`` → ``dom``, so an agent reads ``obs.dom`` whichever
    capability resolved it. Three places need this answer — the model
    factory, ``EnvironmentBase.observe`` and the loop's fragment
    population — and they were each spelling out the same ``rsplit``.
    Two active fragments cannot collapse to one name here:
    ``negotiate_observation`` refuses that combination by name before the
    run starts.
    """
    return fragment_key.rsplit(FRAGMENT_KEY_SEPARATOR, 1)[-1]


# Bridge resolves BaseModel (ModelMetaclass) + AutoRegistrar metaclass conflict.
# Subclasses of FragmentBase auto-register AND are valid Pydantic models.
_BridgeBase = ObservationRegistrar.bridge(BaseModel)


class FragmentBase(_BridgeBase):  # type: ignore[misc]
    """Concrete base for all observation fragment implementations.

    Subclasses must set:

    - ``__registry_key__``: the fully-qualified conscribe registration
      key, ``"{source}.{local_id}"`` (e.g. ``"browser.dom"``). Consumers
      do not have to spell the prefix out — an agent needing a screenshot
      writes ``"screenshot"`` in its ``__wiring__`` and negotiation binds
      it to whichever capability this run holds.
    - ``__source__``: ``"env"`` or ``"loop"``

    Example::

        class TerminalObs(FragmentBase):
            __registry_key__ = "docker.terminal"
            __source__: ClassVar[Literal["env", "loop"]] = "env"
            stdout: str
            stderr: str = ""
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    #: The fully-qualified registry key, carried **in the data**.
    #:
    #: This used to be a ``@property``, which meant ``model_dump()``
    #: omitted it and every fragment reached the trace anonymous: the WAL
    #: named the file after the negotiated local name (``000_dom.json``)
    #: and nothing on disk recorded that ``browser`` produced it. An
    #: evaluator reading a ``RunTrace`` could not tell a browser
    #: screenshot from a desktop one — which "Record Once, Evaluate Many"
    #: does not survive.
    #:
    #: Not a YAML knob: fragments are wired as a ``Literal`` of keys, so
    #: no per-fragment config class is generated and none of these fields
    #: reaches a config file. This one is provenance anyway — it is
    #: written by the run, never by a user.
    fragment_id: str = ""

    #: Unix time at which this fragment's data was captured.
    #:
    #: Stamped at construction, which for a capability that batches (the
    #: browser builds every fragment of one ``BrowserStateRequestEvent``
    #: inside one scrape) is the moment of the capture itself — not the
    #: moment the observation was assembled. That distinction is the whole
    #: point: capabilities are observed **concurrently**, so the browser
    #: may be sampled 0.5 s before the Android emulator finishes, and the
    #: agent is handed both as if they were one instant. Without a
    #: per-fragment capture time the skew is invisible in the run and
    #: unrecoverable from the trace; with it, an evaluator reading a
    #: ``RunTrace`` can see exactly how far apart the halves of a step's
    #: observation were taken.
    #:
    #: Wall clock rather than ``perf_counter``: the readers are the trace
    #: and the humans reading it, and both need it comparable to
    #: ``StepRecord.timestamp``. A fragment served twice inside one cycle
    #: keeps the capture's stamp, because it *is* that capture.
    #:
    #: Not a YAML knob — same reason as ``fragment_id``: provenance is
    #: written by the run, never by a user.
    captured_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def _default_fragment_id(self) -> Self:
        """Stamp the registry key when the caller did not supply one.

        A per-subclass default is not expressible as a field default (the
        base class does not know the subclass's key), and the value must
        survive a round trip through JSON, so it is filled in on the
        instance rather than computed on read.
        """
        if not self.fragment_id:
            key = getattr(type(self), "__registry_key__", "")
            if key:
                object.__setattr__(self, "fragment_id", key)
        return self

    # ── trace persistence contract ────────────────────────────
    #
    # A fragment owns how it lands on disk — its JSON shape as much as
    # its file layout. **What shape that is, is the fragment's business.**
    # This base class prescribes none and names no attribute of any
    # particular domain; a fragment whose payload needs domain knowledge
    # to encode (a cyclic DOM graph, a sparse framebuffer) carries a
    # ``field_serializer`` of its own and lives in the package that owns
    # the type it is encoding. Nothing here should ever grow a list of
    # some other package's field names.
    #
    # Binary payloads (screenshots, audio, video frames) must NOT be
    # base64'd into the JSON record: it inflates the file ~33%, and every
    # reader that globs for the real extension silently finds nothing. A
    # fragment that carries bytes overrides ``trace_binary`` to hand them
    # over verbatim and ``trace_json_exclude`` to keep them out of the
    # JSON sidecar.
    #
    # ``from_trace`` is the inverse of the two: what a reader calls to get
    # the fragment back from the sidecar and the artifact. A fragment that
    # overrides ``trace_binary`` overrides it too, and one whose JSON shape
    # is its own (a ``field_serializer`` encoding a graph) owns the decoder
    # for that shape — so no reader ever writes a second one.

    def trace_binary(self) -> tuple[str, bytes] | None:
        """Binary artifact as ``(file_suffix, payload)``, or ``None``.

        The suffix includes the dot (e.g. ``".png"``). The writer stores it
        beside the JSON sidecar under the same step + fragment key.
        """
        return None

    def trace_json_exclude(self) -> set[str]:
        """Field names to omit from the JSON sidecar.

        Use for anything already persisted by :meth:`trace_binary`.
        """
        return set()

    @classmethod
    def from_trace(cls, payload: dict[str, Any], artifact: bytes | None) -> Self:
        """Rebuild the fragment a trace recorded — the inverse of the two hooks above.

        *payload* is the JSON sidecar as written,
        ``model_dump(mode="json", exclude=trace_json_exclude())``, and
        *artifact* is the bytes :meth:`trace_binary` returned, or ``None``
        when it returned ``None``. Reading the type that was written back
        as that type, rather than as a ``dict``, is the reader's contract
        (``docs/architecture_design.md`` §5.5): a field name guessed off a
        JSON file passes every static check and fails at run time.

        This default serves a fragment with no binary payload. It refuses
        an artifact instead of dropping it: bytes here mean the class
        overrides :meth:`trace_binary` without overriding this, and
        validating the sidecar alone would hand back a fragment with its
        payload silently missing.

        Raises:
            ValueError: The payload or the artifact cannot be this
                fragment — pydantic's ``ValidationError`` for a sidecar of
                the wrong shape, or an artifact this class never writes.
        """
        if artifact is not None:
            raise ValueError(
                f"{cls.__name__} ({getattr(cls, '__registry_key__', '?')!r}) reads no binary "
                f"artifact back, but was handed one ({len(artifact)} bytes). A fragment that "
                f"overrides trace_binary() must override from_trace() to put the bytes back; "
                f"otherwise the trace names an artifact this fragment never writes."
            )
        return cls.model_validate(payload)

    def summary(self) -> str:
        """One-line human-readable digest for ``StepRecord.observation_summary``.

        Empty means "nothing worth summarising" and the fragment is
        omitted from the step's summary line.
        """
        return ""
