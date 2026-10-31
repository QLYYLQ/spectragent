"""Reading a capability's declarations imports nothing heavy.

The framework reads every capability package's ``declarations.py`` before it
imports anything else of the package (``docs/architecture_design.md``
§4.2.1). So a declarations module imports only registrar definitions and its
setup schema, and every ``__init__.py`` on the way imports no implementation:
a program that composes no browser run pays for no browser, and a missing
optional extra blocks nothing it does not use.

Imports are read statically and followed the way Python runs them when the
module is imported: every module-level import (inside ``try`` and ordinary
``if`` blocks too) and the ``__init__.py`` of every package on the way,
transitively through ``spectragent``. An import inside a function or under
``if TYPE_CHECKING:`` does not run at import time and is not followed. The
precedent is ``test_agent_cdp_boundary.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import spectragent
from spectragent.core.registries import (
    CAPABILITIES_PACKAGE,
    DECLARATIONS_MODULE,
    CapabilityDeclarations,
    capability_declarations,
)

PACKAGE_ROOT = Path(spectragent.__file__).resolve().parent
#: The third-party packages a declarations module may reach: the registrar and
#: schema machinery. agent-cdp, websockets and numpy are what this test keeps out.
ALLOWED_THIRD_PARTY = frozenset({"conscribe", "pydantic"})


def _source(module: str) -> Path | None:
    """The file *module* is executed from, or ``None`` when it names no module."""
    base = PACKAGE_ROOT.joinpath(*module.split(".")[1:])
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports_run_on_import(module: str, path: Path) -> set[str]:
    """What importing *module* imports: module-level statements, ``TYPE_CHECKING`` skipped."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                    base = ".".join([*parts, base] if base else parts)
                found.add(base)
                # ``from package import name`` imports the submodule when *name* is one.
                found.update(f"{base}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.If):
                if not _is_type_checking(node.test):
                    visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return found


def _closure(start: str) -> tuple[set[str], set[str]]:
    """The ``spectragent`` modules and third-party packages that importing *start* runs."""
    modules: set[str] = set()
    third_party: set[str] = set()
    pending = [start]
    while pending:
        name = pending.pop()
        top = name.split(".")[0]
        if top != "spectragent":
            if top not in sys.stdlib_module_names:
                third_party.add(top)
            continue
        parts = name.split(".")
        for depth in range(1, len(parts) + 1):
            module = ".".join(parts[:depth])
            path = _source(module)
            if module in modules or path is None:
                continue
            modules.add(module)
            pending.extend(_imports_run_on_import(module, path))
    return modules, third_party


def _registered_classes(module: str) -> set[str]:
    """Classes *module* defines that register an implementation: a key, not abstract."""
    path = _source(module)
    assert path is not None
    registered: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.ClassDef):
            continue
        assigned: dict[str, ast.expr] = {}
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        assigned[target.id] = statement.value
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.value is not None
            ):
                assigned[statement.target.id] = statement.value
        abstract = assigned.get("__abstract__")
        if "__registry_key__" in assigned and not (
            isinstance(abstract, ast.Constant) and abstract.value is True
        ):
            registered.add(f"{module}.{node.name}")
    return registered


DECLARED = capability_declarations()


def _declarations_of(capability: CapabilityDeclarations) -> str:
    return f"{CAPABILITIES_PACKAGE}.{capability.capability_id}.{DECLARATIONS_MODULE}"


def test_there_is_a_capability_to_check() -> None:
    # Without this the checks below would pass on a scan that found nothing.
    assert "browser" in {capability.capability_id for capability in DECLARED}


@pytest.mark.parametrize("capability", DECLARED, ids=lambda capability: capability.capability_id)
def test_declarations_reach_no_heavy_package(capability: CapabilityDeclarations) -> None:
    _, third_party = _closure(_declarations_of(capability))
    assert third_party <= ALLOWED_THIRD_PARTY, (
        f"importing {_declarations_of(capability)} imports {sorted(third_party - ALLOWED_THIRD_PARTY)}. "
        f"It may import only registrar definitions and its setup schema; put an import used "
        f"only in annotations under TYPE_CHECKING, and keep every __init__.py on the way empty "
        f"of implementation imports."
    )


@pytest.mark.parametrize("capability", DECLARED, ids=lambda capability: capability.capability_id)
def test_declarations_reach_no_implementation(capability: CapabilityDeclarations) -> None:
    modules, _ = _closure(_declarations_of(capability))
    implementations = {name for module in modules for name in _registered_classes(module)}
    assert not implementations, (
        f"importing {_declarations_of(capability)} defines registered implementations "
        f"{sorted(implementations)}"
    )


def test_the_scan_sees_what_the_browser_implementation_imports() -> None:
    # Positive control: the walk does reach the heavy packages it must keep out.
    _, third_party = _closure(f"{CAPABILITIES_PACKAGE}.browser.capability")
    assert {"agent_cdp", "websockets"} <= third_party
    _, sampler = _closure(f"{CAPABILITIES_PACKAGE}.browser.input._mouse_gan_v2")
    assert "numpy" in sampler


def test_an_import_inside_a_function_or_for_type_checking_is_not_followed() -> None:
    # ``learned`` imports numpy only when constructed and under TYPE_CHECKING,
    # which is why it registers on a machine without the extra.
    _, third_party = _closure(f"{CAPABILITIES_PACKAGE}.browser.input.learned")
    assert "numpy" not in third_party
