"""DOM cleaning strategy registry.

Three strategies registered via conscribe:
- ``browser_use``: 5-stage pipeline (simplify → optimize → paint-order → bounds → index)
- ``skyvern``:     original page-side JS extraction + server-side trim
- ``skyvern_hit_test``: ``skyvern`` plus an in-page hit test per indexed element
  (``covered_elements: drop | keep``; arch §4.12)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent_cdp import CDPCommandProtocol
from conscribe import create_registrar

from spectragent.core.capabilities.browser.dom.capture import FrameTargets
from spectragent.core.capabilities.browser.dom.models import (
    DOMSelectorMap,
    DOMState,
    TargetCapture,
)


@dataclass(frozen=True, slots=True)
class DOMCaptureSource:
    """Live, tab-bound capture abilities passed to one cleaning strategy.

    The configured cleaner is shared by the capability. A source is created
    for each observation, so no tab's CDP session or event scope is retained
    on that configured instance.
    """

    cdp: CDPCommandProtocol
    session_id: str | None
    frames: FrameTargets | None
    run_js: Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any] | None]]
    js_click_listeners: bool = False
    frame_timeout_s: float = 2.0


@runtime_checkable
class DOMCleanerProtocol(Protocol):
    """Strategy that selects capture work and converts it to DOMState."""

    async def capture(self, source: DOMCaptureSource) -> tuple[TargetCapture, float]:
        """Choose and run the CDP, JS, or combined capture for this strategy."""
        ...

    async def clean(
        self,
        capture: TargetCapture,
        viewport: tuple[int, int],
        *,
        previous_selector_map: DOMSelectorMap | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> DOMState:
        """Clean and filter raw CDP DOM data into a DOMState.

        Parameters
        ----------
        capture:
            The strategy's capture for the tab and its frames. BrowserUse
            fills raw CDP document/snapshot/AX fields; Skyvern fills a JS
            tree and CDP-resolved node references. A cleaner that indexes
            frame content must use ``dom.models.element_handle`` because
            backend node ids of different targets collide.
        viewport:
            ``(width, height)`` in CSS pixels.
        previous_selector_map:
            Previous selector_map for new-node tracking. ``None`` on first call.
        device_pixel_ratio:
            Live ``window.devicePixelRatio`` for the target.  CDP
            ``DOMSnapshot`` returns ``bounds`` in **device pixels**;
            divide by DPR to recover CSS pixels expected by every
            downstream consumer (click coords, occlusion, serialization).
            Default ``1.0`` keeps tests and DPR-1 environments unchanged.
        """
        ...


DOMCleanerRegistrar = create_registrar(
    "dom_cleaner",
    DOMCleanerProtocol,
    discriminator_field="cleaner_type",
)


class DOMCleanerBase(metaclass=DOMCleanerRegistrar.Meta):
    """Base for DOM cleaning strategies — subclass to auto-register."""

    __abstract__ = True
    __registry_key__: str

    @property
    def cleaner_type(self) -> str:
        return self.__registry_key__


def default_cleaner() -> DOMCleanerProtocol:
    """The cleaner used when no configuration named one.

    Same contract and same reason as
    :func:`~spectragent.core.capabilities.browser.dom.serializers.default_serializer`:
    ``BrowserCapability.__init__`` and ``DOMWatchdog.__init__`` both need
    a default and must not disagree about it. Not a registry lookup — the
    receptor is the configuration surface, this is just the framework's
    answer when nobody configured anything.
    """
    from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
        BrowserUseCleaner,
    )

    return BrowserUseCleaner()
