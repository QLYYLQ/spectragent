"""Archive negotiation — archive consumers ↔ Environment.

The third provides axis (``docs/architecture_design.md`` §4.5), negotiated
like the other two: capabilities declare the archivers they provide
(``__wiring__["archiver"]``, aggregated by ``Environment.provided_archivers``),
and the result is a binding table downstream reads instead of working the
answer out again.

**The one asymmetry is where the demand comes from.** Observation and action
demands are class declarations, capability-relative, bound to a provider
here. An archive demand is the trace hook *instance's* ``archive`` list —
what a run records is a per-run choice (the user's ruling of 2026-10-09, AGENTS.md §2.5), neither an agent
class's domain knowledge nor a property of the recorder class — and it is
always fully qualified: the archiver's own ``archiver_id`` names its
provider. So there is no relative key to resolve, and the rules are a
plain routing check.

This module validates only. ``AgentRuntime.prepare`` runs it right after
memory negotiation, so a binding error surfaces during assembly (a dry run
included); it binds — ``env.bind_archivers`` — as the last step of
``prepare``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.archive._registrar import ARCHIVER_KEY_SEPARATOR, Archiver
from spectragent.core.archive.protocols import ArchivingCapability
from spectragent.core.negotiation._base import demand_label
from spectragent.core.negotiation._registrar import NegotiationResultBase
from spectragent.core.negotiation.engine import negotiate


class IncompatibleArchiveError(Exception):
    """The archivers a run asks for cannot be bound to its environment.

    Every problem is collected and reported at once — one line each,
    sorted, so two processes print the same message — because a
    fix-and-rerun per problem is the experience the other negotiation
    errors stopped giving. A line names both parties: the requesting hook
    (``demand_label``: ``trace_writer``) and what the environment and the
    capability actually provide.

    Isomorphic to
    :class:`~spectragent.core.negotiation.observation.IncompatibleFragmentError`.

    Attributes:
        missing: Requested keys no capability here provides — the prefix
            names no capability, or the capability does not provide the
            key. With no environment, every requested key. Duplicate and
            multi-consumer problems appear in the message only.
        provided: Every archiver the environment provides (empty without
            an environment).
        requested_by: Requested key → the labels of the consumers that
            requested it, in consumer order; keys sorted.
    """

    def __init__(
        self,
        problems: Sequence[str],
        *,
        missing: frozenset[str],
        provided: frozenset[str],
        requested_by: Mapping[str, Sequence[str]],
    ) -> None:
        # Sorted, and each distinct line once: two consumers carrying the
        # same label can produce the same sentence, and saying it twice
        # adds nothing.
        lines = tuple(sorted(set(problems)))
        super().__init__("\n".join(lines))
        self.problems = lines
        self.missing = missing
        self.provided = provided
        self.requested_by = {key: tuple(labels) for key, labels in sorted(requested_by.items())}


@dataclass(frozen=True)
class ArchiveNegotiationResult(NegotiationResultBase):
    """What the archive negotiation bound, for this run.

    Attributes:
        active_archivers: Fully-qualified keys of the archivers the run
            binds — exactly the consumer's ``archive`` list.
        archive_bindings: Archiver key → the ``capability_id`` (the
            capability's registry key, not its instance id) that provides
            it. Values are registry keys, which is why the trace records
            the table verbatim.
    """

    __registry_key__ = "archive"

    active_archivers: frozenset[str]
    archive_bindings: Mapping[str, str] = field(default_factory=dict)


def _sorted_list(keys: Iterable[str]) -> list[str]:
    return sorted(set(keys))


def _archive_rules(
    *,
    consumers: Sequence[tuple[str, Sequence[str]]],
    env_label: str | None,
    capabilities: Sequence[tuple[str, frozenset[str]]],
    non_blocking: Sequence[str] = (),
) -> ArchiveNegotiationResult:
    """The routing rules, on plain data.

    Args:
        consumers: ``(label, requested keys in list order)`` per consumer,
            in consumer order. Never empty.
        env_label: The environment's ``env_type``; ``None`` when the run
            has no environment.
        capabilities: ``(capability_id, provided archivers)`` per
            capability, in capability order. A capability that does not
            archive provides ``frozenset()``.
        non_blocking: The labels of the consumers that are not blocking
            (``blocking=False``).

    Raises:
        IncompatibleArchiveError: With every problem found.
    """
    problems: list[str] = []
    provides = dict(capabilities)
    env_provided = frozenset(key for keys in provides.values() for key in keys)

    requested_by: dict[str, list[str]] = {}
    for label, keys in consumers:
        for key in dict.fromkeys(keys):
            requested_by.setdefault(key, []).append(label)

    # A consumer that does not block: its handlers finish after the commit
    # point (§4.6 "blocking 的唯一语义 = 并发"), so the evidence it pulls
    # would reach no trace.
    for label, keys in consumers:
        if label in non_blocking:
            problems.append(
                f"{label} requests archivers {_sorted_list(keys)} but is not blocking "
                f"(blocking=False); a non-blocking hook's handlers finish after the step is "
                f"committed, so the evidence it pulls would reach no trace: an archive "
                f"consumer must be blocking."
            )

    # A list naming one archiver twice.
    for label, keys in consumers:
        for key, count in Counter(keys).items():
            if count > 1:
                problems.append(
                    f"{label} lists archiver {key!r} more than once; an archive list names "
                    f"each archiver at most once."
                )

    # One archiver in two consumers' lists.
    for key, labels in requested_by.items():
        if len(labels) > 1:
            problems.append(
                f"archiver {key!r} is requested by more than one hook ({', '.join(labels)}); "
                f"a run binds each archiver once."
            )

    # More than one consumer at all — v1 binds the archive of one.
    if len(consumers) > 1:
        detail = "; ".join(f"{label}: {_sorted_list(keys)}" for label, keys in consumers)
        problems.append(
            f"more than one hook requests archivers ({detail}); a run binds the archive "
            f"of at most one hook."
        )

    missing: set[str] = set()
    if env_label is None:
        for label, keys in consumers:
            problems.append(
                f"{label} requests archivers {_sorted_list(keys)}, but this run has no environment."
            )
            missing.update(keys)
    else:
        for label, keys in consumers:
            for key in dict.fromkeys(keys):
                prefix = key.partition(ARCHIVER_KEY_SEPARATOR)[0]
                if prefix not in provides:
                    problems.append(
                        f"{label} requests archiver {key!r}, but environment {env_label!r} "
                        f"has no capability {prefix!r} (its capabilities: {sorted(provides)})."
                    )
                    missing.add(key)
                elif key not in provides[prefix]:
                    declared = sorted(provides[prefix]) if provides[prefix] else "none"
                    problems.append(
                        f"{label} requests archiver {key!r}, but environment {env_label!r} "
                        f"provides archivers: {sorted(env_provided)} "
                        f"(capability {prefix!r} declares {declared})."
                    )
                    missing.add(key)

    if problems:
        raise IncompatibleArchiveError(
            problems,
            missing=frozenset(missing),
            provided=env_provided if env_label is not None else frozenset(),
            requested_by=requested_by,
        )

    _label, keys = consumers[0]
    return ArchiveNegotiationResult(
        active_archivers=frozenset(keys),
        archive_bindings={
            key: key.partition(ARCHIVER_KEY_SEPARATOR)[0] for key in sorted(set(keys))
        },
    )


def negotiate_archive(consumers: Sequence[Any], env: Any) -> ArchiveNegotiationResult:
    """Check that *consumers*' archivers can be bound to *env*; return the binding.

    Args:
        consumers: The hooks whose ``archive`` is non-empty — the runtime
            filters on :class:`~spectragent.core.archive.protocols.ArchiveConsumer`
            before calling. Compared by identity: the same object listed
            twice is one consumer, while two distinct consumers stay two
            even if they compare equal.
        env: The run's environment, or ``None``.

    Returns:
        The archivers to bind (fully qualified) and the capability each
        binds to. Nothing is bound here.

    Raises:
        ValueError: *consumers* is empty — a programming error: with no
            archive to bind, the runtime does not negotiate at all.
        TypeError: An ``archive`` item is not an archiver (a raw dict, a
            name): the consumer was handed configuration, not instances.
        IncompatibleArchiveError: Every binding problem, at once — a
            consumer that is not blocking among them (a hook that does not
            say is taken as blocking, as the weaver takes it).
    """
    unique: list[Any] = []
    seen: set[int] = set()
    for consumer in consumers:
        if id(consumer) not in seen:
            seen.add(id(consumer))
            unique.append(consumer)
    if not unique:
        raise ValueError(
            "negotiate_archive needs at least one consumer with a non-empty archive; "
            "a run that archives nothing makes no archive call on its environment."
        )

    parties: list[tuple[str, tuple[str, ...]]] = []
    non_blocking: list[str] = []
    for consumer in unique:
        label = demand_label(consumer)
        keys: list[str] = []
        for item in consumer.archive:
            if not isinstance(item, Archiver):
                raise TypeError(
                    f"{label}'s archive holds a {type(item).__name__}, not an archiver: "
                    f"an archive list carries archiver instances (ArchiverBase), never "
                    f"names or raw configuration."
                )
            keys.append(item.archiver_id)
        parties.append((label, tuple(keys)))
        # A hook that does not say is blocking — the weaver's reading too
        # (``core/aop/proxy.py``).
        if not bool(getattr(consumer, "blocking", True)):
            non_blocking.append(label)

    env_label: str | None = None
    capabilities: list[tuple[str, frozenset[str]]] = []
    if env is not None:
        env_label = getattr(env, "env_type", None) or type(env).__name__
        for cap in env.capabilities.values():
            cap_id: str = cap.capability_id
            # A capability without the (optional) archive surface provides none.
            provided: frozenset[str] = (
                frozenset(cap.provided_archivers)
                if isinstance(cap, ArchivingCapability)
                else frozenset()
            )
            capabilities.append((cap_id, provided))

    return negotiate(
        ArchiveNegotiationResult,
        "archiver",
        _archive_rules,
        consumers=parties,
        env_label=env_label,
        capabilities=capabilities,
        non_blocking=non_blocking,
    )
