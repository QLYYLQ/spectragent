"""Only the browser capability imports agent-cdp.

agent-cdp is the browser capability's in-process event scheduler (AGENTS.md
§4, ``docs/architecture_design.md`` §11). Every other capability owns its own
middle layer: "它们之间不共享 base" (§4.9). A second package importing
agent-cdp would turn a browser implementation detail into a shared base, and
an Android package importing a library named for CDP would tell its readers
the wrong protocol. These tests are the tripwire.

Imports are read statically, every one of them — module level, inside a
function, under ``if TYPE_CHECKING:`` — because a type-only import is still a
dependency of the design.
"""

from __future__ import annotations

import ast
from pathlib import Path

import spectragent

PACKAGE_ROOT = Path(spectragent.__file__).resolve().parent
BROWSER_PACKAGE = PACKAGE_ROOT / "core" / "capabilities" / "browser"


def _imports_agent_cdp(module: Path) -> bool:
    tree = ast.parse(module.read_text(), filename=str(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        if any(name == "agent_cdp" or name.startswith("agent_cdp.") for name in names):
            return True
    return False


def _importers(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if _imports_agent_cdp(path))


def test_the_browser_package_imports_agent_cdp() -> None:
    # Without this the rule below would pass on a scan that finds nothing.
    assert _importers(BROWSER_PACKAGE)


def test_nothing_outside_the_browser_package_imports_agent_cdp() -> None:
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in _importers(PACKAGE_ROOT)
        if not path.is_relative_to(BROWSER_PACKAGE)
    ]
    assert not offenders, (
        f"{offenders} import agent_cdp. agent-cdp is the browser capability's "
        f"internal event scheduler; another capability writes its own middle "
        f"layer behind Protocols defined in its own package."
    )


def test_an_import_inside_a_function_is_found(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("def f():\n    from agent_cdp.scope import EventScope\n")
    assert _imports_agent_cdp(planted)


def test_a_relative_import_of_a_local_module_is_not_counted(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("from .agent_cdp import helper\n")
    assert not _imports_agent_cdp(planted)
