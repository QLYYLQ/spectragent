"""The generated config must describe the registries as they are *now*.

``spectragent/scripts/generate_all.py`` writes ``.fingerprint.json`` and its
own docstring calls it "a content fingerprint used by CI to assert the
generated file is in sync with the current registry state". The write half
shipped; the read half never did — before this test, ``fingerprint`` appeared
nowhere under ``tests/``, so nothing ever compared it to anything.

That gap is invisible by construction. Change a ``__wiring__`` entry or an
``Annotated`` constructor field, forget to regenerate, and ``_composed.py``
does not become *broken* — it becomes **stale**, which is a different thing.
It still imports, it is still internally consistent, and every test that reads
it still passes. It simply describes an older registry, so a knob that now
exists is unreachable from YAML and a config naming it fails validation with
"extra inputs are not permitted".

The drift is real, not hypothetical: this test was written immediately after
catching the ``hook`` layer out of sync (``7050f18d0b365460`` on disk against
``15cb44d3518a26fb`` live) in a working tree where all 2908 tests were green.

Two reasons nothing else catches it:

- ``tests/conftest.py::_ensure_generated_config`` regenerates only when
  ``_composed.py`` is **missing**. A stale file exists, so it is left alone —
  correctly, since regenerating on every session would hide exactly this.
- ``spectragent/generated/`` is gitignored apart from ``__init__.py`` and
  ``.fingerprint.json``, so regenerating produces almost no diff and code
  review sees no signal either.

**In a subprocess, deliberately** — the same reason ``tests/conftest.py``
gives for its own. A fingerprint is a hash of a registry's *contents*, and by
the time this module runs inside the full suite, other test modules have
imported stub capabilities, agents, actions and environments into those very
registries. Computing the hash in the pytest interpreter therefore measures
the suite's fixtures, not the shipped code: run alone it passed, run in the
suite it reported four layers drifting. Only a clean interpreter sees what
``spectragent-gen`` sees.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FINGERPRINT = _REPO_ROOT / "spectragent" / "generated" / ".fingerprint.json"

_REGENERATE = (
    "Regenerate with `uv run spectragent-gen` "
    "(or `python -m spectragent.scripts.generate_all`)."
)

#: Mirrors what ``generate_all.main()`` fingerprints, and nothing else.
_PROBE = """
import json
from conscribe import compute_registry_fingerprint
from spectragent.scripts.generate_all import _collect_registrars, discover

discover("spectragent")
print(json.dumps({
    layer: compute_registry_fingerprint(registrar)
    for layer, registrar in _collect_registrars().items()
}))
"""


@pytest.fixture(scope="module")
def live_fingerprints() -> dict[str, str]:
    """Fingerprint every collected registrar in a clean interpreter."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "Could not fingerprint the registries in a subprocess "
        f"(exit {result.returncode}).\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def recorded_fingerprints() -> dict[str, str]:
    assert _FINGERPRINT.exists(), (
        f"{_FINGERPRINT} is missing. It is written by codegen. {_REGENERATE}"
    )
    return json.loads(_FINGERPRINT.read_text())


def test_generated_config_matches_the_live_registries(
    live_fingerprints: dict[str, str], recorded_fingerprints: dict[str, str]
) -> None:
    """Every collected registrar hashes to what codegen last recorded."""
    drifted = {
        layer: (recorded_fingerprints.get(layer), current)
        for layer, current in live_fingerprints.items()
        if recorded_fingerprints.get(layer) != current
    }
    assert not drifted, (
        "spectragent/generated/ is stale — it describes an older registry, so "
        "a knob that exists in the source is unreachable from YAML.\n"
        + "\n".join(
            f"  {layer}: recorded {recorded!r} but the registry now hashes to {current!r}"
            for layer, (recorded, current) in sorted(drifted.items())
        )
        + f"\n{_REGENERATE}"
    )


def test_every_collected_registrar_is_fingerprinted(
    live_fingerprints: dict[str, str], recorded_fingerprints: dict[str, str]
) -> None:
    """The recorded layers are exactly the collected ones.

    Separate from the drift check because the failure has a different cause
    and a different fix. A registrar added to ``_collect_registrars`` without
    regenerating shows up here as a *missing* layer; one removed from the code
    but left in the file shows up as an extra. Folding both into the hash
    comparison above would report "recorded None" and send the reader looking
    for a hash mismatch that is not the problem.
    """
    missing = sorted(set(live_fingerprints) - set(recorded_fingerprints))
    extra = sorted(set(recorded_fingerprints) - set(live_fingerprints))
    assert not missing and not extra, (
        "The fingerprint file and the collected registrars disagree about "
        "which layers exist.\n"
        f"  collected but never fingerprinted: {missing or 'none'}\n"
        f"  fingerprinted but no longer collected: {extra or 'none'}\n"
        f"{_REGENERATE}"
    )
