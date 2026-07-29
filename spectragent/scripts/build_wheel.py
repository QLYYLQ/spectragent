"""Build a wheel that depends only on the commit, not on the disk.

    uv run spectragent-build                    # via [project.scripts]
    uv run python -m spectragent.scripts.build_wheel

``uv build`` on its own is not reproducible here, because setuptools keeps
two caches that it never prunes:

- ``build/lib`` — the staging copy of the package. A file deleted from the
  source tree is still sitting there and is still copied into the next
  wheel. ``.gitignore`` already documents this for modules; it applies
  verbatim to data files. Measured: with ``spectragent/benchmark_data/``
  gone from both git and the working tree, a stale ``build/lib`` still
  shipped all 17 data files, while the same tree with ``build/`` removed
  shipped none.
- ``spectragent.egg-info/SOURCES.txt`` — the file manifest. Worse, because
  it survives ``rm -rf build``: dropping ``benchmark_data/**/*`` from
  ``package-data`` changed nothing until that file was regenerated.

The manifest half is fixed at the declaration — ``include-package-data =
false`` in ``pyproject.toml`` makes the wheel's data content exactly the
``package-data`` globs and nothing a cache remembers. The staging half
cannot be declared away, so the build path owns it: wipe, then build.
Everything that builds a wheel — humans and the packaging test alike —
goes through ``build_wheel``, so there is only one such path to keep
honest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from spectragent import REPO_ROOT

BUILD_CACHE: Path = REPO_ROOT / "build"
"""setuptools' non-pruning staging tree."""


class WheelBuildError(RuntimeError):
    """Raised when the wheel build command fails."""


def clean_build_cache() -> None:
    """Remove setuptools' staging tree and stale file manifests. Idempotent."""
    shutil.rmtree(BUILD_CACHE, ignore_errors=True)
    for egg_info in REPO_ROOT.glob("*.egg-info"):
        shutil.rmtree(egg_info, ignore_errors=True)


def build_wheel(out_dir: Path) -> Path:
    """Build a wheel into ``out_dir`` from a pruned tree and return its path."""
    clean_build_cache()
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WheelBuildError(
            f"`uv build --wheel` failed (exit {result.returncode}).\n"
            f"{result.stdout}\n{result.stderr}"
        )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise WheelBuildError(
            f"expected exactly one wheel in {out_dir}, found {[w.name for w in wheels]}"
        )
    # Leave no stale tree behind either: the next build must not inherit
    # this one's copy of the package.
    clean_build_cache()
    return wheels[0]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = Path(argv[0]) if argv else REPO_ROOT / "dist"
    try:
        wheel = build_wheel(out_dir)
    except WheelBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Wrote {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
