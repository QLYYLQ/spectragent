"""What the commands do with a benchmark's data and installation (``docs/architecture_design.md`` §4.10.1 "数据").

The framework never downloads unasked:

- :func:`require_data`, called by every command that reads a benchmark before
  it composes the run (so before the task loader reads anything), refuses
  when the task loader's data directory does not hold the pinned version,
  naming the command that fetches it: ``spectragent benchmark fetch
  <selection>``. ``run`` and ``bench`` fetch it instead when given
  ``--fetch``.
- :func:`fetch_benchmark` (``spectragent benchmark fetch``) fetches the pinned
  version into the task loader's data directory, checked against the pin, and
  records the version beside it (``data_version.json``, written last); the
  loader reads it back and every run records it (``TraceConfig.data_version``).
- :func:`set_up_benchmark` (``spectragent benchmark setup``) installs what
  the benchmark installs. A missing installation is refused in ``prepare()``
  by the component that needs it, naming that command.

The data directory is the task loader's: its ``data_dir`` when the block
writes one, else the benchmark's copy in the shared cache
(``$SPECTRAGENT_BENCHMARK_DATA/<env_type>/<benchmark>``). The cache copy must
be the pinned version, recorded by a fetch. A ``data_dir`` the block writes is
the user's own: data there that no fetch recorded is read as it is, and runs
record no version for it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from spectragent.benchmarks._registrar import BenchmarkRegistrar
from spectragent.benchmarks.data import BenchmarkSetupError, PinnedData, Say, to_stderr
from spectragent.benchmarks.selection import BENCHMARK_SECTION, read_benchmark, read_spec
from spectragent.core.composer import ConfigComposer
from spectragent.core.data_loader._common import (
    DATA_VERSION_FILE,
    BenchmarkDataError,
    read_data_version,
    write_data_version,
)
from spectragent.schemas.benchmark_spec import BenchmarkSpec
from spectragent.schemas.data_version import DataVersion

if TYPE_CHECKING:
    from spectragent.benchmarks._registrar import BenchmarkBase

#: The file a split map is read from (``core/data_loader/_common.py``).
_SPLIT_MAP = "data_info.json"


@dataclass(frozen=True)
class FetchReport:
    """What :func:`fetch_benchmark` fetched, and where."""

    benchmark_id: str
    directory: Path
    version: DataVersion


def benchmark_of(block: Mapping[str, object]) -> tuple[type[BenchmarkBase], BaseModel]:
    """The class of the benchmark *block* selects, and the block validated against it.

    Validated as a run root holding only the block, as ``spectragent eval``
    composes it: nothing is constructed, so no loader reads data yet.

    Raises:
        ComposeError: The block names no benchmark package.
        ValidationError: The block is not a valid block of its benchmark.
    """
    root = {BENCHMARK_SECTION: [dict(block)]}
    config = ConfigComposer.for_config(root).validate(root)
    (validated,) = getattr(config, BENCHMARK_SECTION)
    cls: type[BenchmarkBase] = BenchmarkRegistrar.get(validated.benchmark_id)
    return cls, validated


def require_data(
    selection: str, block: Mapping[str, object], *, fetch: bool = False, say: Say = to_stderr
) -> None:
    """Refuse, or with *fetch* fetch, a benchmark's pinned data its task loader would not find.

    *block* is the one ``--benchmark`` *selection* read (``read_benchmark``).
    Nothing to check for a benchmark that pins no data, or a ``data_dir`` of
    the user's own that holds data no fetch recorded (module docstring).

    Raises:
        BenchmarkDataError: The data directory does not hold the pinned
            version and *fetch* is false; the message names
            ``spectragent benchmark fetch <selection>``.
        Refusal: *fetch* is true and fetching failed.
    """
    cls, config = benchmark_of(block)
    pinned = cls.pinned_data(config)
    if pinned is None:
        return
    directory = cls.data_directory(config)
    problem = _version_problem(cls.__registry_key__, directory, pinned.version, own=_own(config))
    if problem is None:
        return
    if not fetch:
        raise BenchmarkDataError(
            f"{problem}: fetch it with `spectragent benchmark fetch {shlex.quote(selection)}` "
            f"(run and bench fetch it first when given --fetch)"
        )
    _fetch_into(directory, pinned, say)


def read_benchmark_with_data(
    selection: str, *, subset: str | None = None, fetch: bool = False, say: Say = to_stderr
) -> dict[str, object]:
    """The block ``--benchmark`` *selection* names (``read_benchmark``), its pinned data checked.

    What every command that composes a benchmark reads it with, so the check
    comes before its task loader reads anything (:func:`require_data`).

    Raises:
        BenchmarkSelectionError: As ``read_benchmark``.
        BenchmarkDataError: Its pinned data is not there and *fetch* is false.
    """
    block = read_benchmark(selection, subset=subset)
    require_data(selection, block, fetch=fetch, say=say)
    return block


def read_spec_with_data(
    spec: BenchmarkSpec, *, fetch: bool = False, say: Say = to_stderr
) -> dict[str, object]:
    """The block a source run's *spec* selected (``read_spec``), its pinned data checked.

    What a branch task's inherited benchmark is read with, so its task loader
    finds its data as the selected benchmark's does (:func:`read_benchmark_with_data`).

    Raises:
        BenchmarkSelectionError: As ``read_spec``.
        BenchmarkDataError: Its pinned data is not there and *fetch* is false.
    """
    block = read_spec(spec)
    require_data(spec.selection, block, fetch=fetch, say=say)
    return block


def fetch_benchmark(selection: str, say: Say = to_stderr) -> FetchReport:
    """Fetch the pinned data of the benchmark *selection* names into its task loader's data directory.

    Fetches again when the directory already holds the pinned version:
    every download is checked against the pin, so this is how a damaged
    copy is replaced.

    Raises:
        BenchmarkDataError: The benchmark pins no data to fetch.
        Refusal: Fetching failed.
    """
    cls, config = benchmark_of(read_benchmark(selection))
    directory = cls.data_directory(config)
    pinned = cls.pinned_data(config)
    if pinned is None:
        raise BenchmarkDataError(
            f"the benchmark {cls.__registry_key__} pins no data source to fetch: its task "
            f"loader reads {directory}, where its data is put by hand (or set the task "
            f"loader's data_dir to where it is)"
        )
    _fetch_into(directory, pinned, say)
    return FetchReport(cls.__registry_key__, directory, pinned.version)


def set_up_benchmark(selection: str, say: Say = to_stderr) -> list[str]:
    """Install what the benchmark *selection* names installs; the lines saying what is in place.

    Raises:
        BenchmarkSetupError: Something is still missing once it installed what
            it can (a program only the user can install); every problem named.
        Refusal: An installation step failed.
    """
    cls, config = benchmark_of(read_benchmark(selection))
    installation = cls.installation(config)
    if installation is None:
        return [f"the benchmark {cls.__registry_key__} installs nothing"]
    installation.install(say)
    problems = installation.problems()
    if problems:
        raise BenchmarkSetupError(
            f"the benchmark {cls.__registry_key__} is not set up yet: " + "; ".join(problems)
        )
    return [f"the benchmark {cls.__registry_key__} is set up"]


def _own(config: BaseModel) -> bool:
    """Whether the block's task loader names a data directory of its own (``data_dir``)."""
    return getattr(getattr(config, "task_loader", None), "data_dir", None) is not None


