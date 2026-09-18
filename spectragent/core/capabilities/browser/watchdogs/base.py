"""BrowserWatchdog — the registrar-visible Resolver contract.

A watchdog is the Browser Capability's **Resolver**. It owns one CDP
concern and, when that concern produces an observation fragment, names
it in :attr:`BrowserWatchdog.provides`.

``agent_cdp`` already ships the registry (``WatchdogRegistrar``, keyed on
``watchdog_type`` with the ``Watchdog`` suffix stripped), so subclassing
is registration — ``DOMWatchdog`` is reachable as ``"dom"`` without any
hand-maintained table. This module adds the three things the registrar
itself does not model:

- :attr:`~BrowserWatchdog.level` — which scope the watchdog binds to.
- :attr:`~BrowserWatchdog.provides` — which fragments it resolves.
- :meth:`~BrowserWatchdog.build` — construction from a single
  :class:`WatchdogContext` instead of a bespoke per-watchdog lambda.

Consequence: adding an observation fragment is **one class**. Register a
watchdog, declare ``provides``, implement ``build``. ``BrowserCapability``
never names a watchdog and does not change.

``detach()`` is also declared here rather than probed for with
``getattr``: ``agent_cdp``'s ``WatchdogProtocol`` only requires
``attach``, and a watchdog that opens its own ``CDPEventBridge`` must be
able to release it on ``close_tab`` / ``teardown``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from agent_cdp import WatchdogBase, WatchdogRegistrar

from spectragent.core.capabilities.browser.dom.budget import DOMBudget

if TYPE_CHECKING:
    from agent_cdp import CDPCommandProtocol

    from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerProtocol
    from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap
    from spectragent.core.capabilities.browser.dom.serializers import (
        DOMSerializerProtocol,
    )
    from spectragent.core.capabilities.browser.input import (
        MouseTrajectoryProtocol,
        TypingRhythmProtocol,
    )
    from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig

#: Which EventScope a watchdog binds to. ``"tab"`` watchdogs are built
#: once per attached tab; ``"browser"`` watchdogs once on the global scope.
WatchdogLevel = Literal["tab", "browser"]


async def _no_tabs() -> list[dict[str, Any]]:
    return []


def _no_selector_map() -> DOMSelectorMap:
    return {}


def _no_main_frame_id() -> str | None:
    return None


def _discard_selector_map(_selector_map: DOMSelectorMap) -> None:
    return None


@dataclass(frozen=True)
class WatchdogContext:
    """Everything a watchdog may need, assembled once by the capability.

    This replaces the per-watchdog closure/lambda table. A new watchdog
    reads whatever fields it needs off the context; the capability's
    construction code is identical for all of them, so it does not grow
    when a watchdog is added.

    The ``*_getter`` / ``*_setter`` callables exist because two watchdogs
    share state the capability owns: ``DOMWatchdog`` produces the
    per-tab selector map and ``ActionWatchdog`` consumes it to find the
    element an event names — its ``backend_node_id``, the session that owns
    it and the frames around it.
    """

    cdp: CDPCommandProtocol
    profile: BrowserProfileConfig
    viewport: tuple[int, int] = (1280, 720)

    #: CDP session id of the tab this watchdog serves. ``None`` for
    #: browser-level watchdogs.
    cdp_session_id: str | None = None

    debug_highlight: bool = False
    #: The configured trajectory *instance* — already carrying its own
    #: knobs, because the Composer built it from the capability's
    #: ``mouse_trajectory`` config block. It is the only humanization
    #: switch: ``instant`` (or ``None``, nothing configured) is the legacy
    #: instant jump, anything else humanizes.
    mouse_trajectory: MouseTrajectoryProtocol | None = None

    #: The configured typing rhythm *instance*, same route and same rule as
    #: ``mouse_trajectory`` and on its own axis: ``instant`` (or ``None``,
    #: nothing configured) is the legacy bulk ``Input.insertText``, anything
    #: else dispatches real per-character key events on modelled timings.
    typing_rhythm: TypingRhythmProtocol | None = None

    #: The configured DOM cleaning strategy *instance*, for the same reason
    #: and by the same route as ``mouse_trajectory``: the Composer built it
    #: from the capability's ``dom_cleaner`` config block, so its knobs are
    #: already on it. ``None`` only when the capability was constructed by
    #: hand; ``DOMWatchdog`` then default-constructs the default strategy.
    dom_cleaner: DOMCleanerProtocol | None = None

    #: The configured DOM serializer *instance* — third strategy on this
    #: route and for the third time the same reason. It used to be
    #: ``profile.serializer``, a registry key, which meant ``DOMWatchdog``
    #: did ``DOMSerializerRegistrar.get(name)()`` and
    #: ``TextSerializer.max_text_length`` / ``HTMLSerializer.max_href_length``
    #: were configurable in name only. ``None`` only for a hand-built
    #: capability.
    dom_serializer: DOMSerializerProtocol | None = None

    #: How much the ``browser.dom`` fragment is allowed to cost. Scalars,
    #: not a strategy — the DOM half of ``screenshot_format`` /
    #: ``screenshot_quality``. Default is unbudgeted.
    dom_budget: DOMBudget = DOMBudget()

    #: Encoding of the ``browser.screenshot`` *observation* — a scalar, not
    #: a strategy, because there is nothing to swap: it is one CDP
    #: parameter. ``png`` is the default and byte-identical to the
    #: pre-config behaviour. The ``browser.screenshot`` action keeps its
    #: own per-call format/quality; these decide only what every observe
    #: ships to the model.
    screenshot_format: Literal["png", "jpeg"] = "png"
    #: Lossy-encoder quality 1-100. Ignored for ``png``.
    screenshot_quality: int = 80

    selector_map_getter: Callable[[], DOMSelectorMap] = field(
        default=_no_selector_map
    )
    selector_map_setter: Callable[[DOMSelectorMap], None] = field(
        default=_discard_selector_map
    )
    main_frame_id_getter: Callable[[], str | None] = field(
        default=_no_main_frame_id
    )
    #: Enumerate open tabs. Browser-level state, so the capability owns
    #: it and lends it to whichever watchdog reports tab metadata.
    tabs_getter: Callable[[], Awaitable[list[dict[str, Any]]]] = field(
        default=_no_tabs
    )


class BrowserWatchdog(WatchdogBase):
    """Base for every watchdog the Browser Capability installs.

    Subclassing registers the class under ``WatchdogRegistrar`` with the
    ``Watchdog`` suffix stripped and snake_cased — ``DOMWatchdog`` →
    ``"dom"``, ``BrowserMetaWatchdog`` → ``"browser_meta"``.
    """

    __abstract__ = True

    #: Scope this watchdog binds to.
    level: ClassVar[WatchdogLevel] = "tab"

    #: Fully-qualified observation fragment keys this watchdog resolves.
    #: Empty for behavioural watchdogs (action, security, popups, crash)
    #: — those execute actions or guard navigation, they observe nothing.
    provides: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def build(cls, ctx: WatchdogContext) -> BrowserWatchdog:
        """Construct an instance from the injection context."""
        raise NotImplementedError(
            f"{cls.__name__} is registered as a browser watchdog but does "
            f"not implement build(ctx). Every watchdog must be "
            f"constructible from a WatchdogContext alone."
        )

    def detach(self) -> None:
        """Release CDP callbacks this watchdog opened. Idempotent.

        No-op by default — only watchdogs that open their own
        ``CDPEventBridge`` need to override it. Declared here so
        ``close_tab`` / ``teardown`` can call it unconditionally.
        """
        return None


def browser_watchdog_classes(
    exclude: Iterable[str] = (),
) -> dict[str, type[BrowserWatchdog]]:
    """Registered browser watchdogs by registry key, minus ``exclude``.

    Only :class:`BrowserWatchdog` subclasses are returned — a watchdog
    registered by some other consumer of ``agent_cdp`` is not ours to
    install.
    """
    excluded = frozenset(exclude)
    return {
        name: cls
        for name, cls in sorted(WatchdogRegistrar.get_all().items())
        if isinstance(cls, type)
        and issubclass(cls, BrowserWatchdog)
        and name not in excluded
    }


def validate_watchdog_names(names: Iterable[str]) -> frozenset[str]:
    """Raise naming the offending watchdog if any name is unregistered.

    A typo used to be a ``logger.warning`` that let the run continue with
    the fragment unresolved, surfacing much later as a pydantic
    ``ValidationError`` about a *fragment* — pointing at the wrong layer
    entirely. It is a hard error now.
    """
    known = browser_watchdog_classes()
    unknown = sorted(set(names) - set(known))
    if unknown:
        raise ValueError(
            f"Unknown browser watchdog(s): {unknown}. "
            f"Registered watchdogs: {sorted(known)}."
        )
    return frozenset(names)


def fragments_provided_by(
    watchdogs: Iterable[type[BrowserWatchdog]],
) -> frozenset[str]:
    """Union of ``provides`` across watchdog classes."""
    provided: set[str] = set()
    for cls in watchdogs:
        provided |= cls.provides
    return frozenset(provided)
