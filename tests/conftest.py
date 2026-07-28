"""Session bootstrap: make the suite runnable from a clean checkout.

``spectragent/generated/`` is gitignored, so ``_composed.py`` — the
codegen artifact — is absent after ``git clone`` and is only written by
an explicit ``spectragent-gen`` run. Nine tests import it (the codegen
tripwires in ``tests/test_generated/``, plus
``tests/test_evaluator/test_registrar.py`` and
``TestMouseTrajectoryIsConstrained``), and there is no CI step, build
hook or Makefile that generates it first — so the suite simply could not
pass on a fresh clone.

Generating it here rather than skipping keeps the coverage: these tests
exist precisely to catch codegen emitting something unimportable, and a
``pytest.importorskip`` would switch them off in the one situation
(fresh clone, first run) where a codegen regression is most likely to go
unnoticed.

**In a subprocess, deliberately.** ``generate_all.main()`` unlinks
``_composed.py`` and then ``discover("spectragent")``s the whole package,
which imports ``spectragent.generated`` while the file is missing and
pins ``ComposedConfig`` to ``None`` in ``sys.modules`` for the rest of
the process. Running it in-process would therefore *cause* the failure it
is meant to prevent.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSED = _REPO_ROOT / "spectragent" / "generated" / "_composed.py"


def _ensure_generated_config() -> None:
    """Run codegen once if its output is missing. A no-op otherwise."""
    if _COMPOSED.exists():
        return

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "spectragent.scripts.generate_all"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not _COMPOSED.exists():
        # Not fatal: let the tests that need the artifact fail with their
        # own messages rather than making the whole session uncollectable.
        warnings.warn(
            "Could not generate spectragent/generated/_composed.py "
            f"(exit {result.returncode}). Tests that import the generated "
            "config surface will fail. Run "
            "`uv run python -m spectragent.scripts.generate_all`.\n"
            f"{result.stderr.strip()}",
            RuntimeWarning,
            stacklevel=1,
        )


_ensure_generated_config()
