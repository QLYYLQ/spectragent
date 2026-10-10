"""Layering of the archive axis, pinned on source text.

``docs/architecture_design.md`` §10 ("归档相关的 import 方向") states four
rules; each is a pure function over a module's source here, run over the real
tree and shown failing on planted sources:

1. ``core/runtime.py``, ``core/negotiation/*`` and ``core/environments/*``
   import neither the trace hook (``core.hooks.trace_writer``, or the name
   ``TraceWriter`` from anywhere) nor any module that defines a concrete
   archiver. The runtime knows the consumer only as an ``ArchiveConsumer``,
   the environment knows a capability only as an ``ArchivingCapability`` —
   decision 2: runtime and trace hook never call a capture method.
2. ``core/archive/*`` imports nothing from ``core.trace``, ``core.hooks``,
   ``core.negotiation``, ``core.environments`` or ``core.capabilities`` —
   they depend on it, never the other way round.
3. ``core/trace/*`` imports from ``core.archive`` only
   ``core.archive.privacy`` (the ledger type ``TaskSetupRecord`` carries);
   archive files cross into the trace layer duck-typed, and the WAL's
   interface stays ``TraceFile``.
4. No field of the new archive and negotiation models is about money:
   tokens, never prices (2026-09-19).

Imports are read statically, every one of them — module level, inside a
function, under ``if TYPE_CHECKING:`` — because a type-only import is still
a design-level dependency. A package ``__init__`` that imports more is
outside what these rules see: they are about what each module *names*.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

from conscribe import discover

import spectragent
from spectragent.core.archive import ArchiverRegistrar

PACKAGE_ROOT = Path(spectragent.__file__).resolve().parent
CORE = PACKAGE_ROOT / "core"

TRACE_WRITER_MODULE = "spectragent.core.hooks.trace_writer"
TRACE_WRITER_NAME = "TraceWriter"
PRIVACY_MODULE = "spectragent.core.archive.privacy"
ARCHIVE_PACKAGE = "spectragent.core.archive"

#: What ``core/archive`` must never import (rule 2).
ARCHIVE_MUST_NOT_IMPORT = (
    "spectragent.core.trace",
    "spectragent.core.hooks",
    "spectragent.core.negotiation",
    "spectragent.core.environments",
    "spectragent.core.capabilities",
)

#: Field names that are about money (rule 4). A substring search: ``unit_cost``,
#: ``price_usd`` and ``dollars`` all match.
MONEY = re.compile(r"price|cost|usd|dollar", re.IGNORECASE)


# ── pure functions over source text ───────────────────────────────────


def module_name(path: Path) -> str:
    """``spectragent/core/archive/types.py`` → ``spectragent.core.archive.types``."""
    parts = path.resolve().relative_to(PACKAGE_ROOT.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_modules(source: str, module: str, *, is_package: bool = False) -> set[str]:
    """Every module *source* imports, as absolute dotted names.

    ``import a.b`` yields ``a.b``; ``from a.b import c`` yields ``a.b`` and
    ``a.b.c`` (``c`` may be a submodule); a relative import is resolved
    against *module* (the package itself when *is_package*). Every import
    counts, wherever it sits in the file.
    """
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def mentioned_names(source: str, name: str) -> list[int]:
    """Lines where *name* is used as an identifier — imported, read or attribute-accessed.

    Docstrings and comments are not identifiers: a module may *explain* the
    trace hook without depending on it.
    """
    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        imported = isinstance(node, ast.alias) and name in {
            node.name.rpartition(".")[2],
            node.asname,
        }
        read = isinstance(node, ast.Name) and node.id == name
        accessed = isinstance(node, ast.Attribute) and node.attr == name
        if imported or read or accessed:
            lines.add(getattr(node, "lineno", 0))
    return sorted(lines)


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def consumer_side_violations(
    source: str, module: str, *, archiver_modules: frozenset[str], is_package: bool = False
) -> list[str]:
    """Rule 1 for one module: no trace hook, no concrete archiver."""
    imported = imported_modules(source, module, is_package=is_package)
    problems = [
        f"imports {name}"
        for name in sorted(imported)
        if _within(name, TRACE_WRITER_MODULE)
        or any(_within(name, archiver) for archiver in archiver_modules)
    ]
    problems += [
        f"names {TRACE_WRITER_NAME} (line {line})"
        for line in mentioned_names(source, TRACE_WRITER_NAME)
    ]
    return problems


def archive_package_violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Rule 2 for one module of ``core/archive``."""
    imported = imported_modules(source, module, is_package=is_package)
    return [
        f"imports {name}"
        for name in sorted(imported)
        if any(_within(name, forbidden) for forbidden in ARCHIVE_MUST_NOT_IMPORT)
    ]


