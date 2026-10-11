"""The shared observation package must not know any domain's internals.

``schemas/observation/`` is what every future domain — SWE, desktop,
mobile, GPU — reads to learn how a fragment is written. It used to carry
a hand-rolled encoder for the browser's DOM dataclass, keyed off six of
that dataclass's field names, so the pattern it taught was "reach into
another package and hardcode its field names", and an upstream rename
silently changed what landed in the trace.

The fix was structural: a fragment that needs domain knowledge to encode
itself lives in that domain's package. These tests are the tripwire.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import spectragent.schemas.observation as shared_pkg
import spectragent.schemas.sample as sample_module

SHARED_DIR = Path(shared_pkg.__file__).parent
#: The canonical training sample (arch §4.15) is read by every domain's
#: exporter the same way, so it is held to the same rules.
SAMPLE_MODULE = Path(sample_module.__file__)
SHARED_MODULES = sorted(SHARED_DIR.glob("*.py")) + [SAMPLE_MODULE]

# Field names of ``DOMElementNode`` / ``DOMState`` that only mean
# something to a serializer: the back-references, the private payload,
# the type dispatch, the cycle-marker id. Every one of these was
# hardcoded here before.
BROWSER_DOM_IDENTIFIERS = (
    "parent",
    "content_document",
    "shadow_roots",
    "_compound_children",
    "tag_name",
    "backend_node_id",
)


def test_there_are_modules_to_check() -> None:
    assert len(SHARED_MODULES) >= 4


@pytest.mark.parametrize("module", SHARED_MODULES, ids=lambda p: p.name)
def test_no_domain_field_name_appears(module: Path) -> None:
    source = module.read_text()
    leaked = [name for name in BROWSER_DOM_IDENTIFIERS if name in source]
    assert not leaked, (
        f"{module.name} names browser-DOM field(s) {leaked}. A fragment that "
        f"needs a domain's internal shape belongs in that domain's package "
        f"(see core/capabilities/browser/observations.py), not here."
    )


@pytest.mark.parametrize("module", SHARED_MODULES, ids=lambda p: p.name)
def test_nothing_imports_a_capability(module: Path) -> None:
    tree = ast.parse(module.read_text(), filename=str(module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.startswith("spectragent.core.capabilities")]
    assert not offenders, (
        f"{module.name} imports {offenders}. The dependency runs the other "
        f"way: capabilities import the fragment base, never the reverse."
    )


def test_browser_fragments_are_registered_from_the_browser_package() -> None:
    # Imported here, not at module scope, and this is the whole point of
    # the test: the browser package is what registers these keys, so
    # leaning on some *other* test module to have imported it first would
    # make the assertion prove nothing (and this file fails on its own).
    import spectragent.core.capabilities.browser.observations  # noqa: F401
    from spectragent.schemas.observation._registrar import ObservationRegistrar

    for key in ("browser.dom", "browser.screenshot", "browser.browser_meta"):
        cls = ObservationRegistrar.get(key)
        assert cls.__module__ == "spectragent.core.capabilities.browser.observations"


def _imports(module: Path) -> list[str]:
    tree = ast.parse(module.read_text(), filename=str(module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def test_the_sample_imports_nothing_from_core() -> None:
    """``schemas/sample.py`` sits below ``core``: the trace and the export import it."""
    offenders = [name for name in _imports(SAMPLE_MODULE) if name.startswith("spectragent.core")]
    assert not offenders, f"schemas/sample.py imports {offenders}"


#: Words a sample field must not be named after: a page, an element, a box
#: on one, or money. Domain detail belongs in a translator's opaque payload,
#: and an export carries tokens, never prices (arch §4.15, §10).
_FORBIDDEN_FIELD_WORDS = {"url", "dom", "bbox", "bounds", "html", "xpath", "price", "cost", "usd"}


def test_no_sample_field_names_a_page_an_element_or_money() -> None:
    from pydantic import BaseModel

    models = [
        value
        for value in vars(sample_module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == sample_module.__name__
    ]
    assert len(models) >= 10
    leaked = sorted(
        f"{model.__name__}.{name}"
        for model in models
        for name in model.model_fields
        if set(name.lower().split("_")) & _FORBIDDEN_FIELD_WORDS
    )
    assert not leaked, f"sample fields named after a domain or money: {leaked}"
