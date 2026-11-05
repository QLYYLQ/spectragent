"""VenusBench's task list, its loader, the run it composes, and its installation (``docs/architecture_design.md`` §4.10.1, §11.2).

Nothing here downloads, fetches, makes a venv or starts an emulator: the
programs a fetch or an installation runs, and its downloads, are recorded by
stand-ins that do to the disk what each would do.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from spectragent.benchmarks.androidworld import data as androidworld_data
from spectragent.benchmarks.androidworld import install as androidworld_install
from spectragent.benchmarks.androidworld.data import TASK_LIST
from spectragent.benchmarks.androidworld.open_app import OPEN_APP_FILE, OPEN_APP_FORMAT
from spectragent.benchmarks.androidworld.service import FORWARDER_APK, checkout_commit
from spectragent.benchmarks.androidworld.tasks import TASK_LIST_FORMAT, instance_seed
from spectragent.benchmarks.data import BenchmarkSetupError, Say
from spectragent.benchmarks.operations import benchmark_of, fetch_benchmark, set_up_benchmark
from spectragent.benchmarks.selection import read_benchmark
from spectragent.benchmarks.venusbench import install, service
from spectragent.benchmarks.venusbench.install import LOCK, VenusBenchInstaller
from spectragent.benchmarks.venusbench.service import VenusBenchInstallation
from spectragent.benchmarks.venusbench.tasks import UNSUPPORTED, VENUSBENCH
from spectragent.core.data_loader._common import read_data_version, write_data_version
from spectragent.schemas.data_version import DataVersion

APPS = {"markor": "net.gsantner.markor/net.gsantner.markor.activity.MainActivity"}
VERSION = DataVersion(source="git", repository=VENUSBENCH.repository, revision=VENUSBENCH.commit)


def _record(task: str, commit: str = VENUSBENCH.commit) -> dict[str, object]:
    return {
        "commit": commit,
        "family": "venus",
        "task": task,
        "index": 0,
        "suite_seed": 30,
        "params": {"seed": instance_seed(30, task, 0)},
        "goal": f"the goal of {task}",
        "goal_at_initialize": "unchanged",
        "complexity": 2.4,
    }


def _write_lists(out: Path, split: str, commit: str = VENUSBENCH.commit) -> None:
    """What ``task_list.py`` writes: one task run, one left out, and the ``open_app`` table."""
    tasks = [
        _record("VagueSystemBrightnessMax", commit),
        _record("MultiRoundRetroSavePlaylist", commit),
    ]
    (out / f"{split}.json").write_text(json.dumps({"format": TASK_LIST_FORMAT, "tasks": tasks}))
    (out / "data_info.json").write_text(json.dumps({"split": {split: f"{split}.json"}}))
    table = {"format": OPEN_APP_FORMAT, "commit": commit, "apps": APPS}
    (out / OPEN_APP_FILE).write_text(json.dumps(table))


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path / "root"))
    return tmp_path / "root"


def test_fetch_lists_the_venus_family_as_its_runner_makes_its_suite(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._helpers.fake_androidworld import fake_venusbench_installation

    installation = fake_venusbench_installation(
        data_root / "android" / "venusbench" / "installation", monkeypatch
    )
    ran: list[list[str]] = []

    def run(argv: Sequence[str | Path], purpose: str, *, cwd: Path | None = None) -> None:
        command = [str(part) for part in argv]
        ran.append(command)
        _write_lists(Path(command[command.index("--out") + 1]), "All")

    monkeypatch.setattr(androidworld_data, "run_program", run)
    fetched = fetch_benchmark("venusbench", lambda line: None)

    directory = data_root / "android" / "venusbench"
    assert (fetched.directory, fetched.version) == (directory, VERSION)
    (command,) = ran
    assert command[:3] == [str(installation / ".venv" / "bin" / "python"), "-I", str(TASK_LIST)]
    assert command[3:5] == ["--src", str(installation / "src")]
    assert command[7:] == [
        "--holders",
        "venus",
        "--seed",
        "30",
        "--family",
        "venus",
        "--split",
        "All",
    ]
    assert read_data_version(directory) == VERSION


def test_a_list_made_from_androidworld_is_refused(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
    from spectragent.core.data_loader._common import BenchmarkDataError
    from tests._helpers.fake_androidworld import fake_venusbench_installation

    fake_venusbench_installation(data_root / "android" / "venusbench" / "installation", monkeypatch)
    monkeypatch.setattr(
        androidworld_data,
        "run_program",
        lambda argv, purpose, cwd=None: _write_lists(
            Path(argv[list(map(str, argv)).index("--out") + 1]), "All", PINNED_COMMIT
        ),
    )
    with pytest.raises(BenchmarkDataError, match=f"made from VenusBench {PINNED_COMMIT}"):
        fetch_benchmark("venusbench", lambda line: None)


def test_the_loader_runs_the_first_version_and_says_why_it_leaves_out_the_rest(
    data_root: Path,
) -> None:
    directory = data_root / "android" / "venusbench"
    directory.mkdir(parents=True)
    _write_lists(directory, "All")
    write_data_version(directory, VERSION)

    from spectragent.benchmarks.venusbench.loader import VenusBenchTaskLoader

    loader = VenusBenchTaskLoader(subset="All")
    assert loader.task_ids() == ["VagueSystemBrightnessMax/30/0"]
    assert dict(loader.skipped) == {
        "MultiRoundRetroSavePlaylist/30/0": UNSUPPORTED["MultiRoundRetroSavePlaylist"]
    }
    task = loader["VagueSystemBrightnessMax/30/0"]
    assert (task.benchmark, task.env_type, task.tags) == ("venusbench", "android", ["venus"])
    assert loader.get_task_context(task.task_id).loop == {"max_steps": 24}
    assert loader.data_version == VERSION


def test_the_run_takes_venusbenchs_avd_and_its_open_app_table(data_root: Path) -> None:
    directory = data_root / "android" / "venusbench"
    directory.mkdir(parents=True)
    _write_lists(directory, "All")
    cls, block = benchmark_of(read_benchmark("venusbench"))
    (part,) = cls.computed_capability(block)
    assert part == {
        "capability_id": "android",
        "settle_max_wait_s": 6.0,
        "actions": [{"action_id": "android.open_app", "apps": APPS}],
    }


class _Installer:
    """Stands in for git, uv and the downloads, doing to the disk what each does."""

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
            (src / "android_world").mkdir(exist_ok=True)
            (src / "android_world" / "registry.py").write_text("")
        elif command[1] == "venv":
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)

    def download(self, url: str, sha256: str, target: Path, say: Say) -> Path:
        self.ran.append(["download", url])
        target.write_bytes(self.contents[sha256])
        return target

    contents: dict[str, bytes] = {}


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_root: Path) -> _Installer:
    """git, uv and ffmpeg on PATH; every program and download stood in for; both AVDs there."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "uv", "ffmpeg"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    installer = _Installer(data_root / "android" / "venusbench" / "installation")
    blobs = {name: name.encode() for name in ("app", "forwarder", "fitbook", "file")}
    sha = {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}
    installer.contents = {sha[name]: blob for name, blob in blobs.items()}
    # The checkout and the downloads are AndroidWorld's shared steps.
    monkeypatch.setattr(androidworld_install, "run_program", installer)
    monkeypatch.setattr(androidworld_install, "download_file", installer.download)
    monkeypatch.setattr(install, "SERVICE_APP_SHA256", sha["app"])
    monkeypatch.setattr(service, "SERVICE_APP_SHA256", sha["app"])
    monkeypatch.setattr(androidworld_install, "FORWARDER_SHA256", sha["forwarder"])
    monkeypatch.setattr(install, "FITBOOK_SHA256", sha["fitbook"])
    pinned = [service.DriveFile(sha["file"], 4, "drive-id", "zip/a.zip")]
    monkeypatch.setattr(install, "drive_files", lambda: pinned)
    monkeypatch.setattr(service, "drive_files", lambda: pinned)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "avd"))
    (tmp_path / "avd").mkdir()
    for avd in ("AndroidWorldAvd", "VenusBenchAvd"):
        (tmp_path / "avd" / f"{avd}.ini").write_text("path=x\n")
    return installer


