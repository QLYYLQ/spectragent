"""browseruse-bench — browser agent benchmark framework."""

from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
"""Absolute path to the repository root (the directory containing ``spectragent/``)."""

PACKAGE_ROOT: Path = Path(__file__).resolve().parent
"""Absolute path to the ``spectragent`` package directory."""

BENCHMARK_DATA_ROOT: Path = PACKAGE_ROOT / "benchmark_data"
"""Absolute path to ``spectragent/benchmark_data/``."""

GENERATED_DIR: Path = PACKAGE_ROOT / "generated"
"""Absolute path to ``spectragent/generated/``."""
