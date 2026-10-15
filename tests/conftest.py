"""Session bootstrap: make the suite runnable from a clean checkout.

``spectragent/generated/`` is gitignored, so ``_composed.py`` — the
codegen artifact — is absent after ``git clone`` and is only written by
an explicit ``spectragent config generate`` run. Nine tests import it (the codegen
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

**In a subprocess, deliberately.** Code generation must discover only shipped
registrations, before collection-time test fakes can affect its schema or
fingerprints. Generated packages are excluded from discovery.

"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from spectragent.core.data_loader._common import resolve_data_dir

pytest_plugins = ["conscribe.testing.pytest_plugin"]


@pytest.fixture(scope="module")
def archive_fakes():
    """Activate the shared archive implementations only for their contract tests."""
    from tests._helpers.archive_fakes import FAKES

    with FAKES.activate():
        yield


@pytest.fixture(autouse=True)
def _isolate_registries(conscribe_isolated):
    """Roll back classes registered inside each test, including failing tests."""
    yield


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


# ── Benchmark data is an optional, out-of-band dependency ────────────
#
# ``spectragent/benchmark_data/`` is not part of the distribution and is
# not guaranteed to be in a checkout. The loaders are deliberately loud
# when it is absent (``BenchmarkDataError`` at construction) because for a
# *run* "no data" and "broken data" both have to stop the world. A test
# session needs the opposite: a dataset that was never fetched is not a
# regression, and 32 red entries that all say ``No data_info.json`` drown
# the one that says something real.
#
# So the tests that read shipped data declare which dataset they read and
# skip themselves when it is absent — at fixture or class granularity,
# never module-level: the five affected files still hold 82 tests that
# build their own data under ``tmp_path`` and must keep running.

_BENCHMARK_DATA_HINT = (
    "Benchmark data is not shipped with spectragent. Point the loaders at a "
    "local copy with SPECTRAGENT_BENCHMARK_DATA=<root>, or set `data_dir:` on "
    "the individual loader — see configs/README.md."
)


def _missing_benchmark_data(datasets: tuple[str, ...]) -> list[str]:
    """Return the ``<env_type>/<benchmark>`` entries that are unusable.

    Resolution goes through ``resolve_data_dir`` rather than
    ``BENCHMARK_DATA_ROOT`` so the skip decision and the loader agree about
    where the data is — otherwise ``SPECTRAGENT_BENCHMARK_DATA`` could
    supply the data and the tests would skip anyway. A directory counts as
    usable only once it declares its splits, which is the same condition
    the loaders raise on.
    """
    absent: list[str] = []
    for dataset in datasets:
        env_type, _, benchmark = dataset.partition("/")
        if not env_type or not benchmark:
            raise ValueError(
                f"requires_benchmark_data expects '<env_type>/<benchmark>', got {dataset!r}"
            )
        if not (resolve_data_dir(env_type, benchmark, None) / "data_info.json").is_file():
            absent.append(dataset)
    return absent


def _benchmark_data_skip_reason(datasets: tuple[str, ...]) -> str | None:
    absent = _missing_benchmark_data(datasets)
    if not absent:
        return None
    return f"benchmark data missing: {', '.join(absent)}. {_BENCHMARK_DATA_HINT}"


def require_benchmark_data(*datasets: str) -> None:
    """Skip from inside a fixture when shipped benchmark data is absent.

    Use this where the data is reached through a fixture: the skip lands on
    exactly the tests that requested the fixture and leaves the rest of the
    module running. Use the ``requires_benchmark_data`` marker instead when
    the data is reached from the test body.
    """
    reason = _benchmark_data_skip_reason(datasets)
    if reason is not None:
        pytest.skip(reason)


def _chrome_skip_reason() -> str | None:
    """Why a real-browser test cannot run here, or ``None`` if it can.

    Resolved the way ``LocalChromeProvider`` resolves it, so a test skips
    exactly when a run would fail to launch the browser.
    """
    from spectragent.core.capabilities.browser.backends.local_chrome import find_chrome

    try:
        find_chrome()
    except FileNotFoundError as exc:
        return f"no Chrome / Chromium to launch: {exc}"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_benchmark_data(*datasets): skip unless every "
        "'<env_type>/<benchmark>' listed resolves to a directory that "
        "declares its splits in data_info.json.",
    )
    config.addinivalue_line(
        "markers",
        "requires_chrome: launches a real headless Chrome; skipped when no "
        "executable is found (CHROME_EXECUTABLE_PATH or the platform's "
        "install locations). Deselect with -m 'not requires_chrome'.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    chrome_reason: str | None = None
    chrome_checked = False
    for item in items:
        for mark in item.iter_markers(name="requires_benchmark_data"):
            reason = _benchmark_data_skip_reason(mark.args)
            if reason is not None:
                item.add_marker(pytest.mark.skip(reason=reason))
                break
        if item.get_closest_marker("requires_chrome") is not None:
            if not chrome_checked:
                chrome_reason, chrome_checked = _chrome_skip_reason(), True
            if chrome_reason is not None:
                item.add_marker(pytest.mark.skip(reason=chrome_reason))
