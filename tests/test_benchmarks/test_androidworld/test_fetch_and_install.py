"""AndroidWorld's task lists, made in its installation, and the installation itself (``docs/architecture_design.md`` §4.10.1 "数据", §11.2).

Nothing here downloads, clones, makes a venv or starts an emulator: the
programs a fetch or an installation runs are recorded by a stand-in that does
to the disk what each would do.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from spectragent.benchmarks.androidworld import data as androidworld_data
from spectragent.benchmarks.androidworld import install
from spectragent.benchmarks.androidworld.data import DATA, TASK_LIST
from spectragent.benchmarks.androidworld.install import LOCK, named_emulator
from spectragent.benchmarks.androidworld.service import (
    ANDROIDWORLD,
    FORWARDER_APK,
    PINNED_COMMIT,
    REPOSITORY,
    Installation,
    checkout_commit,
)
from spectragent.benchmarks.data import BenchmarkSetupError, Say
from spectragent.benchmarks.operations import fetch_benchmark, set_up_benchmark
from spectragent.core.data_loader._common import BenchmarkDataError, read_data_version
from tests._helpers.benchmark_data import ANDROIDWORLD_APPS, androidworld_task
from tests._helpers.fake_androidworld import fake_installation


def _task_list(commit: str = PINNED_COMMIT):
    """A stand-in for ``task_list.py``: it writes what the real one writes, from *commit*."""
    from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FORMAT
    from spectragent.benchmarks.androidworld.tasks import TASK_LIST_FORMAT

    ran: list[list[str]] = []

    def run(argv: Sequence[str | Path], purpose: str, *, cwd: Path | None = None) -> None:
        command = [str(part) for part in argv]
        ran.append(command)
        out = Path(command[command.index("--out") + 1])
        split = command[command.index("--split") + 1]
        task = {**androidworld_task(), "commit": commit}
        (out / f"{split}.json").write_text(
            json.dumps({"format": TASK_LIST_FORMAT, "tasks": [task]})
        )
        info = out / "data_info.json"
        listed = json.loads(info.read_text()) if info.exists() else {}
        listed.setdefault("split", {})[split] = f"{split}.json"
        info.write_text(json.dumps(listed))
        table = {"format": OPEN_APP_FORMAT, "commit": commit, "apps": ANDROIDWORLD_APPS}
        (out / "open_app.json").write_text(json.dumps(table))

    return run, ran


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The benchmark data root, holding a working AndroidWorld installation where the setup looks."""
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "root"))
    fake_installation(tmp_path / "root" / "android" / "androidworld" / "installation", monkeypatch)
    return tmp_path / "root"


def test_fetch_lists_both_subsets_with_the_installations_interpreter(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, ran = _task_list()
    monkeypatch.setattr(androidworld_data, "run_program", run)

    fetched = fetch_benchmark("androidworld", lambda line: None)

    directory = root / "android" / "androidworld"
    installation = directory / "installation"
    assert (fetched.directory, fetched.version) == (directory, DATA)
    assert DATA.revision == PINNED_COMMIT and DATA.source == "git"
    common = [str(installation / ".venv" / "bin" / "python"), "-I", str(TASK_LIST)]
    assert [command[:3] for command in ran] == [common, common]
    assert [command[3:5] for command in ran] == [["--src", str(installation / "src")]] * 2
    assert [command[-4:] for command in ran] == [
        ["--family", "android_world", "--split", "All"],
        ["--family", "miniwob", "--split", "miniwob"],
    ]
    assert json.loads((directory / "data_info.json").read_text()) == {
        "split": {"All": "All.json", "miniwob": "miniwob.json"}
    }
    assert read_data_version(directory) == DATA
    assert (installation / "src" / "server" / "android_server.py").is_file(), "left in place"


def test_lists_made_from_another_commit_are_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _task_list(commit="0" * 40)
    monkeypatch.setattr(androidworld_data, "run_program", run)
    with pytest.raises(BenchmarkDataError, match=f"made from AndroidWorld {'0' * 40}"):
        fetch_benchmark("androidworld", lambda line: None)
    assert read_data_version(root / "android" / "androidworld") is None


def test_fetch_without_the_installation_names_the_setup_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))
    with pytest.raises(BenchmarkDataError) as refused:
        fetch_benchmark("androidworld", lambda line: None)
    installation = tmp_path / "android" / "androidworld" / "installation"
    assert str(refused.value) == (
        "AndroidWorld's task lists are made by its own code, in its installation, and there is "
        f"no AndroidWorld installation at {installation}: install it first with spectragent "
        "benchmark setup androidworld"
    )


def test_an_installation_elsewhere_is_named_by_its_root(tmp_path: Path) -> None:
    problems = Installation(tmp_path / "aw").problems()
    assert (
        f"spectragent benchmark setup <a benchmark YAML whose androidworld setup has root "
        f"{tmp_path / 'aw'}> installs AndroidWorld there"
    ) in problems


