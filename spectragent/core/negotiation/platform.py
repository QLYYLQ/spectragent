"""Whether an agent's prompt can render the input platform its run negotiated.

The ``platform`` fragment says which operating system an agent's input
reaches (:data:`~spectragent.schemas.observation.platform.InputPlatform`), and
an agent that renders it states it in its prompt with the keyboard rules that
hold there (``agents/rendering/platform.py``). Two class declarations, both
fixed before a run starts, say whether that can work:

- the capability's: its platform fragment's ``operating_system`` is a closed
  ``Literal`` of the platforms it can report (the browser's ``macos``,
  ``windows`` and ``linux``);
- the agent's: the platforms its prompt renders, taken from the renderer it
  calls::

      __input_platforms__: ClassVar[frozenset[InputPlatform]] = COMPUTER_USE_PLATFORMS

:func:`check_input_platform` compares them in ``AgentRuntime.prepare()``, so a
fragment that may report a platform the agent cannot render is refused there,
by name, before a trace is opened and before the environment is set up
(AGENTS.md §2.3, "What setup would refuse, ``prepare()`` refuses"), not at the
first prompt after setup and a first screenshot. The renderer's own
``ValueError`` stays as the backstop for an observation built outside a run.
An agent that declares nothing is not checked; every registered agent that
renders the platform declares it (``tests/test_agents/test_rendering/test_platform.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, get_args, get_origin

from spectragent.schemas.observation._registrar import ObservationRegistrar
from spectragent.schemas.observation.platform import InputPlatform

#: The local name of the fragment that reports the input platform.
PLATFORM_FIELD = "platform"


class IncompatiblePlatformError(TypeError):
    """A run whose platform fragment may report a platform its agent's prompt cannot render.

    Raised by :func:`check_input_platform` in ``AgentRuntime.prepare()``. A
    ``TypeError`` like ``ScreenshotGeometryError``: two class declarations
    disagree, which no step of the run can repair.
    """


def reported_platforms(fragment: type | None) -> tuple[str, ...] | None:
    """The platforms a platform fragment class may report: the values of its ``operating_system`` Literal.

    ``None`` when *fragment* is no class, has no ``operating_system`` field,
    or the field is not a closed ``Literal`` (``str``, or a ``Literal`` that
    also admits ``None``): then nothing says before a run what it reports.
    """
    field = getattr(fragment, "model_fields", {}).get("operating_system")
    annotation = getattr(field, "annotation", None)
    if get_origin(annotation) is not Literal:
        return None
    return get_args(annotation)


def _listed(platforms: Iterable[str]) -> str:
    """*platforms* in :data:`InputPlatform`'s order (any other last), comma-separated."""
    order = get_args(InputPlatform)
    return ", ".join(
        sorted(platforms, key=lambda p: (order.index(p) if p in order else len(order), p))
    )


def check_input_platform(agent: Any, fragment_bindings: Mapping[str, str], *, name: str) -> None:
    """Refuse a run whose platform fragment may report a platform *agent*'s prompt cannot render.

    Nothing to check when *agent*'s class declares no ``__input_platforms__``,
    or the run negotiates no platform fragment.

    Args:
        agent: The configured agent; its class carries ``__input_platforms__``.
        fragment_bindings: The observation negotiation's local name →
            fully-qualified key (``ObservationNegotiationResult.fragment_bindings``).
        name: How the refusal names the agent.

    Raises:
        IncompatiblePlatformError: The declaration is not a set of platform
            names; the negotiated fragment is no registered class whose
            ``operating_system`` is a closed ``Literal``; or that ``Literal``
            holds a platform the declaration does not.
    """
    declared = getattr(type(agent), "__input_platforms__", None)
    key = fragment_bindings.get(PLATFORM_FIELD)
    if declared is None or key is None:
        return
    if not isinstance(declared, (set, frozenset)) or not all(isinstance(p, str) for p in declared):
        raise IncompatiblePlatformError(
            f"{name}: __input_platforms__ must be a set of input platform names, got {declared!r}"
        )
    fragment = ObservationRegistrar.get_or_none(key)
    shown = fragment.__name__ if fragment is not None else "no registered fragment class"
    reported = reported_platforms(fragment)
    if reported is None:
        raise IncompatiblePlatformError(
            f"{name} renders the platform fragment this run negotiates, {key} ({shown}), whose "
            f"operating_system is not a closed Literal, so which platforms it may report cannot "
            f"be checked before the run. A platform fragment declares operating_system as a "
            f"Literal of the platforms its capability can report (InputPlatform, "
            f"spectragent.schemas.observation.platform)."
        )
    unrenderable = set(reported) - declared
    if unrenderable:
        raise IncompatiblePlatformError(
            f"{name}'s prompt renders the input platforms {_listed(declared)}, but the platform "
            f"fragment this run negotiates, {key} ({shown}), may report {_listed(unrenderable)}. "
            f"Pair the agent with a capability whose platform it renders, or teach its renderer "
            f"(agents/rendering/platform.py) that platform and declare it in __input_platforms__."
        )


__all__ = [
    "PLATFORM_FIELD",
    "IncompatiblePlatformError",
    "check_input_platform",
    "reported_platforms",
]
