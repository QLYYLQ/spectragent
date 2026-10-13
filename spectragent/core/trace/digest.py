"""A directory tree's digest: the one definition every comparison of two trees uses (arch §4.16).

Two kinds of reader compare trees. The flywheel metrics' export determinism
asks whether two exports of the same runs are the same bytes
(``spectragent/flywheel/metrics.py``); tests ask whether a reader left the
run it read untouched (offline replay, the training export, ``RunTrace`` as
an ``ArchiveReader``). Both call :func:`tree_digest`, so "the same tree"
means one thing everywhere.

Imports only the standard library: the trace layer's leaf, safe for any
reader to import — the browser package and the core archive tests included,
which must not import ``spectragent.flywheel``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TreeDigest:
    """A tree's digest and how many files it covers (:func:`tree_digest`)."""

    digest: str
    files: int


def tree_digest(root: Path, *, metadata: bool = False) -> TreeDigest:
    """sha256 over every file under *root*, sorted by relative POSIX path.

    Each regular file contributes ``<relative path>\\0<sha256 of its bytes>\\n``:
    the digest changes when a file is added, removed, renamed or rewritten
    with other bytes.

    With *metadata*, every entry under *root* contributes — directories and
    symbolic links too, read with ``lstat`` and never followed — and with it
    its mode, size and modification time, so the digest also changes when
    something was touched, re-permissioned or rewritten with the same bytes:
    "nothing under *root* changed at all", what a test proving that a reader
    never writes needs. ``files`` still counts the regular files.
    """
    digest = hashlib.sha256()
    files = 0
    if metadata:
        for path in sorted(root.rglob("*")):
            stat = path.lstat()
            rel = path.relative_to(root).as_posix()
            regular = path.is_file() and not path.is_symlink()
            content = hashlib.sha256(path.read_bytes()).hexdigest() if regular else "-"
            digest.update(
                f"{rel}\0{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}\0{content}\n".encode()
            )
            files += int(regular)
        return TreeDigest(digest=digest.hexdigest(), files=files)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
        files += 1
    return TreeDigest(digest=digest.hexdigest(), files=files)


__all__ = ["TreeDigest", "tree_digest"]
