"""ObservationFragment Protocol — semantic unit of observation data.

Each Fragment is an independently registered, semantically indivisible group
of fields. Fragments declare their ``__source__`` to indicate who fills them:

- ``"env"``  — filled by a Capability's Resolver (e.g. DOM from BrowserCapability)
- ``"loop"`` — filled by the Loop itself (e.g. ActionResult from ReactLoop)
"""

from __future__ import annotations

from typing import ClassVar, Literal, Protocol, runtime_checkable


@runtime_checkable
class ObservationFragment(Protocol):
    """Contract for all observation fragments."""

    fragment_id: str
    """Registry discriminator — matches ``__registry_key__``."""

    __source__: ClassVar[Literal["env", "loop"]]
    """Who provides this fragment: ``"env"`` (Capability) or ``"loop"`` (Loop itself)."""