def test_setup_installs_each_step_then_skips_them(installer: _Installer, tmp_path: Path) -> None:
    root = installer.root
    assert set_up_benchmark("venusbench", lambda line: None) == [
        "the benchmark venusbench is set up"
    ]

    git, uv = str(tmp_path / "bin" / "git"), str(tmp_path / "bin" / "uv")
    src, python = str(root / "src"), str(root / ".venv" / "bin" / "python")
    assert installer.ran == [
        [git, "-C", src, "init", "--quiet"],
        [git, "-C", src, "remote", "add", "origin", VENUSBENCH.repository],
        [git, "-C", src, "fetch", "--quiet", "--depth", "1", "origin", VENUSBENCH.commit],
        [git, "-C", src, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach"]
        + [VENUSBENCH.commit],
        [uv, "venv", "--quiet", "--python", "3.11", str(root / ".venv")],
        [uv, "pip", "sync", "--python", python, str(LOCK)],
        ["download", service.SERVICE_APP_URL],
        ["download", androidworld_install.FORWARDER_URL],
        ["download", install.DRIVE_URL.format(id="drive-id")],
        ["download", install.FITBOOK_URL],
    ], "the checkout is never installed into the venv: the launcher puts it on sys.path"
    installation = VenusBenchInstallation(root)
    assert checkout_commit(installation.src) == VENUSBENCH.commit
    assert (root / "cache" / "a11y" / FORWARDER_APK).read_bytes() == b"forwarder"
    assert (installation.files / "zip" / "a.zip").read_bytes() == b"file"
    assert installation.fitbook_apk.read_bytes() == b"fitbook"
    assert installation.problems() == []

    installer.ran.clear()
    set_up_benchmark("venusbench", lambda line: None)
    assert [command[1:3] for command in installer.ran] == [["pip", "sync"]], (
        "what is in place is kept; the venv is synced to the lock again"
    )


def test_without_venusbenchs_avd_the_steps_to_make_it_are_named(
    installer: _Installer, tmp_path: Path
) -> None:
    (tmp_path / "avd" / "VenusBenchAvd.ini").unlink()
    with pytest.raises(BenchmarkSetupError) as refused:
        set_up_benchmark("venusbench", lambda line: None)
    message = str(refused.value)
    assert f"there is no AVD named VenusBenchAvd in {tmp_path / 'avd'}: make it from" in message
    assert f"copy {tmp_path / 'avd'}/AndroidWorldAvd.avd to" in message
    assert str(install.SETUP_APPS) in message and "--fitbook-apk" in message


def test_without_androidworlds_avd_its_setup_comes_first(
    installer: _Installer, tmp_path: Path
) -> None:
    for avd in ("AndroidWorldAvd", "VenusBenchAvd"):
        (tmp_path / "avd" / f"{avd}.ini").unlink()
    with pytest.raises(BenchmarkSetupError, match="spectragent benchmark setup androidworld"):
        set_up_benchmark("venusbench", lambda line: None)


def test_the_installer_reads_the_avd_the_benchmark_names(tmp_path: Path) -> None:
    cls, block = benchmark_of(read_benchmark("venusbench"))
    installer = cls.installation(block)
    assert isinstance(installer, VenusBenchInstaller)
    assert (installer.avd, installer.avd_home) == ("VenusBenchAvd", None)


def test_the_lock_installs_no_checkout_of_a_machines_own() -> None:
    lines = [line for line in LOCK.read_text().splitlines() if line and not line.startswith("#")]
    assert all("==" in line and "file:" not in line for line in lines), lines
    pinned = dict(line.split("==") for line in lines)
    assert pinned["android-env"] == "1.2.3" and pinned["protobuf"] == "6.31.1"
    assert {"fastapi", "uvicorn", "pysqlite3", "uiautomator2", "pyyaml"} <= set(pinned)


def test_the_manifest_pins_every_external_file() -> None:
    files = service.drive_files()
    assert len(files) == 229 and len({item.path for item in files}) == 229
    assert sum(item.size for item in files) == 176_627_081
    assert all(len(item.sha256) == 64 for item in files)
