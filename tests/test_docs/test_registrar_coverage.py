"""Every registrar in the code must appear in the docs' registrar maps.

CLAUDE.md §0.1 makes ``docs/architecture_design.md`` the source of truth and
says the code is wrong when the two disagree. A registrar is this framework's
top-level unit of extension — a whole varying axis, its own YAML block, its own
generated config classes — so a registrar the design doc does not mention is
not merely undocumented: under that rule it is *deletable on sight* by the next
session, which will read the doc, not find it, and conclude the layer is legacy.

That nearly happened: ``core/capabilities/browser/input/`` carried
``MouseTrajectoryRegistrar`` and ``TypingRhythmRegistrar`` — 16 files, two
registries, the entire input humanization layer — through several rounds while
neither the design doc nor CLAUDE.md's registrar map named either one. This
test is the mechanical version of the rule that was previously a habit, and it
found a third omission (``NegotiationResultRegistrar``) the first time it ran.

**Direction.** Code → docs only. A documented registrar with no implementation
is legitimate: the design doc is a *target* state and carries planned entries
(``DockerProviderRegistrar`` is a P2 placeholder). The failure this guards is
the other one — shipping an extension axis nobody wrote down.

**Method.** The scan is an AST walk, not an import: it reads what the source
*declares* rather than what happens to be imported by the time the test runs,
so a registrar in a module no test imports is still caught, and the scan does
not depend on ``pytest-randomly``'s ordering or on the optional extras being
installed. It also checks the discriminator each map claims, because a row that
names the wrong discriminator field is a row that will mislead someone writing
YAML.

**What this does not cover**, stated because a guard advertised as total is
worse than no guard:

- Only ``conscribe.create_registrar`` assigned to a module-level name under
  ``spectragent/``. A registry from a dependency is out of scope by
  construction — ``agent-cdp``'s ``WatchdogRegistrar`` is that library's, and
  the browser capability is its consumer, not its author.
- Presence and discriminator, not accuracy. A row can name the registrar, name
  the right discriminator and still describe it wrongly; nothing mechanical
  catches that.
- The prose. §4.13 of the design doc explains the input humanization layer;
  this test only guarantees §6 lists its two registries. Deleting the prose
  and keeping the rows would pass.
- ``CLAUDE.md`` when it is absent. It is **gitignored** (``.gitignore``
  line 16) and therefore missing from a fresh clone, so its half of the rule
  is enforced only where the file exists — which is every working tree an
  agent session runs in, and none of CI. ``docs/architecture_design.md`` is
  tracked and is required to exist; if it ever stops being the source of
  truth, this test fails rather than quietly checking nothing.
"""

from __future__ import annotations

import ast
import itertools
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from spectragent import PACKAGE_ROOT, REPO_ROOT

#: The design doc's registrar table (§6) and CLAUDE.md's (§2.2). Both are
#: "the registrar map" to a reader, so both are held to the same rule.
_MAPS: dict[str, tuple[Path, str]] = {
    "docs/architecture_design.md §6": (
        REPO_ROOT / "docs" / "architecture_design.md",
        "## 6. conscribe 注册表总览",
    ),
    "CLAUDE.md §2.2": (
        REPO_ROOT / "CLAUDE.md",
        "### 2.2 Registrar map",
    ),
}

#: The map that must exist. The other is gitignored, so it is checked where it
#: is present and skipped where it is not — see the module docstring.
_REQUIRED_MAP = "docs/architecture_design.md §6"