def _version_problem(
    benchmark_id: str, directory: Path, pinned: DataVersion, *, own: bool
) -> str | None:
    """Why *directory* does not hold *pinned* for its task loader to read; ``None`` when it does."""
    try:
        found = read_data_version(directory)
    except BenchmarkDataError as exc:
        return str(exc)
    if found == pinned:
        return None
    has_data = (directory / _SPLIT_MAP).is_file()
    if found is None and own and has_data:
        return None
    if found is not None:
        return (
            f"the {benchmark_id} data at {directory} is {found.describe()}, and the benchmark "
            f"pins {pinned.describe()}"
        )
    if has_data:
        return (
            f"the {benchmark_id} data at {directory} has no {DATA_VERSION_FILE}, so no fetch "
            f"wrote it and its version is unknown; the benchmark pins {pinned.describe()}"
        )
    return f"there is no {benchmark_id} data at {directory}"


def _fetch_into(directory: Path, pinned: PinnedData, say: Say) -> None:
    """Fetch *pinned* into *directory*, replacing the files it writes, and record its version last."""
    directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fetching-", dir=directory))
    try:
        pinned.fetch(staging, say)
        written = sorted(staging.iterdir())
        folders = [entry.name for entry in written if not entry.is_file()]
        if not written or folders:
            raise BenchmarkDataError(
                f"fetching {pinned.version.describe()} wrote "
                + (f"folders ({', '.join(folders)}), not only files" if folders else "nothing")
            )
        # Until the new record is written the directory holds no version: an
        # interrupted fetch leaves data no run takes for the pinned one.
        (directory / DATA_VERSION_FILE).unlink(missing_ok=True)
        for entry in written:
            os.replace(entry, directory / entry.name)
        write_data_version(directory, pinned.version)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    say(f"fetched {pinned.version.describe()} into {directory}")


__all__ = [
    "FetchReport",
    "benchmark_of",
    "fetch_benchmark",
    "read_benchmark_with_data",
    "read_spec_with_data",
    "require_data",
    "set_up_benchmark",
]
