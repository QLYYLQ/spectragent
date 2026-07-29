"""What the wheel carries — asserted against a wheel, not against a glob.

Two facts about this distribution were previously enforced by nothing at
all, and both are invisible from inside a source checkout because an
editable install reads the repository directly:

1. **The prompt tree must ship.** ``PROMPT_PATH`` is a filesystem path
   inside the installed package, and the leaf prompt directories carry no
   ``__init__.py``, so ``packages.find`` does not see them —
   ``[tool.setuptools.package-data]`` is the only thing putting those files
   in the wheel. Drop that glob and every agent and evaluator raises
   ``FileNotFoundError`` on its first ``prepare()`` after ``pip install``.

2. **The benchmark data must not ship.** It is a licensed dataset on its
   way out of git (``.gitignore`` already lists it; the 17 files are still
   tracked, so the rule is inert until they are removed), and globbing it
   made the wheel a function of the build machine's disk rather than of
   the commit.

Nothing here hardcodes a file count: the expected set is read off the
source tree, so adding a prompt cannot silently fail to be packaged.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from spectragent import PACKAGE_ROOT, REPO_ROOT
from spectragent.prompts import PROMPT_PATH
from spectragent.scripts.build_wheel import BUILD_CACHE, build_wheel

_PACKAGE = PACKAGE_ROOT.name


def _source_data_files(root: Path) -> set[str]:
    """Non-Python runtime files under ``root``, as wheel-relative names."""
    return {
        f"{_PACKAGE}/{path.relative_to(PACKAGE_ROOT).as_posix()}"
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix not in {".py", ".pyc", ".pyi"}
        and "__pycache__" not in path.parts
    }


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Every archive member of a freshly built wheel.

    Skips only when ``uv`` itself is unavailable. A build that *runs* and
    fails is a packaging regression and must be reported as a failure —
    that is the whole point of the gate.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH; cannot build a wheel to inspect")
    wheel = build_wheel(tmp_path_factory.mktemp("wheel"))
    with zipfile.ZipFile(wheel) as archive:
        return frozenset(archive.namelist())


class TestRuntimeDataShips:
    def test_every_prompt_file_is_in_the_wheel(
        self, wheel_names: frozenset[str]
    ) -> None:
        expected = _source_data_files(PROMPT_PATH)
        assert expected, "no prompt files found on disk — did prompts/ move?"
        assert expected <= wheel_names, sorted(expected - wheel_names)

    def test_the_prompts_an_agent_reads_are_addressable_in_the_wheel(
        self, wheel_names: frozenset[str]
    ) -> None:
        """A spot check on the exact read path, not just on the count.

        ``_build_system_prompt`` resolves ``PROMPT_PATH / ...`` inside the
        installed package, so the wheel has to hold that relative layout.
        """
        for relative in (
            "prompts/agents/browser_use/system_prompt.md",
            "prompts/evaluators/browsecomp/grader_user.txt",
        ):
            source = PACKAGE_ROOT / relative
            assert source.is_file(), f"{relative} moved; update this gate"
            assert f"{_PACKAGE}/{relative}" in wheel_names


class TestBenchmarkDataDoesNotShip:
    def test_no_benchmark_data_member_at_all(
        self, wheel_names: frozenset[str]
    ) -> None:
        """Includes the ``__init__.py`` markers: they were the data's only
        reason to be a package, and they would make the wheel's contents
        depend on whether the dataset happened to be on disk."""
        strays = sorted(
            name
            for name in wheel_names
            if name.startswith(f"{_PACKAGE}/benchmark_data/")
        )
        assert strays == []

    def test_a_stale_build_cache_cannot_resurrect_deleted_data(
        self, tmp_path: Path
    ) -> None:
        """The nondeterminism ``build_wheel`` exists to remove.

        setuptools copies the package into ``build/lib`` and never prunes
        it, so anything planted there survives into the next wheel. Plant a
        file that is not in the source tree and require the built wheel not
        to carry it.
        """
        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; cannot build a wheel to inspect")
        planted = BUILD_CACHE / "lib" / _PACKAGE / "benchmark_data" / "browser"
        planted.mkdir(parents=True, exist_ok=True)
        (planted / "data_info.json").write_text('{"split": {"All": "ghost.jsonl"}}')
        try:
            wheel = build_wheel(tmp_path / "out")
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
        finally:
            shutil.rmtree(BUILD_CACHE, ignore_errors=True)
        assert not [n for n in names if "benchmark_data" in n]


class TestTheDeclarationHoldsTheOtherHalf:
    """``include-package-data = false``, the half no build path can clean.

    ``build_wheel`` deletes ``*.egg-info`` before every build, so the gate
    above can never see the manifest cache: by the time setuptools runs,
    the stale ``SOURCES.txt`` is gone. That makes the declaration the only
    thing standing between a bare ``uv build`` and a wheel whose data
    content is whatever a previous build happened to record — and until
    this test, flipping it back to ``true`` (which is also what deleting
    the line gives you) kept the whole suite green.

    So this one builds *without* cleaning, on purpose. It plants its own
    file rather than a ``benchmark_data`` path: the dataset is on its way
    out of the tree, and a planted path that does not exist on disk would
    make the assertion pass for the wrong reason.
    """

    def test_a_stale_manifest_cannot_resurrect_unglobbed_data(
        self, tmp_path: Path
    ) -> None:
        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; cannot build a wheel to inspect")

        probe_dir = PACKAGE_ROOT / "_stale_manifest_probe"
        egg_info = REPO_ROOT / f"{_PACKAGE}.egg-info"
        relative = f"{_PACKAGE}/_stale_manifest_probe/ghost.json"
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "ghost.json").write_text('{"split": {"All": "ghost"}}')
            egg_info.mkdir(parents=True, exist_ok=True)
            (egg_info / "SOURCES.txt").write_text(f"{relative}\n")

            out_dir = tmp_path / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(  # noqa: S603
                ["uv", "build", "--wheel", "--out-dir", str(out_dir)],  # noqa: S607
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            wheels = sorted(out_dir.glob("*.whl"))
            assert len(wheels) == 1, [w.name for w in wheels]
            with zipfile.ZipFile(wheels[0]) as archive:
                names = archive.namelist()
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
            shutil.rmtree(egg_info, ignore_errors=True)
            shutil.rmtree(BUILD_CACHE, ignore_errors=True)

        assert relative not in names, (
            "a hand-written SOURCES.txt put a file in the wheel that no "
            "package-data glob names — include-package-data is not false"
        )
        # The globs themselves still work, i.e. this is not a wheel that
        # shipped nothing at all.
        assert any(n.startswith(f"{_PACKAGE}/prompts/") for n in names)
