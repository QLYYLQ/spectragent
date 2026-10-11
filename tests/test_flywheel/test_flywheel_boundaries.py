"""Layering of the metrics gate, pinned on source text (arch §4.16).

1. Nothing under ``spectragent/`` outside ``flywheel/`` imports it: it is a
   leaf consumer, like the evaluator and the export.
2. It touches no running state: no runtime, no loop, no hook, no
   environment, no agent, no concrete browser capability — every module
   reads committed runs, except ``chain.py``, which calls the existing CLIs.
3. Only ``chain.py`` imports ``spectragent.export`` — exactly its CLI — and
   only ``chain.py`` imports ``spectragent-bench``'s module.
4. Nothing loads ``.env``: keys are exported in the shell (the CLIs' rule).
5. No model field is about money (tokens, never prices).

Every import counts, wherever it sits (the archive boundary test's reader).
"""

from __future__ import annotations

from pathlib import Path

import spectragent
from tests.test_core.test_archive.test_archive_boundaries import (
    imported_modules,
    mentioned_names,
    module_name,
    money_fields,
)

PACKAGE = Path(spectragent.__file__).resolve().parent
FLYWHEEL = "spectragent.flywheel"
MODULES = sorted((PACKAGE / "flywheel").glob("*.py"))
#: What no flywheel module may import: running state and live classes.
LIVE = (
    "spectragent.core.runtime",
    "spectragent.core.loops",
    "spectragent.core.hooks",
    "spectragent.core.environments",
    "spectragent.agents",
    "spectragent.core.capabilities.browser.capability",
)
CHAIN = "spectragent.flywheel.chain"
#: The CLIs the chain calls in-process (``main(argv)``).
CHAIN_ENTRIES = {"spectragent.core.benchmark_runner", "spectragent.export.cli"}


def _imports(path: Path) -> set[str]:
    return imported_modules(
        path.read_text(), module_name(path), is_package=path.name == "__init__.py"
    )


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_the_scan_reaches_the_package() -> None:
    assert {path.name for path in MODULES} >= {
        "__init__.py",
        "metrics.py",
        "report.py",
        "chain.py",
        "cli.py",
    }


def test_nothing_outside_imports_the_flywheel() -> None:
    offenders = {
        module_name(path): sorted(name for name in _imports(path) if _within(name, FLYWHEEL))
        for path in PACKAGE.rglob("*.py")
        if not _within(module_name(path), FLYWHEEL)
    }
    assert {module: names for module, names in offenders.items() if names} == {}


def test_no_module_touches_running_state() -> None:
    offenders = {
        path.name: sorted(
            name for name in _imports(path) if any(_within(name, live) for live in LIVE)
        )
        for path in MODULES
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_only_the_chain_reaches_the_clis_and_the_export() -> None:
    for path in MODULES:
        imported = _imports(path)
        export = sorted(name for name in imported if _within(name, "spectragent.export"))
        entries = sorted(
            name for name in imported if any(_within(name, entry) for entry in CHAIN_ENTRIES)
        )
        if module_name(path) == CHAIN:
            assert set(export) <= {"spectragent.export.cli", "spectragent.export.cli.main"}, export
            assert set(entries) >= CHAIN_ENTRIES, entries
        else:
            assert export == [] and entries == [], (path.name, export, entries)


def test_nothing_loads_dotenv() -> None:
    for path in MODULES:
        imported = _imports(path)
        assert not [name for name in imported if "dotenv" in name], path.name
        assert mentioned_names(path.read_text(), "load_dotenv") == [], path.name


def test_no_model_is_about_money() -> None:
    found = {path.name: money_fields(path.read_text()) for path in MODULES}
    assert {name: fields for name, fields in found.items() if fields} == {}


def test_the_rules_have_teeth() -> None:
    planted = "from spectragent.core.runtime import AgentRuntime\n"
    names = imported_modules(planted, "spectragent.flywheel.metrics")
    assert any(_within(name, "spectragent.core.runtime") for name in names)
    assert mentioned_names("from dotenv import load_dotenv\nload_dotenv()\n", "load_dotenv")
