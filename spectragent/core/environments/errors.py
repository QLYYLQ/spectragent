"""Errors raised by an environment: assembling an observation, routing
and collecting environment evidence.

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


class UnroutableArchiverError(Exception):
    """``bind_archivers`` was handed an archiver this environment cannot bind.

    Raised before **any** capability is touched — binding validates every
    archiver first, so a refused call leaves every capability's binding as
    it was. Negotiation (``negotiate_archive``) already refuses the same
    configurations with an error naming the requesting hook; this is the
    environment's own guarantee for any caller that binds directly.

    Attributes:
        problems: Archiver key → why it cannot be bound (no capability with
            that prefix; the capability does not archive; the capability
            does not provide the key).
        capability_ids: The capability ids this environment has.
    """

    def __init__(self, problems: Mapping[str, str], capability_ids: frozenset[str]) -> None:
        ordered = dict(sorted(problems.items()))
        detail = "; ".join(f"{key!r}: {reason}" for key, reason in ordered.items())
        super().__init__(
            f"Cannot bind archiver(s) {sorted(ordered)} — {detail}. This environment's "
            f"capabilities: {sorted(capability_ids)}. Nothing was bound."
        )
        self.problems = ordered
        self.capability_ids = capability_ids


class ArchiveCollectionError(Exception):
    """One or more capabilities failed to hand over their evidence.

    Collection is concurrent and every capability is waited for before the
    outcome is decided, so the message reports the *complete* failure set,
    in capability order. A capability fails by raising, or by handing over
    a file whose ``capability_id`` is not its own (recorded as an
    ``ArchiveContractError``). The batches the healthy capabilities handed
    over are discarded. The archive consumer records the collection failure
    as recording-health evidence; it does not change the agent's outcome
    (``docs/architecture_design.md`` §4.6).

    Attributes:
        env_type: The environment that collected.
        failures: Capability id → its exception, in capability order. The
            first one is also this error's ``__cause__``.
    """

    def __init__(self, env_type: str, failures: Mapping[str, BaseException]) -> None:
        detail = "; ".join(
            f"{cap_id}: {type(exc).__name__}: {exc}" for cap_id, exc in failures.items()
        )
        super().__init__(
            f"Environment {env_type!r} could not collect the archive from "
            f"capability(ies) {list(failures)}: {detail}."
        )
        self.env_type = env_type
        self.failures = dict(failures)