class _Installer:
    """Stands in for git and uv, doing to the disk what each command does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ran: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str | Path], purpose: str, *, cwd: Path | None = None
    ) -> None:
        command = [str(part) for part in argv]
        self.ran.append(command)
        src = self.root / "src"
        if command[3:4] == ["init"]:
            (src / ".git").mkdir(parents=True)
            (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        elif "checkout" in command:
            (src / ".git" / "HEAD").write_text(f"{command[-1]}\n")
            (src / "server").mkdir(exist_ok=True)
            (src / "server" / "android_server.py").write_text("")
        elif command[1] == "venv":
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Installer:
    """git, uv and ffmpeg on PATH, every program run and the forwarder download stood in for."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "uv", "ffmpeg"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "root"))
    installer = _Installer(tmp_path / "root" / "android" / "androidworld" / "installation")
    monkeypatch.setattr(install, "run_program", installer)
    apk = b"forwarder"
    monkeypatch.setattr(install, "FORWARDER_SHA256", hashlib.sha256(apk).hexdigest())

    def download(url: str, sha256: str, target: Path, say: Say) -> Path:
        assert url == install.FORWARDER_URL and sha256 == install.FORWARDER_SHA256
        installer.ran.append(["download", url])
        target.write_bytes(apk)
        return target

    monkeypatch.setattr(install, "download_file", download)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "avd"))
    (tmp_path / "avd").mkdir()
    (tmp_path / "avd" / "AndroidWorldAvd.ini").write_text("path=x\n")
    return installer


def test_setup_installs_the_checkout_the_venv_and_the_forwarder_then_skips_them(
    installer: _Installer, tmp_path: Path
) -> None:
    root = installer.root
    said: list[str] = []
    assert set_up_benchmark("androidworld", said.append) == ["the benchmark androidworld is set up"]

    bin_dir = tmp_path / "bin"
    python = root / ".venv" / "bin" / "python"
    git, src = str(bin_dir / "git"), str(root / "src")
    assert installer.ran == [
        [git, "-C", src, "init", "--quiet"],
        [git, "-C", src, "remote", "add", "origin", REPOSITORY],
        [git, "-C", src, "fetch", "--quiet", "--depth", "1", "origin", PINNED_COMMIT],
        [
            str(bin_dir / "git"),
            "-C",
            str(root / "src"),
            "-c",
            "advice.detachedHead=false",
            "checkout",
        ]
        + ["--quiet", "--detach", PINNED_COMMIT],
        [str(bin_dir / "uv"), "venv", "--quiet", "--python", "3.11", str(root / ".venv")],
        [str(bin_dir / "uv"), "pip", "sync", "--python", str(python), str(LOCK)],
        [str(bin_dir / "uv"), "pip", "install", "--python", str(python), "--no-deps", "-e"]
        + [str(root / "src")],
        ["download", install.FORWARDER_URL],
    ]
    assert checkout_commit(root / "src") == PINNED_COMMIT
    assert (root / "cache" / "a11y" / FORWARDER_APK).read_bytes() == b"forwarder"
    assert Installation(root).problems() == []

    installer.ran.clear()
    set_up_benchmark("androidworld", said.append)
    assert [command[1:3] for command in installer.ran] == [["pip", "sync"], ["pip", "install"]], (
        "a checkout at the commit, a venv and a cached forwarder are kept; the venv is synced to "
        "the lock again, which removes what is not in it and changes nothing else"
    )


def test_a_checkout_at_another_commit_is_moved_to_the_pinned_one(installer: _Installer) -> None:
    src = installer.root / "src"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("0" * 40 + "\n")
    install.check_out(src, ANDROIDWORLD, lambda line: None)
    assert [command[3:5] for command in installer.ran] == [
        ["fetch", "--quiet"],
        ["-c", "advice.detachedHead=false"],
    ]
    assert checkout_commit(src) == PINNED_COMMIT


def test_a_folder_in_the_checkouts_place_is_left_alone(installer: _Installer) -> None:
    (installer.root / "src").mkdir(parents=True)
    with pytest.raises(BenchmarkSetupError, match="is there and is no git checkout: move it away"):
        set_up_benchmark("androidworld", lambda line: None)
    assert installer.ran == []


def test_what_only_the_user_installs_is_named(
    installer: _Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin" / "ffmpeg").unlink()
    (tmp_path / "avd" / "AndroidWorldAvd.ini").unlink()
    with pytest.raises(BenchmarkSetupError) as refused:
        set_up_benchmark("androidworld", lambda line: None)
    message = str(refused.value)
    assert message.startswith("the benchmark androidworld is not set up yet: there is no ffmpeg")
    assert f"there is no AVD named AndroidWorldAvd in {tmp_path / 'avd'}: create it" in message
    assert "set hw.keyboard=yes in its config.ini" in message
    assert "run.py --perform_emulator_setup" in message
    assert installer.ran, "what it can install, it installed"


def test_without_git_or_uv_setup_says_which(installer: _Installer, tmp_path: Path) -> None:
    (tmp_path / "bin" / "uv").unlink()
    with pytest.raises(BenchmarkSetupError, match="installing AndroidWorld needs uv on PATH"):
        set_up_benchmark("androidworld", lambda line: None)


def test_the_avd_is_checked_only_when_the_benchmark_names_it(tmp_path: Path) -> None:
    from spectragent.benchmarks.operations import benchmark_of

    block = {
        "benchmark_id": "androidworld",
        "task_loader": {"benchmark_name": "androidworld"},
        "setup": {"setup_id": "androidworld"},
    }
    backend = {"provider_id": "local_emulator", "avd": "Mine", "avd_home": "/avds"}
    _, named = benchmark_of(
        {**block, "capability": [{"capability_id": "android", "backend": backend}]}
    )
    assert named_emulator(named) == ("Mine", "/avds")
    _, unnamed = benchmark_of(block)
    assert named_emulator(unnamed) == (None, None)


def test_the_lock_installs_no_checkout_of_a_machines_own() -> None:
    """The lock ships in the wheel: a line naming a path on the machine it was made on would break
    every other one."""
    lines = [line for line in LOCK.read_text().splitlines() if line and not line.startswith("#")]
    assert all("==" in line and "file:" not in line for line in lines), lines
    assert {"fastapi", "uvicorn", "android-env", "grpcio"} <= {
        line.split("==")[0] for line in lines
    }
