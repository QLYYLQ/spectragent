"""Every module of the capability packages imports first in a fresh interpreter.

The package ``__init__.py`` files import no implementation, so nothing fixes
the order in which a capability's modules load any more: a tool may import any
one of them first. An import cycle that a fixed order used to hide then fails
for whoever starts at the wrong module — ``storage_seed`` and the storage
archiver once imported each other half-loaded this way. One interpreter per
module, because a module imported once hides the cycle from every later import.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from spectragent import PACKAGE_ROOT

CAPABILITIES = PACKAGE_ROOT / "core" / "capabilities"


def _modules() -> list[str]:
    """Every module under ``core/capabilities``, by name, found without importing it."""
    names = []
    for path in sorted(CAPABILITIES.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _import_alone(module: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return f"{module}: {(result.stderr.strip().splitlines() or ['no output'])[-1]}"


def test_every_capability_module_imports_on_its_own() -> None:
    modules = _modules()
    assert "spectragent.core.capabilities.browser.storage_seed" in modules
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        failures = [failure for failure in pool.map(_import_alone, modules) if failure]
    assert not failures, failures