#: Backticked ``SomethingRegistrar`` token inside a markdown table cell.
_REGISTRAR_TOKEN = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*Registrar)`")


@dataclass(frozen=True)
class CodeRegistrar:
    """One ``X = create_registrar(...)`` found in the package source."""

    name: str
    """The assigned name — ``MouseTrajectoryRegistrar``. This is what the maps
    spell, so it is what the maps are checked against."""

    registry: str
    """First positional argument: the conscribe registry name (``"mouse_trajectory"``)."""

    discriminators: tuple[str, ...]
    """``discriminator_field=`` as a 1-tuple, or ``discriminator_fields=`` in order."""

    where: str
    """Repo-relative path, for the failure message."""


def _create_registrar_call(node: ast.AST) -> ast.Call | None:
    """Return the node if it is a ``create_registrar(...)`` call, else ``None``.

    Matches the bare name and the qualified ``conscribe.create_registrar``
    spelling; both are legal and the repo happens to use only the first.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "create_registrar":
        return node
    if isinstance(func, ast.Attribute) and func.attr == "create_registrar":
        return node
    return None


def _discriminators(call: ast.Call) -> tuple[str, ...]:
    for kw in call.keywords:
        if kw.arg == "discriminator_field" and isinstance(kw.value, ast.Constant):
            return (str(kw.value.value),)
        if kw.arg == "discriminator_fields" and isinstance(kw.value, ast.List):
            return tuple(
                str(elt.value) for elt in kw.value.elts if isinstance(elt, ast.Constant)
            )
    return ()


def _registry_name(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.Constant):
        return str(call.args[0].value)
    return ""


def _code_registrars() -> dict[str, CodeRegistrar]:
    """Every registrar declared under ``spectragent/``, by assigned name.

    ``spectragent/generated/`` is skipped: it is codegen output derived from
    the source classes, never a declaration site.
    """
    found: dict[str, CodeRegistrar] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = set(path.parts)
        if "__pycache__" in parts or "generated" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = _create_registrar_call(node.value)
            if call is None:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                found[target.id] = CodeRegistrar(
                    name=target.id,
                    registry=_registry_name(call),
                    discriminators=_discriminators(call),
                    where=str(path.relative_to(REPO_ROOT)),
                )
    return found


def _map_rows(path: Path, heading: str) -> dict[str, str]:
    """Registrar name -> the whole table row that names it, for one map.

    The section runs from *heading* to the next heading of the same or a
    shallower level, so a sub-heading inside the section does not truncate it.
    A registrar named in two rows keeps the first; none are today.
    """
    text = path.read_text(encoding="utf-8")
    start = text.find(heading)
    assert start != -1, f"{path.name}: heading {heading!r} is gone — retarget this test"
    depth = len(heading) - len(heading.lstrip("#"))
    end = len(text)
    for match in re.finditer(r"^#{1,6} ", text[start + len(heading) :], re.MULTILINE):
        level = len(match.group(0)) - 1
        if level <= depth:
            end = start + len(heading) + match.start()
            break

    rows: dict[str, str] = {}
    for line in text[start:end].splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for name in _REGISTRAR_TOKEN.findall(line):
            rows.setdefault(name, line)
    return rows


# Computed once at import; both are inputs to every test below.
CODE_REGISTRARS = _code_registrars()
MAP_ROWS = {
    label: _map_rows(*spec) for label, spec in _MAPS.items() if spec[0].exists()
}


def _rows_or_skip(label: str) -> dict[str, str]:
    rows = MAP_ROWS.get(label)
    if rows is None:
        pytest.skip(f"{label}: file is gitignored and absent from this checkout")
    return rows


def test_the_required_map_is_present() -> None:
    """The tracked map may not go missing or get renamed unnoticed.

    Without this, deleting §6 from the design doc would turn every check
    below into a skip — the map would be gone and nothing would say so.
    """
    assert _REQUIRED_MAP in MAP_ROWS, f"{_REQUIRED_MAP} is missing — retarget this test"


def test_the_scan_and_the_maps_are_both_non_trivial() -> None:
    """Positive control: neither side may be quietly empty.

    A coverage test whose two sets are both empty passes forever. If the AST
    walk stops matching (conscribe renames its factory) or the section
    extraction stops matching (a heading is reworded), this fails first and
    names which half broke, instead of the real checks going vacuously green.
    """
    assert len(CODE_REGISTRARS) >= 10, (
        f"the AST scan found only {sorted(CODE_REGISTRARS)} — "
        "create_registrar is probably spelled differently now"
    )
    for label, rows in MAP_ROWS.items():
        assert len(rows) >= 10, f"{label}: parsed only {sorted(rows)} rows"


@pytest.mark.parametrize("label", sorted(_MAPS))
def test_every_registrar_in_the_code_is_in_the_map(label: str) -> None:
    """The rule itself. Add the registrar, add the row, same change.

    The remedy depends on who wrote the registrar, and the two branches are
    not the same move — see the assertion message.
    """
    rows = _rows_or_skip(label)
    missing = sorted(name for name in CODE_REGISTRARS if name not in rows)
    assert not missing, (
        f"{label} does not mention: "
        + ", ".join(f"{n} ({CODE_REGISTRARS[n].where})" for n in missing)
        + ". A registrar is a top-level extension axis; one the docs do not "
        "name is deletable on sight under CLAUDE.md §0.1. Two remedies, and "
        "which one applies depends on whether you wrote it: (a) you are "
        "adding this registrar — write the row in this same change, that "
        "coupling is what this test buys; (b) this fired on a registrar you "
        "did not author — do not just add a row. CLAUDE.md §3.1 lists "
        "'existing code implements something architecture_design.md does not "
        "mention' as a stop-and-ask, because the doc is the *target* state "
        "and a row is an endorsement that the axis should exist. Confirm "
        "with the user that it should, then document it. Relaxing this test "
        "is neither remedy."
    )


@pytest.mark.parametrize("label", sorted(_MAPS))
def test_the_map_names_the_discriminator_the_code_uses(label: str) -> None:
    """A row that names the wrong discriminator misleads whoever writes YAML.

    Checked as containment rather than equality because the maps spell the
    nested case as ``model_type.provider`` and annotate rows with ``(flat)``.
    """
    rows = _rows_or_skip(label)
    wrong: list[str] = []
    for name, reg in CODE_REGISTRARS.items():
        row = rows.get(name)
        if row is None:
            continue  # the other test owns this failure
        for field in reg.discriminators:
            if field not in row:
                wrong.append(f"{name}: row does not name {field!r} ({reg.where})")
    assert not wrong, f"{label}: " + "; ".join(wrong)


def test_the_two_maps_agree_with_each_other() -> None:
    """Neither map may carry a registrar the other one silently lacks.

    Both are read as "the registrar map"; a reader consults whichever is at
    hand. The design doc is allowed *planned* entries with no code (that is
    what it is for), so this compares only the registrars that actually exist.
    """
    if len(MAP_ROWS) < 2:
        pytest.skip("only one map is present in this checkout")
    per_map = {label: set(rows) & set(CODE_REGISTRARS) for label, rows in MAP_ROWS.items()}
    # Every pair, not the first two: with a third map added to ``_MAPS`` the
    # index form would have kept passing while checking two thirds of nothing,
    # which is the same vacuous-green failure the guards above exist to stop.
    disagreements = [
        f"{a} has {sorted(per_map[a] - per_map[b])} that {b} lacks"
        for a, b in itertools.permutations(sorted(per_map), 2)
        if per_map[a] - per_map[b]
    ]
    assert not disagreements, "; ".join(disagreements)
