"""Shared primitives for observation and action negotiation.

Two things live here, and they are the halves of one idea:

- :func:`extract_wiring` reads what a class *declared* it needs.
- :func:`resolve_demand` turns those declarations into the
  fully-qualified keys of the providers that will actually satisfy them,
  given the environment assembled for this run.

The split exists because a declaration is capability-relative and a
provider is not. ``"click"`` is a statement of domain knowledge — an
agent that can click does not thereby know whether it is clicking in a
browser, a desktop VM or an Android emulator. ``browser.click`` is a
routing address. Keeping the two in one string is what used to weld an
agent to a single capability.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

from conscribe.wiring import expand_relative_keys, parse_wiring


def extract_wiring(cls: type, registry_name: str) -> tuple[set[str], set[str]]:
    """Extract ``(required, optional)`` key sets from ``cls.__wiring__``.

    Keys come back exactly as declared, which since conscribe 1.3.0 means
    they may be **capability-relative**: ``"click"`` rather than
    ``"browser.click"``. Deciding which provider a relative key means
    needs the assembled environment, which this function does not have —
    that is :func:`resolve_demand`'s job, and it is the only place that
    decision is made.

    Uses ``conscribe.wiring.parse_wiring`` so registries don't need to be
    populated at parse time. Returns empty sets when no matching wiring
    spec is found.
    """
    specs = parse_wiring(cls)
    for spec in specs:
        if spec.registry_name == registry_name:
            required = set(spec.allowed_keys) if spec.allowed_keys else set()
            optional = set(spec.optional_keys) if spec.optional_keys else set()
            return required, optional
    return set(), set()


class AmbiguousKeyError(Exception):
    """Raised when a capability-relative key has more than one provider.

    Two capabilities in one environment can legitimately offer the same
    local id (``browser.screenshot`` and ``desktop.screenshot``), and a
    declaration of ``"screenshot"`` then names no provider. Every way of
    guessing one — first in the wiring list, registration order, a
    capability-affinity table — resolves it by silently binding the run
    to a provider nobody chose, which is the failure mode this codebase
    keeps deleting. So it refuses, and says what to write instead.

    Subclasses only supply :attr:`kind`, the noun used in the message.
    """

    #: Noun for what the key names, e.g. ``"action"``.
    kind: ClassVar[str] = "key"

    def __init__(self, declared: str, candidates: Sequence[str]) -> None:
        ordered = sorted(candidates)
        super().__init__(
            f"Capability-relative {self.kind} key {declared!r} is provided by "
            f"more than one capability in this environment: {ordered}. "
            f"A relative key resolves only when exactly one capability offers "
            f"it — picking a winner here would bind the run to a provider "
            f"nobody chose. Name the one you mean by writing its "
            f"fully-qualified key in __wiring__ (one of {ordered})."
        )
        self.declared = declared
        self.candidates = tuple(ordered)


@dataclass(frozen=True)
class ResolvedDemand:
    """What a class's declarations resolved to against one environment.

    Attributes:
        bindings: Declared key → fully-qualified provider key, for every
            declaration that found exactly one provider.
        missing: Required declarations no provider satisfied, reported in
            the form they were written so the diagnosis points at the
            wiring line the user has to edit.
    """

    bindings: Mapping[str, str]
    missing: frozenset[str]

    @property
    def resolved(self) -> frozenset[str]:
        """The fully-qualified keys activated by this demand."""
        return frozenset(self.bindings.values())


def resolve_demand(
    *,
    required: Iterable[str],
    optional: Iterable[str],
    universe: Collection[str],
    separator: str,
    ambiguity_error: type[AmbiguousKeyError],
) -> ResolvedDemand:
    """Bind declared keys to the providers present in *universe*.

    *universe* is the fully-qualified key set this run can actually
    serve — the environment's provided keys, plus whatever the framework
    itself supplies. Scoping resolution to it, rather than to the whole
    registry, is what makes ``"click"`` mean the one click this run has:
    a registry-wide match would go ambiguous the moment an unrelated
    capability is imported.

    Relative and fully-qualified declarations are separated by
    :func:`conscribe.wiring.expand_relative_keys`, the same rule conscribe
    applies at config-generation time — a key carrying the separator is
    passed through untouched (so it pins its provider, including when a
    relative form would have been ambiguous), a key without one expands
    to every provider whose trailing segment matches.

    Raises:
        ambiguity_error: A relative key matched more than one provider.
            Required and optional keys alike: an optional key with two
            providers is a config the user must decide, not one to drop.
    """
    required = set(required)
    optional = set(optional)
    ordered_universe = sorted(universe)

    bindings: dict[str, str] = {}
    missing: set[str] = set()
    # Sorted so two processes diagnose the same declaration first.
    for declared in sorted(required | optional):
        candidates = [
            key
            for key in expand_relative_keys([declared], ordered_universe, separator)
            if key in universe
        ]
        if len(candidates) > 1:
            raise ambiguity_error(declared, candidates)
        if not candidates:
            if declared in required:
                missing.add(declared)
            continue
        bindings[declared] = candidates[0]

    return ResolvedDemand(bindings=bindings, missing=frozenset(missing))


def group_by_short_name(
    keys: Collection[str], separator: str
) -> dict[str, list[str]]:
    """Group fully-qualified keys by their trailing segment.

    One computation, two readers: :func:`bind_short_names` takes the
    unambiguous groups, and observation negotiation takes the ambiguous
    ones — a local id claimed twice cannot be delivered, because the
    generated observation model has one attribute per local name.
    """
    by_short: dict[str, list[str]] = {}
    for key in keys:
        by_short.setdefault(key.rsplit(separator, 1)[-1], []).append(key)
    return {short: sorted(group) for short, group in by_short.items()}


def bind_short_names(grouped: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Reduce a :func:`group_by_short_name` result to short → full key.

    This is the run's answer to "what does ``click`` mean here", computed
    once at negotiation so no consumer has to work it out again — a
    mapping re-derived at each point of use is a mapping that eventually
    disagrees with itself.

    A short name claimed by two active keys is **omitted**: both can only
    be active because the wiring named both providers explicitly, so the
    run advertises both fully-qualified names and the bare short form
    genuinely means neither.
    """
    return {
        short: group[0]
        for short, group in grouped.items()
        if len(group) == 1 and group[0] != short
    }
