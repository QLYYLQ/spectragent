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
