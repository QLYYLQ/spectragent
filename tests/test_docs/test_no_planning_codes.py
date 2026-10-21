"""Names people read carry no planning code (AGENTS.md §3.6).

A planning code — a local-plan question or revision number, a module id, a
spike or defect label — means something only to whoever holds the planning
document, and most of those documents are not in Git. AGENTS.md §3.6 keeps
them out of everything people read. Most of that list cannot be checked by a
machine (a log line, an error text built at run time); the parts that can are
checked here, by an AST walk of the source, never an import:

- **identifiers** under ``spectragent/``, ``lookahead/`` and ``tests/`` — function, class,
  variable, attribute and parameter names, test names included
  (``Q1_KEEP_STRICT`` and ``test_q4`` were both real);
- **field descriptions and CLI help** — every ``description=``, ``help=`` and
  ``epilog=`` string literal, which is what the generated config schema and
  ``--help`` show (a ``base_time`` description once told every YAML reader that
  a value was "refused at setup until then (M30)", long after it was not).

The flywheel report's own keys, headings and rule texts are pinned by
``tests/test_flywheel/test_report_schema.py``.

**What this does not cover**: comments and docstrings (a comment may cite a
versioned doc by file and section, ``docs/execution_dag.md`` M12 included),
string values built at run time, and the docs themselves.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import spectragent

REPO = Path(spectragent.__file__).resolve().parent.parent
SCANNED = ("spectragent", "lookahead", "tests")
#: A planning code inside free text: a local-plan section, a module id, a question number,
#: a revision or defect label, a spike label, a decision or acceptance file label.
CODE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:PRD|M\d{2}|Q\d|H\d{2}|S2[ab]|dec\d{2}|acc\d{2})(?![A-Za-z0-9])"
)
#: A planning code as one segment of a snake_case or UPPER_CASE identifier.
CODE_IN_NAME = re.compile(r"(?:^|_)(?:prd|m\d{2}|q\d|h\d{2}|s2[ab])(?:_|$)", re.IGNORECASE)
#: The keyword arguments whose string is shown to a person: a field's description (the
#: generated config schema), an argparse parser's description, epilog and help.
SHOWN_KEYWORDS = frozenset({"description", "help", "epilog"})


def _sources() -> Iterator[Path]:
    for base in SCANNED:
        for path in sorted((REPO / base).rglob("*.py")):
            if "generated" not in path.relative_to(REPO).parts and "__pycache__" not in path.parts:
                yield path


def coded_names(source: str) -> list[tuple[int, str]]:
    """Every defined name in *source* with a planning code as one of its segments."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.append(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.append(node.attr)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        line = getattr(node, "lineno", 0)
        found += [(line, name) for name in names if CODE_IN_NAME.search(name)]
    return found


def coded_shown_text(source: str) -> list[tuple[int, str]]:
    """Every planning code in a ``description=`` / ``help=`` / ``epilog=`` string literal."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.keyword) and node.arg in SHOWN_KEYWORDS):
            continue
        parts = [node.value] if isinstance(node.value, ast.Constant) else []
        if isinstance(node.value, ast.JoinedStr):
            parts = [value for value in node.value.values if isinstance(value, ast.Constant)]
        for part in parts:
            if isinstance(part.value, str):
                found += [
                    (node.value.lineno, m.group(0)) for m in CODE_IN_TEXT.finditer(part.value)
                ]
    return found


def test_the_scan_reaches_every_tree() -> None:
    scanned = {path.relative_to(REPO).parts[0] for path in _sources()}
    assert scanned == set(SCANNED)
    assert any(path.name == "report.py" for path in _sources())


def test_no_identifier_carries_a_planning_code() -> None:
    offenders = {
        f"{path.relative_to(REPO)}:{line}": name
        for path in _sources()
        for line, name in coded_names(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, "name it by what it measures or decides (AGENTS.md §3.6)"


def test_no_description_or_help_carries_a_planning_code() -> None:
    offenders = {
        f"{path.relative_to(REPO)}:{line}": code
        for path in _sources()
        for line, code in coded_shown_text(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, "say the fact, not the planning code (AGENTS.md §3.6)"


def test_the_rules_have_teeth() -> None:
    """The leaks this guard was written after, planted: each is found."""
    planted_names = (
        "Q1_KEEP_STRICT = 0.9\n"
        "class Decisions:\n    q4_coverage: int\n"
        "def test_q4_without_branches_is_insufficient(): ...\n"
        "PRD_DECISIONS = ()\n"
    )
    assert {name for _line, name in coded_names(planted_names)} == {
        "Q1_KEEP_STRICT",
        "q4_coverage",
        "test_q4_without_branches_is_insufficient",
        "PRD_DECISIONS",
    }
    planted_text = (
        "Field(description=('valid only with an archive network source (M30); refused at '\n"
        "    'setup until then.'))\n"
        "parser.add_argument('--x', help=f'see PRD §11 for {x}')\n"
    )
    assert [code for _line, code in coded_shown_text(planted_text)] == ["M30", "PRD"]
    clean = "SITE_MIN_FIDELITY = 0.8\ndef test_miss_policy(): ...\nField(description='Chrome 153, §4.14')\n"
    assert coded_names(clean) == [] and coded_shown_text(clean) == []
