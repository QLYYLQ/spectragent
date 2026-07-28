"""Observation Fragment registrar — conscribe-driven auto-registration.

Uses ``bridge(BaseModel)`` to resolve the metaclass conflict between
Pydantic's ``ModelMetaclass`` and conscribe's ``AutoRegistrar``.
Validated by conscribe's own test suite (test_pydantic_generic.py:183-239).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from conscribe import create_registrar
from pydantic import BaseModel

from spectragent.schemas.observation.base import ObservationFragment

ObservationRegistrar = create_registrar(
    "observation",
    ObservationFragment,
    discriminator_field="fragment_id",
)

# Bridge resolves BaseModel (ModelMetaclass) + AutoRegistrar metaclass conflict.
# Subclasses of FragmentBase auto-register AND are valid Pydantic models.
_BridgeBase = ObservationRegistrar.bridge(BaseModel)


class FragmentBase(_BridgeBase):  # type: ignore[misc]
    """Concrete base for all observation fragment implementations.

    Subclasses must set:
    - ``__registry_key__``: conscribe registration key (e.g. ``"dom"``)
    - ``__source__``: ``"env"`` or ``"loop"``

    Example::

        class TerminalObs(FragmentBase):
            __registry_key__ = "terminal"
            __source__: ClassVar[Literal["env", "loop"]] = "env"
            stdout: str
            stderr: str = ""
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    @property
    def fragment_id(self) -> str:
        return self.__registry_key__

    # ── trace persistence contract ────────────────────────────
    #
    # A fragment owns how it lands on disk. Binary payloads (screenshots,
    # audio, video frames) must NOT be base64'd into the JSON record: it
    # inflates the file ~33%, and every reader that globs for the real
    # extension silently finds nothing. A fragment that carries bytes
    # overrides ``trace_binary`` to hand them over verbatim and
    # ``trace_json_exclude`` to keep them out of the JSON sidecar.

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

    def summary(self) -> str:
        """One-line human-readable digest for ``StepRecord.observation_summary``.

        Empty means "nothing worth summarising" and the fragment is
        omitted from the step's summary line.
        """
        return ""