def trace_layer_violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Rule 3 for one module of ``core/trace``."""
    imported = imported_modules(source, module, is_package=is_package)
    return [
        f"imports {name}"
        for name in sorted(imported)
        if _within(name, ARCHIVE_PACKAGE) and not _within(name, PRIVACY_MODULE)
    ]


def money_fields(source: str) -> list[str]:
    """``Class.attribute`` for every class attribute or ``__init__`` parameter about money.

    Class-level assignments and annotations (dataclass and pydantic fields,
    class variables), methods and properties, and the parameters of every
    ``__init__``.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            names: list[str] = []
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                names.append(item.target.id)
            elif isinstance(item, ast.Assign):
                names += [target.id for target in item.targets if isinstance(target, ast.Name)]
            elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                names.append(item.name)
                if item.name == "__init__":
                    arguments = item.args
                    names += [
                        arg.arg
                        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
                    ]
            found += [f"{node.name}.{name}" for name in names if MONEY.search(name)]
    return found


# ── the tree, as it is ────────────────────────────────────────────────


def _modules(*paths: Path) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
        else:
            files.append(path)
    return files


def _check(path: Path, rule: Callable[..., list[str]]) -> list[str]:
    """*rule* over one file, each problem prefixed with the file."""
    source = path.read_text(encoding="utf-8")
    problems = rule(source, module_name(path), is_package=path.name == "__init__.py")
    return [f"{path.relative_to(PACKAGE_ROOT.parent)}: {problem}" for problem in problems]


def _concrete_archiver_modules() -> frozenset[str]:
    """Modules that define a registered concrete archiver — none until M29."""
    discover("spectragent")
    return frozenset(
        cls.__module__
        for cls in ArchiverRegistrar.get_all().values()
        if cls.__module__.startswith("spectragent.")
    )


CONSUMER_SIDE = _modules(CORE / "runtime.py", CORE / "negotiation", CORE / "environments")


class TestTheTreeKeepsTheLayering:
    def test_the_scans_reach_real_files(self) -> None:
        names = {module_name(path) for path in CONSUMER_SIDE}
        assert {
            "spectragent.core.runtime",
            "spectragent.core.negotiation.archive",
            "spectragent.core.environments._registrar",
            "spectragent.core.environments.base",
        } <= names
        assert {module_name(p) for p in _modules(CORE / "archive")} >= {
            "spectragent.core.archive._registrar",
            "spectragent.core.archive.privacy",
        }
        assert module_name(CORE / "trace" / "models.py") in {
            module_name(p) for p in _modules(CORE / "trace")
        }

    def test_runtime_negotiation_and_environments_never_import_the_trace_hook(self) -> None:
        archivers = _concrete_archiver_modules()
        offenders = [
            line
            for path in CONSUMER_SIDE
            for line in _check(
                path,
                lambda source, module, is_package: consumer_side_violations(
                    source, module, archiver_modules=archivers, is_package=is_package
                ),
            )
        ]
        assert not offenders, (
            f"{offenders}. The runtime finds the archive consumer through ArchiveConsumer and "
            f"the environment talks to capabilities through ArchivingCapability; naming the "
            f"trace hook or a concrete archiver here couples the orchestration to one recorder "
            f"or one capability's capture code (architecture §10, decision 2)."
        )

    def test_core_archive_depends_on_none_of_its_dependents(self) -> None:
        offenders = [
            line
            for path in _modules(CORE / "archive")
            for line in _check(path, archive_package_violations)
        ]
        assert not offenders, (
            f"{offenders}. core/archive is what trace, hooks, negotiation, environments and "
            f"capabilities build on — an import the other way is a cycle waiting to happen."
        )

    def test_core_trace_imports_only_the_privacy_module_from_core_archive(self) -> None:
        offenders = [
            line
            for path in _modules(CORE / "trace")
            for line in _check(path, trace_layer_violations)
        ]
        assert not offenders, (
            f"{offenders}. The trace layer takes archive files duck-typed (encode_archive_file) "
            f"and types only the ledger it records; the WAL's interface stays TraceFile."
        )

    def test_the_new_models_record_tokens_never_money(self) -> None:
        scanned = _modules(
            CORE / "archive", CORE / "negotiation" / "archive.py", CORE / "trace" / "models.py"
        )
        offenders = [
            f"{path.relative_to(PACKAGE_ROOT.parent)}: {field}"
            for path in scanned
            for field in money_fields(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            f"{offenders}. The framework records tokens, never prices: a price is a property of "
            f"a provider's tariff on a date, not of the run (2026-09-19)."
        )


# ── the rules have teeth ──────────────────────────────────────────────


class TestTheImportReaderHasTeeth:
    def test_absolute_from_and_submodule_imports(self) -> None:
        source = "import a.b\nfrom c.d import e\n"
        assert imported_modules(source, "x.y") == {"a.b", "c.d", "c.d.e"}

    def test_relative_imports_resolve_against_the_module(self) -> None:
        source = "from .types import ArchiveFile\nfrom ..hooks import trace_writer\nfrom . import privacy\n"
        assert imported_modules(source, "spectragent.core.archive.protocols") == {
            "spectragent.core.archive.types",
            "spectragent.core.archive.types.ArchiveFile",
            "spectragent.core.hooks",
            "spectragent.core.hooks.trace_writer",
            "spectragent.core.archive",
            "spectragent.core.archive.privacy",
        }

    def test_a_package_init_resolves_relative_imports_against_itself(self) -> None:
        assert "spectragent.core.archive.types" in imported_modules(
            "from .types import ArchiveFile\n", "spectragent.core.archive", is_package=True
        )

    def test_lazy_and_type_only_imports_count(self) -> None:
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from spectragent.core.trace.models import TraceConfig\n"
            "def late():\n"
            "    import spectragent.core.hooks.trace_writer\n"
        )
        found = imported_modules(source, "spectragent.core.archive.types")
        assert "spectragent.core.trace.models" in found
        assert "spectragent.core.hooks.trace_writer" in found


