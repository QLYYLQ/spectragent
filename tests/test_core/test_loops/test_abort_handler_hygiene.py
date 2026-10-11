"""Every blanket ``except Exception`` in a Loop must let ``RunAbort`` out.

B5 is not a bug that was fixed once. It is a shape that has to hold at
every place a Loop catches everything, forever, in every Loop that gets
written — ``CodeActLoop`` and ``PlanExecLoop`` are in the design and do
not exist yet. The obligation used to live in a docstring, which is worth
nothing: a new phase written with only ``except Exception`` reintroduces
B5 for that phase, no test turns red, and the guard's veto is silently
flattened into ``agent_done="error"`` again.

So this file reads the package's own source. For every ``try`` in
``spectragent/core/loops/*.py`` whose handlers include a bare
``except Exception``, one of the two shapes ReactLoop actually uses must
be present:

* **handle it inline** — an ``except RunAbort`` handler *before* the
  blanket one (Python matches handlers top-down), which records the
  interrupted step and re-raises;
* **capture and re-raise** — the blanket handler binds the exception to a
  variable so the failure can be attributed to one unit of work among
  several, and the enclosing function later does
  ``isinstance(<that variable>, RunAbort)`` and re-raises. This is what
  the per-action loop does, because the step record it builds on the
  ordinary path carries the actions and results a bare abort record
  cannot.

A third answer is allowed and must be written down: the marker comment
below, on the ``except Exception`` line, for a handler that genuinely
cannot be vetoing anything (cleanup, logging). It is deliberately ugly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import spectragent.core.loops as loops_pkg

#: Opt out of the rule, in place, with a reason. Grep-able on purpose.
_EXEMPT = "RunAbort-exempt:"

_PACKAGE_DIR = Path(loops_pkg.__file__).parent
# Recursive: a Loop's helpers live in subpackages too (``restore/``, whose
# restorers execute recorded actions through ``act_fn``), and a blanket
# handler there swallows a veto exactly as one in ``react.py`` would.
_MODULES = sorted(p for p in _PACKAGE_DIR.rglob("*.py") if p.name != "__init__.py")


def _is_bare_exception(node: ast.expr | None) -> bool:
    """``except Exception`` / ``except (Exception, ...)`` — the blanket."""
    if isinstance(node, ast.Name):
        return node.id == "Exception"
    if isinstance(node, ast.Tuple):
        return any(_is_bare_exception(elt) for elt in node.elts)
    return False


def _mentions_run_abort(node: ast.expr | None) -> bool:
    return (
        any(isinstance(child, ast.Name) and child.id == "RunAbort" for child in ast.walk(node))
        if node is not None
        else False
    )


def _captured_names(handler: ast.ExceptHandler) -> set[str]:
    """Variables the handler assigns the caught exception to.

    ``except Exception as exc: act_error = exc`` -> ``{"act_error"}``.
    That variable is the only way a deferred re-raise can find the
    exception again once the handler has exited.
    """
    if handler.name is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id == handler.name
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _reraises_abort(scope: ast.AST, names: set[str]) -> bool:
    """Does *scope* test one of *names* for ``RunAbort`` and raise it?"""
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        if len(node.args) != 2:
            continue
        subject, wanted = node.args
        if isinstance(subject, ast.Name) and subject.id in names and _mentions_run_abort(wanted):
            return True
    return False


def _functions_with_tries(
    module: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Module, ast.Try]]:
    """Pair every ``try`` with its innermost enclosing function."""
    found: list[tuple[ast.AST, ast.Try]] = []

    def walk(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            if isinstance(child, ast.Try):
                found.append((scope, child))
            walk(child, inner)

    walk(module, module)
    return found  # type: ignore[return-value]


def _offenders(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    module = ast.parse(source)
    bad: list[str] = []

    for scope, node in _functions_with_tries(module):
        for idx, handler in enumerate(node.handlers):
            if not _is_bare_exception(handler.type):
                continue
            line = lines[handler.lineno - 1]
            if _EXEMPT in line:
                continue
            earlier_abort = any(_mentions_run_abort(h.type) for h in node.handlers[:idx])
            if earlier_abort:
                continue
            if _reraises_abort(scope, _captured_names(handler)):
                continue
            where = getattr(scope, "name", "<module>")
            bad.append(f"{path.name}:{handler.lineno} in {where}()")
    return bad


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.relative_to(_PACKAGE_DIR).as_posix())
def test_blanket_handlers_let_run_abort_through(path: Path) -> None:
    offenders = _offenders(path)
    assert not offenders, (
        "These `except Exception` handlers can swallow a RunAbort — a guard's "
        "veto would come back as an ordinary failed AgentResult (B5):\n  "
        + "\n  ".join(offenders)
        + "\n\nAdd `except RunAbort as exc:` above it (record the step via "
        "_note_abort, then bare `raise`), or capture the exception and "
        "re-raise it after `isinstance(<var>, RunAbort)`. If the handler "
        f"truly cannot be vetoing anything, put `# {_EXEMPT} <reason>` on "
        "the `except` line."
    )


class TestTheRuleItself:
    """The checker has to fail for the right reason, or it pins nothing."""

    @staticmethod
    def _check(src: str) -> list[str]:
        module = ast.parse(src)
        lines = src.splitlines()
        bad = []
        for scope, node in _functions_with_tries(module):
            for idx, handler in enumerate(node.handlers):
                if not _is_bare_exception(handler.type):
                    continue
                if _EXEMPT in lines[handler.lineno - 1]:
                    continue
                if any(_mentions_run_abort(h.type) for h in node.handlers[:idx]):
                    continue
                if _reraises_abort(scope, _captured_names(handler)):
                    continue
                bad.append(handler.lineno)
        return bad

    def test_a_lone_blanket_handler_is_caught(self) -> None:
        assert self._check("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")

    def test_an_abort_handler_below_the_blanket_does_not_count(self) -> None:
        """Python matches top-down — order is the whole point."""
        assert self._check(
            "def f():\n"
            "    try:\n"
            "        g()\n"
            "    except Exception:\n"
            "        pass\n"
            "    except RunAbort:\n"
            "        raise\n"
        )

    def test_an_abort_handler_above_the_blanket_passes(self) -> None:
        assert not self._check(
            "def f():\n"
            "    try:\n"
            "        g()\n"
            "    except RunAbort:\n"
            "        raise\n"
            "    except Exception:\n"
            "        pass\n"
        )

    def test_capture_and_deferred_reraise_passes(self) -> None:
        """The act-phase shape."""
        assert not self._check(
            "def f():\n"
            "    err = None\n"
            "    try:\n"
            "        g()\n"
            "    except Exception as exc:\n"
            "        err = exc\n"
            "    if isinstance(err, RunAbort):\n"
            "        raise err\n"
        )

    def test_capturing_a_different_variable_does_not_pass(self) -> None:
        """A near-miss must still fail — this is what rot looks like."""
        assert self._check(
            "def f():\n"
            "    try:\n"
            "        g()\n"
            "    except Exception as exc:\n"
            "        other = exc\n"
            "    if isinstance(unrelated, RunAbort):\n"
            "        raise\n"
        )

    def test_the_exemption_marker_works(self) -> None:
        assert not self._check(
            "def f():\n"
            "    try:\n"
            "        g()\n"
            f"    except Exception:  # {_EXEMPT} teardown, nothing to veto\n"
            "        pass\n"
        )


def test_the_rule_has_something_to_check() -> None:
    """A rule that matches nothing passes forever.

    If ReactLoop's blanket handlers are ever restructured away, this file
    should be revisited rather than left green by vacuity.
    """
    blanket = 0
    for path in _MODULES:
        module = ast.parse(path.read_text(encoding="utf-8"))
        for _scope, node in _functions_with_tries(module):
            blanket += sum(1 for h in node.handlers if _is_bare_exception(h.type))
    assert blanket >= 3, f"expected ReactLoop's three phase handlers, found {blanket}"


@pytest.mark.parametrize("module", ["restore/prefix_replay.py", "branch.py", "_registrar.py"])
def test_the_scan_reaches_every_module_that_handles_a_veto(module: str) -> None:
    """The restorer and ``BranchLoop`` catch around ``act_fn`` and the environment.

    A scan that stopped at the package's top level would let a blanket
    handler in ``restore/`` swallow a veto unseen.
    """
    assert _PACKAGE_DIR / module in _MODULES
