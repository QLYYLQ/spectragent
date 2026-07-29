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

SHARED_DIR = Path(shared_pkg.__file__).parent
SHARED_MODULES = sorted(SHARED_DIR.glob("*.py"))

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
    offenders = [
        name for name in imported if name.startswith("spectragent.core.capabilities")
    ]
    assert not offenders, (
        f"{module.name} imports {offenders}. The dependency runs the other "
        f"way: capabilities import the fragment base, never the reverse."
    )


def test_browser_fragments_are_registered_from_the_browser_package() -> None:
    from spectragent.schemas.observation._registrar import ObservationRegistrar

    for key in ("browser.dom", "browser.screenshot", "browser.browser_meta"):
        cls = ObservationRegistrar.get(key)
        assert cls.__module__ == "spectragent.core.capabilities.browser.observations"
