"""Merging the parts of a capability's configuration that a run and its benchmark write.

``docs/architecture_design.md`` §4.10.1 "capability 配置合并", §4.2 "基准层在
``prepare()`` 里加的检查". The run configuration, the benchmark and later its
verifiers each write their part of one capability's configuration in the same
schema; no field belongs to one side. A benchmark writes its part in its
block's ``capability`` list and may compute more in code (AndroidWorld: its
``open_app`` name table). Each part is checked against the capability's own
schema for what it writes (:meth:`CapabilityMerge.check`, never for what it
leaves out), then merged field by field into the run configuration's block of
that capability (:meth:`CapabilityMerge.merge`):

- a field one side writes is taken from it; one neither writes keeps the
  schema's default, and one required that neither writes is refused afterwards,
  by the Composer, naming the field;
- a field two sides write with different values is refused, naming the field
  and both sides: there is no precedence, as two providers of one action are
  refused (§4.3 "消歧靠拒绝"); a strategy block both write (``backend``) is
  merged inside when both name the same strategy, refused when they name two,
  and so is a block of settings a field is typed as (``network_source.matcher``),
  while a free-form map (``open_app``'s ``apps``) is one value, whose
  conflict names the keys the two maps differ in when either is too long to show;
- a list of strategy blocks (``actions``) is the run configuration's, or the
  whole default list when it leaves the list out: a part may set options on an
  entry already there, matched by its discriminator (``action_id``), and never
  adds or removes one (coordinator ruling 2026-11-03), since a benchmark that
  sets one action's knob would otherwise delete the others. A block that leaves
  the list out while the run writes a top-level section of its layer (``action:``)
  takes that section when it is composed; a part on such a list is refused,
  naming where to write the list instead, since its entries are not the
  default list (coordinator decision 2026-11-04).

The per-task data stays in the per-task setup channel (``TaskContext.env``)
and is never merged. The merged block is what the run validates, constructs
and records (``resolved``), so the trace and ``config_hash`` hold the merged
configuration (§5.3 "实验身份").
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import UnionType
from typing import Annotated, Union, cast, get_args, get_origin

from conscribe import LayerRegistrar
from conscribe.introspect import WiringSlot, wiring_slots
from pydantic import BaseModel, JsonValue, ValidationError
from pydantic.fields import FieldInfo

from spectragent.core.configuration_catalog import ComponentDefinition, ConfigurationCatalog

#: The top-level section of capability blocks, and an environment's receptor of them.
CAPABILITY_SECTION = "capability"
ENVIRONMENT_SECTION = "environment"
ENVIRONMENT_CAPABILITIES = "capabilities"
#: How long a value shown in a conflict may be before it is cut.
_SHOWN = 120
#: How many keys of a map in a conflict are named before the rest are counted.
_NAMED = 3


@dataclass(frozen=True)
class Contribution:
    """One side's part of a capability's configuration.

    Attributes:
        block: The part, a capability block holding only what this side writes,
            with its ``capability_id``.
        source: Who writes it, for messages: ``the benchmark androidworld``.
        path: Where it is written: ``benchmark[0].capability[0]``.
    """

    block: Mapping[str, JsonValue]
    source: str
    path: str


@dataclass(frozen=True)
class MergeProblem:
    """One problem of the merge, at the path its author wrote.

    ``code`` says which kind, the issue a refusal reports:

    - ``capability_contribution_invalid``: a part breaks the capability's
      schema, or sets one list entry's options twice;
    - ``capability_without_block``: a part of a capability the run
      configuration has no block of;
    - ``capability_setting_conflict``: two sides write one field differently;
    - ``capability_entry_not_listed``: a part sets options on a list entry the
      run configuration's list leaves out;
    - ``capability_list_in_top_level_section``: a part sets options on a list
      the run configuration writes in a top-level section, not in the block.
    """

    path: str
    message: str
    code: str


class CapabilityMergeError(ValueError):
    """The parts of a capability's configuration cannot be merged; every problem named."""

    def __init__(self, problems: Sequence[MergeProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__(
            "\n".join(f"{problem.path}: {problem.message}" for problem in self.problems)
        )


@dataclass
class _Side:
    """Who wrote a value already in the merged block."""

    source: str
    path: str

    def __str__(self) -> str:
        return f"{self.source} ({self.path})" if self.path else self.source


@dataclass
class _Block:
    """A capability block of the run configuration being merged: its path, its fields, who wrote each."""

    path: str
    fields: dict[str, JsonValue]
    #: The layers the run writes a top-level section of, with entries: a list
    #: the block leaves out takes that section's instances when it is composed.
    pooled: frozenset[str] = frozenset()
    #: Field path within the block (``backend.avd``) → who wrote it; the run
    #: configuration for what this map leaves out.
    written_by: dict[str, _Side] = field(default_factory=dict)


class CapabilityMerge:
    """The merge of capability parts into a run's capability blocks, over one Composer's catalog."""

    def __init__(
        self,
        catalog: ConfigurationCatalog,
        registrars: Mapping[str, type[LayerRegistrar[object]]],
    ) -> None:
        self._catalog = catalog
        self._registrars = registrars

    def check(self, contribution: Contribution) -> None:
        """Refuse a part that breaks its capability's schema in what it writes.

        A field it leaves out is never a problem: another side may write it.

        Raises:
            CapabilityMergeError: ``capability_contribution_invalid``, each
                problem at the path the part writes it;
                ``capability_without_block`` for a part of a capability this
                run has no schema of, so no block of either.
        """
        capability_id = contribution.block.get("capability_id")
        components = self._catalog.components.get(CAPABILITY_SECTION, {})
        if not isinstance(capability_id, str):
            raise CapabilityMergeError(
                [
                    MergeProblem(
                        f"{contribution.path}.capability_id",
                        f"{contribution.source} writes a part of a capability's configuration "
                        f"and names no capability_id",
                        "capability_contribution_invalid",
                    )
                ]
            )
        if capability_id not in components:
            # Each capability block of the run names its package, so the run
            # has none of this one: a part computed in code, for another kind of run.
            raise CapabilityMergeError([_without_block(contribution, capability_id)])
        model = components[capability_id].config_model
        try:
            model.model_validate(contribution.block)
        except ValidationError as exc:
            problems = [
                MergeProblem(
                    _joined(contribution.path, error["loc"], contribution.block),
                    f"{contribution.source} writes it: {error['msg']}",
                    "capability_contribution_invalid",
                )
                for error in exc.errors()
                if error["type"] != "missing"
            ]
            if problems:
                raise CapabilityMergeError(problems) from None

    def merge(
        self, sections: Mapping[str, object], contributions: Sequence[Contribution]
    ) -> dict[str, object]:
        """*sections* with every part of *contributions* merged into its capability's blocks.

        The run configuration's blocks are the top-level ``capability`` entries
        and each environment's ``capabilities``; a part goes into every block of
        its ``capability_id``. *sections* is not changed: the result is a copy.

        Raises:
            CapabilityMergeError: ``capability_without_block`` when a part's
                capability has no block here; otherwise every problem of the
                merge, each with its kind (:class:`MergeProblem`).
        """
        merged = copy.deepcopy(dict(sections))
        pooled = frozenset(
            name for name, value in merged.items() if isinstance(value, list) and value
        )
        blocks = [_Block(block.path, block.fields, pooled) for block in _capability_blocks(merged)]
        problems: list[MergeProblem] = []
        missing: list[MergeProblem] = []
        for contribution in contributions:
            capability_id = contribution.block.get("capability_id")
            targets = [
                block for block in blocks if block.fields.get("capability_id") == capability_id
            ]
            if not targets:
                missing.append(_without_block(contribution, capability_id))
                continue
            assert isinstance(capability_id, str)
            definition = self._catalog.components[CAPABILITY_SECTION][capability_id]
            side = _Side(contribution.source, contribution.path)
            for target in targets:
                problems += self._merge_block(
                    target,
                    target.fields,
                    contribution.block,
                    side,
                    definition.implementation,
                    definition.config_model,
                    here="",
                )
        if missing:
            raise CapabilityMergeError(missing)
        if problems:
            raise CapabilityMergeError(problems)
        return merged

    # -- one block --------------------------------------------------------

    def _merge_block(
        self,
        target: _Block,
        into: dict[str, JsonValue],
        part: Mapping[str, JsonValue],
        side: _Side,
        owner: type | None,
        model: type | None,
        *,
        here: str,
    ) -> list[MergeProblem]:
        """Merge *part* into *into* (inside *target* at *here*).

        *part* is a block of the component *owner* (``None``: a block of plain
        settings, which has no receptors), validated by *model*. A field
        *part* writes ``null`` is not written: ``null`` says an optional
        setting is not set.
        """
        problems: list[MergeProblem] = []
        slots = wiring_slots(owner) if owner is not None else {}
        for name, value in part.items():
            if value is None:
                continue
            at = f"{here}.{name}" if here else name
            existing = into.get(name)
            if existing is None:
                if isinstance(value, list) and self._list_receptor(slots.get(name)):
                    # Left out by the run configuration: the default list, set on.
                    entry = slots[name]
                    problems += self._merge_list(target, into, name, value, side, entry, at=at)
                else:
                    into[name] = copy.deepcopy(value)
                    target.written_by[at] = side
                continue
            entry = slots.get(name)
            if (
                isinstance(value, list)
                and isinstance(existing, list)
                and self._list_receptor(entry)
            ):
                assert entry is not None
                problems += self._merge_list(target, into, name, value, side, entry, at=at)
            elif (
                isinstance(value, Mapping)
                and isinstance(existing, dict)
                and self._strategy_receptor(entry)
            ):
                assert entry is not None
                problems += self._merge_strategy(target, existing, value, side, entry, at=at)
            elif (
                isinstance(value, Mapping)
                and isinstance(existing, dict)
                and (settings := _settings_model(model, name)) is not None
            ):
                problems += self._merge_block(
                    target, existing, value, side, None, settings, here=at
                )
            elif existing != value:
                problems.append(self._conflict(target, at, existing, value, side))
        return problems

    def _merge_strategy(
        self,
        target: _Block,
        existing: dict[str, JsonValue],
        value: Mapping[str, JsonValue],
        side: _Side,
        entry: WiringSlot,
        *,
        at: str,
    ) -> list[MergeProblem]:
        """Merge one strategy block (``backend``) two sides write: inside it, when both name one strategy.

        A side that names none takes the layer's only strategy: the part's
        check refused a block without its discriminator where there are two.
        """
        layer = entry.spec.registry_name
        discriminator = _discriminator(self._registrars.get(layer))
        if discriminator is None:
            return [] if existing == value else [self._conflict(target, at, existing, value, side)]
        mine, theirs = existing.get(discriminator), value.get(discriminator)
        if mine is not None and theirs is not None and mine != theirs:
            return [self._conflict(target, f"{at}.{discriminator}", mine, theirs, side)]
        definition = self._definition(layer, mine if mine is not None else theirs)
        if definition is None:
            return [] if existing == value else [self._conflict(target, at, existing, value, side)]
        return self._merge_block(
            target,
            existing,
            value,
            side,
            definition.implementation,
            definition.config_model,
            here=at,
        )

    def _merge_list(
        self,
        target: _Block,
        into: dict[str, JsonValue],
        name: str,
        value: list[JsonValue],
        side: _Side,
        entry: WiringSlot,
        *,
        at: str,
    ) -> list[MergeProblem]:
        """Merge a list of strategy blocks (``actions``): options on entries already there, none added or removed.

        The entries are the run configuration's list, or the whole default
        list when it leaves the list out: every key the receptor takes, in
        its order.
        """
        layer = entry.spec.registry_name
        discriminator = _discriminator(self._registrars.get(layer))
        base = into.get(name)
        if discriminator is None:
            if base is None:
                into[name] = copy.deepcopy(value)
                target.written_by[at] = side
                return []
            return [] if base == value else [self._conflict(target, at, base, value, side)]
        if not isinstance(base, list):
            if layer in target.pooled:
                return [
                    MergeProblem(
                        f"{target.path}.{at}",
                        f"{side} sets options on the {name} of this block, which leaves {name} "
                        f"out and so takes the run configuration's top-level {layer!r} section; a "
                        f"part sets options only on entries written in the capability block: "
                        f"write the {name} in the capability block, not in the top-level "
                        f"{layer!r} section",
                        "capability_list_in_top_level_section",
                    )
                ]
            base = [_entry(discriminator, key) for key in entry.spec.allowed_keys or ()]
            into[name] = base
        entries = {
            item.get(discriminator): item for item in reversed(base) if isinstance(item, dict)
        }
        problems: list[MergeProblem] = []
        seen: set[JsonValue] = set()
        for position, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            key = item.get(discriminator)
            where = f"{at}[{key}]"
            if key in seen:
                # A fault of the part: named where the part writes it.
                problems.append(
                    MergeProblem(
                        f"{side.path}.{at}[{position}]",
                        f"{side} sets options on {key!r} twice; a part sets each entry's "
                        f"options once",
                        "capability_contribution_invalid",
                    )
                )
                continue
            seen.add(key)
            existing = entries.get(key)
            if existing is None:
                problems.append(
                    MergeProblem(
                        f"{target.path}.{where}",
                        f"{side} sets options on {key!r}, and the run configuration's {name} "
                        f"({target.path}.{at}) leave it out; a part sets options on the entries "
                        f"there and never adds or removes one: list {key!r} in the run "
                        f"configuration's {name}, or drop its options",
                        "capability_entry_not_listed",
                    )
                )
                continue
            definition = self._definition(layer, key)
            if definition is None:
                if existing != item:
                    problems.append(self._conflict(target, where, existing, item, side))
                continue
            problems += self._merge_block(
                target,
                existing,
                item,
                side,
                definition.implementation,
                definition.config_model,
                here=where,
            )
        return problems

    @staticmethod
    def _list_receptor(entry: WiringSlot | None) -> bool:
        return (
            entry is not None
            and not entry.declare_only
            and entry.slot is not None
            and entry.slot.shape == "list"
        )

    @staticmethod
    def _strategy_receptor(entry: WiringSlot | None) -> bool:
        return (
            entry is not None
            and not entry.declare_only
            and entry.slot is not None
            and entry.slot.shape == "scalar"
        )

    def _definition(self, layer: str, key: JsonValue) -> ComponentDefinition | None:
        """The implementation of *key* in *layer* and its config model; with no key, the layer's only one."""
        components = self._catalog.components.get(layer, {})
        if key is None:
            only = list(components.values())
            return only[0] if len(only) == 1 else None
        return components.get(key) if isinstance(key, str) else None

    def _conflict(
        self, target: _Block, at: str, existing: object, value: object, side: _Side
    ) -> MergeProblem:
        first = _first_writer(target, at)
        if (
            isinstance(existing, Mapping)
            and isinstance(value, Mapping)
            and max(len(repr(existing)), len(repr(value))) > _SHOWN
        ):
            # A map cut short (open_app's 117 names) says nothing: say where the two differ.
            written = (
                f"{first} writes {_entries(existing)} and {side} writes {_entries(value)}, "
                f"which differ ({_difference(existing, value)})"
            )
        else:
            written = f"{first} writes {_shown(existing)} and {side} writes {_shown(value)}"
        return MergeProblem(
            f"{target.path}.{at}",
            f"{written}; a run takes each setting from one side only: leave it out of one of "
            f"them, or write the same value in both",
            "capability_setting_conflict",
        )


def _without_block(contribution: Contribution, capability_id: object) -> MergeProblem:
    return MergeProblem(
        contribution.path,
        f"{contribution.source} writes a part of the {capability_id!r} capability, and the run "
        f"configuration has no {capability_id!r} capability block to merge it into",
        "capability_without_block",
    )


def _first_writer(target: _Block, at: str) -> _Side:
    """Who wrote the value at *at* first: the nearest recorded writer of it or of a block holding it."""
    parts = at.split(".")
    for end in range(len(parts), 0, -1):
        side = target.written_by.get(".".join(parts[:end]))
        if side is not None:
            return side
    return _Side("the run configuration", target.path)


def _capability_blocks(sections: dict[str, object]) -> list[_Block]:
    """The run configuration's capability blocks in *sections*, each a live view of its dict."""
    blocks: list[_Block] = []
    top = sections.get(CAPABILITY_SECTION)
    for index, item in enumerate(top if isinstance(top, list) else []):
        if isinstance(item, dict):
            blocks.append(_Block(f"{CAPABILITY_SECTION}[{index}]", _json(item)))
    environments = sections.get(ENVIRONMENT_SECTION)
    for index, environment in enumerate(environments if isinstance(environments, list) else []):
        if not isinstance(environment, dict):
            continue
        held = environment.get(ENVIRONMENT_CAPABILITIES)
        where = f"{ENVIRONMENT_SECTION}[{index}].{ENVIRONMENT_CAPABILITIES}"
        if isinstance(held, dict):
            blocks.append(_Block(where, _json(held)))
        elif isinstance(held, list):
            for position, item in enumerate(held):
                if isinstance(item, dict):
                    blocks.append(_Block(f"{where}[{position}]", _json(item)))
    return blocks


def _json(block: dict[object, object]) -> dict[str, JsonValue]:
    """A capability block a YAML document wrote, as the JSON object it is."""
    return cast(dict[str, JsonValue], block)


def _entry(discriminator: str, key: str) -> JsonValue:
    """A list entry that names *key* and writes nothing else: the default's form of it."""
    return {discriminator: key}


def _settings_model(model: type | None, name: str) -> type | None:
    """The block of settings field *name* of *model* is typed as, or ``None`` when it is no such block.

    A pydantic model or a pydantic dataclass (``MatcherConfig``), alone or in
    an optional: its fields are merged one by one. Anything else, a free-form
    map (``dict[str, str]``) included, is one value.
    """
    info = _fields(model).get(name) if model is not None else None
    if info is None:
        return None
    annotation = info.annotation
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    members = (
        [member for member in get_args(annotation) if member is not type(None)]
        if get_origin(annotation) in (Union, UnionType)
        else [annotation]
    )
    blocks = [member for member in members if isinstance(member, type) and _fields(member)]
    return blocks[0] if len(blocks) == 1 and len(members) == 1 else None


def _fields(model: object) -> Mapping[str, FieldInfo]:
    """The fields of a pydantic model or a pydantic dataclass; empty for anything else."""
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_fields
    found = getattr(model, "__pydantic_fields__", None)
    return cast(Mapping[str, FieldInfo], found) if isinstance(found, Mapping) else {}


def _discriminator(registrar: type[LayerRegistrar[object]] | None) -> str | None:
    field_name = getattr(registrar, "discriminator_field", None)
    return field_name if isinstance(field_name, str) else None


def _joined(path: str, loc: tuple[int | str, ...], block: Mapping[str, JsonValue]) -> str:
    """*path* followed by a pydantic location inside *block*, as written (union tags left out)."""
    written = path
    node = cast(object, block)
    for part in loc:
        if isinstance(part, int):
            written += f"[{part}]"
            node = node[part] if isinstance(node, list) and part < len(node) else None
        elif isinstance(node, Mapping) and part not in node:
            continue
        else:
            written += f".{part}"
            node = node.get(part) if isinstance(node, Mapping) else None
    return written


def _shown(value: object) -> str:
    text = repr(value)
    return text if len(text) <= _SHOWN else f"{text[: _SHOWN - 3]}..."


def _entries(value: Mapping[object, object]) -> str:
    return "1 entry" if len(value) == 1 else f"{len(value)} entries"


def _difference(first: Mapping[object, object], second: Mapping[object, object]) -> str:
    """Where two maps differ: the keys only one has, and the keys both have with different values."""
    said = [
        f"{lead} {_named(keys)}{tail}"
        for lead, keys, tail in (
            ("only the first has", [key for key in first if key not in second], ""),
            ("only the second has", [key for key in second if key not in first], ""),
            (
                "both have",
                [key for key in first if key in second and first[key] != second[key]],
                " with different values",
            ),
        )
        if keys
    ]
    return "; ".join(said)


def _named(keys: Sequence[object]) -> str:
    """The first few of *keys* in sorted order by name, and how many more there are."""
    named = ", ".join(_shown(key) for key in sorted(keys, key=str)[:_NAMED])
    rest = len(keys) - _NAMED
    return f"{named} and {rest} more" if rest > 0 else named


__all__ = [
    "CapabilityMerge",
    "CapabilityMergeError",
    "Contribution",
    "MergeProblem",
]
