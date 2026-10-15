"""A clean-interpreter fingerprint probe, matching the generator's discovery order.

Building composed Pydantic models normalizes source numeric metadata. Compute
the source fingerprints before that happens, independently of earlier config
inspection calls and application-owned registries in a long-lived caller.
"""

from __future__ import annotations

import json
import subprocess
import sys


def source_fingerprints() -> dict[str, str]:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "spectragent.cli.fingerprints"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("Could not inspect source registries in a clean interpreter")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise RuntimeError("The registry fingerprint probe returned an invalid result")
    return value


def _probe() -> dict[str, str]:
    from conscribe import compute_registry_fingerprint, discover

    from spectragent.scripts.generate_all import _collect_registrars

    discover("spectragent", exclude=["spectragent.generated", "spectragent.scripts"])
    return {
        name: compute_registry_fingerprint(registrar)
        for name, registrar in _collect_registrars().items()
    }


if __name__ == "__main__":
    print(json.dumps(_probe()))
