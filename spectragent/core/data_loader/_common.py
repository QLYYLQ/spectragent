"""Shared parsing logic for benchmark data loaders.

Both Task and Eval loaders use these utilities to resolve data
directories, read split maps from ``data_info.json``, and parse
JSONL/JSON data files.

Every failure here is loud. A benchmark whose data is missing,
mis-declared, or empty must fail at loader construction — a zero-length
loader that reports no error is indistinguishable from a benchmark where
the agent scored nothing.
"""

import json
import os
from pathlib import Path
from typing import Any

from spectragent import BENCHMARK_DATA_ROOT

#: Environment variable naming an alternative benchmark-data root.
#:
#: The shipped tree is not part of the distribution (see
#: ``[tool.setuptools.package-data]``), so an installed spectragent has no
#: data at all. ``data_dir`` on the loader config only fixes one loader at
#: a time — a run needs the same benchmark named twice, once on the task
#: loader and once on the eval loader — so pointing the *root* somewhere
#: else has to be expressible in one place.
BENCHMARK_DATA_ENV: str = "SPECTRAGENT_BENCHMARK_DATA"

#: Wrapper keys recognised when a JSON data file holds records under a key.
_WRAPPER_KEYS = ("tasks", "data", "records", "items")

#: Raw-record keys accepted as a task's identity, in precedence order.
#: ``id`` covers upstream LexBench files, ``instance_id`` covers SWE-bench.
_TASK_ID_KEYS = ("task_id", "id", "instance_id")


class BenchmarkDataError(RuntimeError):
    """Raised when benchmark data is missing, malformed, or empty."""


def benchmark_data_root() -> Path:
    """The root every benchmark's data directory hangs off.

    ``$SPECTRAGENT_BENCHMARK_DATA`` wins over the in-package default so a
    single environment variable relocates *every* loader in a run. Read on
    each call rather than captured at import: a module-level snapshot would
    be global state, and tests that relocate the root would need a reload.
    """
    override = os.environ.get(BENCHMARK_DATA_ENV)
    if override:
        return Path(override).expanduser()
    return BENCHMARK_DATA_ROOT


def resolve_data_dir(env_type: str, registry_key: str, data_dir: str | None) -> Path:
    """Resolve a data directory: explicit path > env root > default root.

    The convention below the root is ``<env_type>/<registry_key>``, which
    is what makes the ``desktop/`` and ``mobile/`` trees addressable at
    all — they are unreachable if the environment segment is fixed.

    ``~`` is expanded on both escape hatches; a config file that cannot
    say ``~/data`` forces absolute paths that differ per machine.
    """
    if data_dir is not None:
        return Path(data_dir).expanduser()
    return benchmark_data_root() / env_type / registry_key


def load_split_map(root: Path) -> dict[str, str]:
    """Read ``data_info.json`` and return its ``split`` mapping."""
    info_path = root / "data_info.json"
    if not info_path.exists():
        raise BenchmarkDataError(
            f"No data_info.json at {info_path}. A benchmark data directory "
            f"must declare its splits before any subset can be loaded."
        )
    with open(info_path) as f:
        info = json.load(f)
    split = info.get("split")
    if not isinstance(split, dict) or not split:
        raise BenchmarkDataError(
            f"{info_path} has no usable 'split' mapping "
            f"(top-level keys: {sorted(info)})."
        )
    return split


def resolve_split_file(root: Path, subset: str) -> Path:
    """Resolve the data file backing ``subset`` within ``root``."""
    split_map = load_split_map(root)
    filename = split_map.get(subset)
    if not filename:
        raise BenchmarkDataError(
            f"Subset {subset!r} is not in the split map of {root} "
            f"(available: {', '.join(sorted(split_map))})."
        )
    return root / filename


def parse_file(path: Path) -> list[dict]:
    """Parse a JSONL or JSON benchmark data file into raw dicts.

    Returns a non-empty list of dicts — callers apply their own schema
    validation. Anything that would yield zero records raises instead.
    """
    if not path.exists():
        raise BenchmarkDataError(f"Data file not found: {path}")

    items: list[dict] = []
    if path.suffix == ".jsonl":
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    else:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            for key in _WRAPPER_KEYS:
                value = data.get(key)
                if isinstance(value, list):
                    items.extend(value)
                    break
            else:
                raise BenchmarkDataError(
                    f"{path} is a JSON object with no record list under any of "
                    f"{list(_WRAPPER_KEYS)} (top-level keys: {sorted(data)})."
                )
        else:
            raise BenchmarkDataError(
                f"{path} holds {type(data).__name__}, expected a list of records "
                f"or an object wrapping one."
            )

    if not items:
        raise BenchmarkDataError(f"Parsed zero records from {path}.")
    return items


def resolve_task_id(raw: dict[str, Any]) -> str:
    """Extract a task identity from a raw record.

    Accepts the normalized ``task_id`` plus the two upstream spellings
    that ship in real data (``id``, ``instance_id``), and coerces to
    ``str`` because upstream files key on integers.
    """
    for key in _TASK_ID_KEYS:
        value = raw.get(key)
        if value is not None and value != "":
            return str(value)
    raise BenchmarkDataError(
        f"Record has no task identity under any of {list(_TASK_ID_KEYS)} "
        f"(keys present: {sorted(raw)})."
    )
