"""Errors raised while assembling one observation.

Separate module rather than a class in ``_registrar.py`` for the same
reason ``core/loops/errors.py`` exists: an evaluator or a hook may want to
catch this without importing the registrar (and the metaclass, and every
capability it drags in).
"""

from __future__ import annotations

from collections.abc import Mapping


class ObservationFailedError(Exception):
    """A capability failed to produce a fragment the run declared required.

    The environment resolves capabilities concurrently and isolates their
    failures, so by the time this is raised every other capability has
    already finished — the message therefore reports the *complete*
    failure set, not the first one to land, and says which capability owed
    each fragment.

    A failure touching only **optional** fragments never reaches here: it
    is recorded in the observation as a
    :class:`~spectragent.schemas.observation.failure.FragmentFailure` and
    the step continues. The split is exactly what the agent and loop
    declared in ``__wiring__``, carried through negotiation as
    ``ObservationNegotiationResult.required_fragments``. Nothing here
    decides it.
    """

    def __init__(
        self,
        cycle: int,
        failures: Mapping[str, BaseException],
        owners: Mapping[str, str],
        required: frozenset[str],
    ) -> None:
        def _line(key: str) -> str:
            exc = failures[key]
            owner = owners.get(key, "?")
            tag = "required" if key in required else "optional"
            return f"{key} ({tag}, from {owner}): {type(exc).__name__}: {exc}"

        detail = "; ".join(_line(key) for key in sorted(failures))
        fatal = sorted(set(failures) & required)
        super().__init__(
            f"Observation cycle {cycle} could not produce required fragment(s) "
            f"{fatal}. All failures this cycle: {detail}."
        )
        self.cycle = cycle
        self.failures = dict(failures)
        self.owners = dict(owners)
        self.required = required
        self.fatal = tuple(fatal)


class UnroutableFragmentError(Exception):
    """Negotiation activated an env fragment no capability in this env owns.

    Not a capture failure — a wiring inconsistency between the negotiation
    result and the capability set actually assembled, which can only
    happen when a caller hands ``observe()`` a result built against a
    different environment. It used to surface as a pydantic
    ``ValidationError`` about a missing model field, several layers from
    the cause.
    """

    def __init__(self, missing: frozenset[str], provided: frozenset[str]) -> None:
        super().__init__(
            f"No capability in this environment resolves observation "
            f"fragment(s) {sorted(missing)}. This environment provides: "
            f"{sorted(provided)}. The negotiation result was built against a "
            f"different environment."
        )
        self.missing = missing
        self.provided = provided