class TestTheRulesHaveTeeth:
    def test_rule_one_catches_the_trace_hook_however_it_is_reached(self) -> None:
        module = "spectragent.core.negotiation.archive"
        planted = {
            "from spectragent.core.hooks.trace_writer import TraceWriter\n": "imports",
            "import spectragent.core.hooks.trace_writer as tw\n": "imports",
            "from ..hooks.trace_writer import TraceWriter\n": "imports",
            "from spectragent.core.hooks import TraceWriter\n": "names TraceWriter",
            "import spectragent.core.hooks as hooks\nhooks.TraceWriter\n": "names TraceWriter",
        }
        for source, phrase in planted.items():
            problems = consumer_side_violations(source, module, archiver_modules=frozenset())
            assert any(phrase in problem for problem in problems), (source, problems)

    def test_rule_one_catches_a_concrete_archiver_module(self) -> None:
        archiver = "spectragent.core.capabilities.browser.archivers.network"
        for source in (
            f"from {archiver} import NetworkArchiver\n",
            f"import {archiver}\n",
            "from spectragent.core.capabilities.browser.archivers import network\n",
        ):
            assert consumer_side_violations(
                source, "spectragent.core.runtime", archiver_modules=frozenset({archiver})
            ), source

    def test_rule_one_lets_the_protocols_and_a_docstring_through(self) -> None:
        source = (
            '"""The runtime never imports TraceWriter."""\n'
            "from spectragent.core.archive.protocols import ArchiveConsumer\n"
            "from spectragent.core.negotiation import negotiate_archive\n"
        )
        assert (
            consumer_side_violations(
                source, "spectragent.core.runtime", archiver_modules=frozenset()
            )
            == []
        )

    def test_rule_two_catches_each_dependent_and_respects_name_boundaries(self) -> None:
        module = "spectragent.core.archive.types"
        for forbidden in ARCHIVE_MUST_NOT_IMPORT:
            assert archive_package_violations(f"from {forbidden}.x import y\n", module), forbidden
            assert archive_package_violations(f"import {forbidden}\n", module), forbidden
        assert archive_package_violations("from ..trace import models\n", module)
        # ``core.tracer`` is not ``core.trace``.
        assert archive_package_violations("import spectragent.core.tracer\n", module) == []
        assert (
            archive_package_violations("from spectragent.core.archive import types\n", module) == []
        )

    def test_rule_three_allows_privacy_and_nothing_else(self) -> None:
        module = "spectragent.core.trace.models"
        assert (
            trace_layer_violations(
                "from spectragent.core.archive.privacy import RedactionLedger\n", module
            )
            == []
        )
        for source in (
            "from spectragent.core.archive import ArchiveFile\n",
            "from spectragent.core.archive.types import ArchiveFile\n",
            "import spectragent.core.archive.protocols\n",
            "from spectragent.core.archive import privacy\n",  # names the package itself
        ):
            assert trace_layer_violations(source, module), source

    def test_rule_four_catches_money_however_it_is_spelled(self) -> None:
        source = (
            "class Leaky:\n"
            '    """The cost of a run is not recorded."""\n'
            "    unit_cost: float = 0.0\n"
            "    PRICE_TABLE = {}\n"
            "    def __init__(self, dollars: int, *, budget_usd: float) -> None: ...\n"
            "    @property\n"
            "    def total_price(self) -> float: ...\n"
            "class Clean:\n"
            "    # the price of a call is not a field\n"
            "    prompt_tokens: int = 0\n"
        )
        assert sorted(money_fields(source)) == [
            "Leaky.PRICE_TABLE",
            "Leaky.budget_usd",
            "Leaky.dollars",
            "Leaky.total_price",
            "Leaky.unit_cost",
        ]
