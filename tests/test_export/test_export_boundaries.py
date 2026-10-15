"""Layering of the training export, pinned on source text (arch §10 "导出相关的 import 方向").

1. Nothing under ``spectragent/`` outside ``export/`` imports
   ``spectragent.export`` — the export is a leaf consumer — except the
   registrar collection in ``scripts/generate_all.py`` and what it generates
   (``generated/_composed.py`` imports the exporter's knob types), and the
   metrics chain (``flywheel/chain.py``, M33), which runs
   ``spectragent export``'s ``main`` in-process — its CLI module and nothing
   else of the export.
2. ``export/runner.py`` reaches exporters and translators only through the
   registrars and Protocols: it imports no concrete exporter or translator.
3. ``schemas/sample.py`` imports nothing from ``spectragent.core``.
4. The browser's export knowledge (``index_points.py``, ``capture_check.py``,
   ``dom/cleaning/skyvern_hit_test.py``) reads what it is handed: no
   ``core.trace``, no ``spectragent.export``.
5. No field of the new models is about money (tokens, never prices).

Every import counts, wherever it sits (the archive boundary test's reader).
"""

from __future__ import annotations

from pathlib import Path

import spectragent
from tests.test_core.test_archive.test_archive_boundaries import (
    imported_modules,
    module_name,
    money_fields,
)

PACKAGE = Path(spectragent.__file__).resolve().parent
EXPORT = "spectragent.export"
#: The one module outside the export that may import it: it collects the registrars.
COLLECTOR = "spectragent.scripts.generate_all"
#: Its build output, which imports the knob types it typed the config with.
GENERATED = "spectragent.generated"
#: The metrics chain: it may import the export's CLI module, by exact name, and nothing else of it.
CHAIN = "spectragent.flywheel.chain"
CHAIN_MAY_IMPORT = frozenset({"spectragent.export.cli", "spectragent.export.cli.main"})
#: Concrete strategies the runner must not name.
CONCRETE = (
    "spectragent.export.openai_messages",
    "spectragent.export.translate.identity",
    "spectragent.export.translate.index_to_coordinate",
)
BROWSER_EXPORT_KNOWLEDGE = (
    PACKAGE / "core/capabilities/browser/index_points.py",
    PACKAGE / "core/capabilities/browser/capture_check.py",
    PACKAGE / "core/capabilities/browser/dom/cleaning/skyvern_hit_test.py",
)


def _imports(path: Path) -> set[str]:
    return imported_modules(
        path.read_text(), module_name(path), is_package=path.name == "__init__.py"
    )


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_nothing_outside_the_export_imports_it() -> None:
    offenders = {
        module_name(path): sorted(
            name
            for name in _imports(path)
            if _within(name, EXPORT)
            and not (module_name(path) == CHAIN and name in CHAIN_MAY_IMPORT)
        )
        for path in PACKAGE.rglob("*.py")
        if not _within(module_name(path), EXPORT)
        and module_name(path) != COLLECTOR
        and not _within(module_name(path), GENERATED)
    }
    assert {module: names for module, names in offenders.items() if names} == {}
    assert any(_within(name, EXPORT) for name in _imports(PACKAGE / "scripts/generate_all.py"))
    chain = _imports(PACKAGE / "flywheel/chain.py")
    assert {name for name in chain if _within(name, EXPORT)} == CHAIN_MAY_IMPORT, (
        "the chain no longer imports the export's CLI — drop the exception with it"
    )


def test_the_runner_names_no_concrete_strategy() -> None:
    imported = _imports(PACKAGE / "export/runner.py")
    assert not [name for name in imported if any(_within(name, c) for c in CONCRETE)]
    source = (PACKAGE / "export/runner.py").read_text()
    for name in ("OpenAIMessagesExporter", "IdentityTranslator", "IndexToCoordinateTranslator"):
        assert name not in source


def test_the_sample_is_domain_neutral() -> None:
    imported = _imports(PACKAGE / "schemas/sample.py")
    assert not [name for name in imported if _within(name, "spectragent.core")]


def test_the_browser_export_knowledge_reads_what_it_is_handed() -> None:
    for path in BROWSER_EXPORT_KNOWLEDGE:
        imported = _imports(path)
        bad = [
            name
            for name in imported
            if _within(name, "spectragent.core.trace") or _within(name, EXPORT)
        ]
        assert bad == [], f"{path.name}: {bad}"


def test_no_model_is_about_money() -> None:
    sources = [PACKAGE / "schemas/sample.py", *sorted((PACKAGE / "export").rglob("*.py"))]
    found = {path.name: money_fields(path.read_text()) for path in sources}
    assert {name: fields for name, fields in found.items() if fields} == {}
