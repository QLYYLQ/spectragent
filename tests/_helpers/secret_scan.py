"""Read every byte of a trace directory as text — what a secret guard scans.

Shared by the framework's secret guard (``tests/test_core/test_trace/test_secret_guard.py``)
and the browser archive's privacy guard (``test_browser_archive_live.py``): both
must search the same bytes the same way, gzip included (the network archive's
WARC is gzip members; raw DOM captures are gzip JSON).
"""

from __future__ import annotations

import gzip
import zlib
from pathlib import Path

#: The two bytes every gzip member starts with.
GZIP_MAGIC = b"\x1f\x8b"
#: Layers of gzip peeled off one file at most — a ``.gz`` inside a ``.gz``.
_MAX_GZIP_LAYERS = 4


def readable(path: Path) -> str:
    """A file's bytes as text — gunzipped first when it is gzip.

    Gzip by its magic bytes (``1f 8b``) or by a ``.gz`` name, peeled
    repeatedly for a gzip inside a gzip; a multi-member gzip (a WARC) is read
    whole. A file that only looks like gzip and does not decompress is
    scanned as it is: the guard never skips bytes because they would not
    decode.
    """
    data = path.read_bytes()
    named_gzip = path.name.endswith(".gz")
    for _ in range(_MAX_GZIP_LAYERS):
        if not (named_gzip or data[:2] == GZIP_MAGIC):
            break
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error):  # not gzip after all
            break
        named_gzip = False
    return data.decode("utf-8", errors="replace")


def all_text(run_dir: Path) -> list[tuple[Path, str]]:
    """Every readable byte under the trace dir, as text.

    Walks the directory rather than the manifest: a leak in a file nothing
    indexes is still a leak, and the manifest is exactly what a careless
    writer would forget to list it in. ``env/`` is walked like the rest,
    gzip decoded (:func:`readable`).
    """
    out: list[tuple[Path, str]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        out.append((path, readable(path)))
    return out
