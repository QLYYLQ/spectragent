"""Shared parsing logic for browser benchmark data loaders.

Both Task and Eval loaders use these utilities to resolve data directories,
read split maps from ``data_info.json``, and parse JSONL/JSON data files.
"""

import json
import logging
from pathlib import Path

from spectragent import BENCHMARK_DATA_ROOT

logger = logging.getLogger(__name__)

_BENCHMARK_DATA_ROOT = BENCHMARK_DATA_ROOT / "browser"


def resolve_data_dir(registry_key: str, data_dir: str | None) -> Path:
    """Resolve data directory: explicit path > default convention."""
    if data_dir is not None:
        return Path(data_dir)
    return _BENCHMARK_DATA_ROOT / registry_key


def load_split_map(root: Path) -> dict[str, str]:
    """Read ``data_info.json`` and return the ``split`` mapping."""
    info_path = root / "data_info.json"
    if not info_path.exists():
        logger.warning("data_info.json not found: %s", info_path)
        return {}
    with open(info_path) as f:
        info = json.load(f)
    return info.get("split", {})


def parse_file(path: Path) -> list[dict]:
    """Parse a JSONL or JSON benchmark data file into raw dicts.

    Returns a list of dicts — callers apply their own schema validation.
    """
    if not path.exists():
        logger.warning("Data file not found: %s", path)
        return []

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
        raw = data.get("tasks", data) if isinstance(data, dict) else data
        if isinstance(raw, list):
            items.extend(raw)
    return items
