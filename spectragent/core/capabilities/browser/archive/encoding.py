"""Byte encodings the browser archivers share: JSON and JSON Lines, plain or gzipped.

Deterministic on purpose: keys sorted, compact separators, no timestamp in a
gzip header. A content-addressed file (``ArchiveFile.content_addressed``) is
deduplicated by its bytes, so the same evidence must always encode to the
same bytes.
"""

import gzip
import json
from collections.abc import Mapping, Sequence
from typing import Any


def canonical_json(value: Any) -> bytes:
    """*value* as compact JSON with sorted keys (UTF-8)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def jsonl(lines: Sequence[Mapping[str, Any]]) -> bytes:
    """One canonical JSON object per line, each line ``\\n``-terminated."""
    return b"".join(canonical_json(line) + b"\n" for line in lines)


def gzip_json(value: Any) -> bytes:
    """Canonical JSON, gzipped with ``mtime=0`` so equal values give equal bytes."""
    return gzip.compress(canonical_json(value), mtime=0)


def gzip_jsonl(lines: Sequence[Mapping[str, Any]]) -> bytes:
    """:func:`jsonl`, gzipped with ``mtime=0``: a ``.jsonl.gz`` file ``archive_lines`` reads back."""
    return gzip.compress(jsonl(lines), mtime=0)
