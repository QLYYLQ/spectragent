"""Durable file primitives shared by the WAL and the evaluator.

Three operations, and the split between them is the point:

- :func:`write_durable` makes one file's *data* durable — write a temp file
  beside the target, ``fsync`` it, ``os.replace`` it into place — and stops
  there. It does not ``fsync`` the directory. It returns the directories
  whose entries changed, so a caller writing many files can ``fsync`` each
  directory **once** instead of once per file.
- :func:`fsync_dir` makes a directory's entries durable: the name an
  ``os.replace`` or ``mkdir`` put there. Atomic is not durable — a rename
  can sit in the page cache — so a commit point that skipped this would
  only be crash-consistent, not crash-durable.
- :func:`atomic_write` is the single-file composition of the two, for files
  that commit on their own (``manifest.json``, ``result.json``, ``evals/``).
  It is the one public atomic-write helper. (``config.json`` is not one of
  them: ``WALWriter.init_run`` writes it with :func:`write_durable`, creates
  the run's subdirectories, and ``fsync``-s the run directory and the parent
  of every directory created for it in one batch, before the first
  manifest.)

The per-file data ``fsync`` is never skipped. Renaming a file whose data
never reached the disk lets a power loss leave a committed name pointing at
an empty file, and a manifest listing it would be lying about what was
recorded. What batching saves is only the directory ``fsync``: one per
directory covers every entry written under it (``docs/architecture_design.md``
§5.2).

TODO(linux): what these ``fsync`` calls cost was measured on macOS APFS
only, where ``os.fsync`` does not flush the device cache; ext4 and xfs have
not been measured. ``SPECTRAGENT_WAL_COMMIT_BUDGET_S`` overrides the one
budget that depends on it, the 1000-file benchmark test's.

**Directory ``fsync`` errors are not swallowed.** The only tolerated
failures are the ones that mean "this platform or filesystem cannot
``fsync`` a directory": Windows, which cannot open one, and ``EINVAL`` /
``ENOTSUP`` / ``EOPNOTSUPP`` from the ``fsync`` call itself. Everything else
— ``EIO``, ``ENOSPC``, and ``os.open`` failing with ``EMFILE``, ``EACCES``
or ``ENOENT`` — propagates, exactly as a failed data ``fsync`` or
``os.replace`` always has. A caller must distinguish failure before rename
from failure after it: the latter may already have installed the new file.
The WAL reconciles its manifest before it performs another write; recording
errors remain separate from execution outcomes.

**Permissions are a plain write's.** A new file gets ``0o666`` masked by
the process umask, and replacing a file keeps that file's permission bits
— what ``open(path, "w")`` does. The temp file is created with
``os.open(..., 0o666)`` rather than ``tempfile.mkstemp``, whose ``0o600``
is right for scratch data and wrong for a file about to be renamed into
place: every trace file and every verdict used to come out owner-only, so a
results directory shared with a teammate, a dashboard or a CI uploader
running as another user could not be read, and re-scoring reset a mode an
operator had set on purpose. Whether a trace is private is the umask's (or
the directory's) decision, as for any other file the process writes.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
from pathlib import Path

#: ``fsync`` on a directory descriptor failing with one of these means the
#: filesystem does not support directory ``fsync`` at all — not that this
#: one failed. ``ENOTSUP`` and ``EOPNOTSUPP`` are the same number on Linux
#: and different numbers on macOS, hence both.
_DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})

#: How a temp file is opened: created, never reused (``O_EXCL`` also refuses
#: a symlink planted at the name), not inherited by child processes.
_TEMP_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOINHERIT", 0)
    | getattr(os, "O_BINARY", 0)
)

#: Name attempts before giving up — 64 random bits each, so in practice one.
_TEMP_ATTEMPTS = 100


def fsync_dir(path: Path) -> None:
    """``fsync`` directory *path* so the entries written in it survive power loss.

    Skipped only on Windows, which cannot open a directory. Opened with
    ``O_DIRECTORY`` where the platform has it, so handing this a file
    fails with ``NotADirectoryError`` instead of quietly syncing the file —
    the directory entry that needed it would stay in the page cache.

    Raises:
        OSError: Any failure to open *path*, and any ``fsync`` failure other
            than ``EINVAL`` / ``ENOTSUP`` / ``EOPNOTSUPP`` (the filesystem
            not supporting directory ``fsync``).
    """
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def write_durable(path: Path, data: bytes) -> set[Path]:
    """Put *data* at *path* durably, except for the directory entries.

    Same-directory temp file → write and flush → ``fsync(file)`` →
    ``os.replace``. Missing parent directories are created first. On any
    failure the temp file is removed and *path* is untouched — a previous
    version of it stays complete. The file ends up with the permissions a
    plain write would give it (module docstring).

    The flush is what the ``fsync`` covers. ``os.fdopen`` buffers up to
    ``st_blksize`` (4 KiB on APFS and ext4), so without it — or with the
    ``fsync`` ahead of the write — every smaller file is ``fsync``-ed while
    still empty and its bytes reach the kernel only at ``close()``. That is
    most of a trace: step records, the early manifests, call records,
    verdicts. The number of ``fsync`` calls does not change, so only the
    file's size at the moment of the ``fsync`` can tell (``test_fsio.py``).

    ``os.fdopen`` owns the descriptor, so it is closed exactly once on every
    path. Closing it by hand and then probing it in the error handler once
    replaced the real failure (a full disk during the rename) with a bogus
    ``EBADF`` and left the temp file behind.

    Returns:
        The directories that must be :func:`fsync_dir`'d before a commit
        point may claim this file: ``path.parent``, plus the parent of every
        directory created for it — a new directory's own name is an entry
        in *its* parent.
    """
    parent = path.parent
    created = _make_dirs(parent)
    fd, tmp_path = _create_temp(parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            mode = _replaced_mode(path)
            if mode is not None and hasattr(os, "fchmod"):
                # Before a byte is written: the temp is never readable more
                # widely than the file it replaces.
                os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return {parent, *(directory.parent for directory in created)}


def atomic_write(path: Path, data: bytes) -> None:
    """Write one file atomically and durably: :func:`write_durable`, then each directory.

    After this returns, *path* holds *data* and its name survives power
    loss. A reader sees either the previous file or the new one, never a
    partial write. An exception from the directory fsync occurs after the
    replacement: *path* may already hold *data*. Callers maintaining an
    in-memory index must read back the commit point before writing again.
    """
    # Deepest first, so a directory's own entries are durable before the
    # entry naming it in its parent is.
    for directory in sorted(write_durable(path, data), key=lambda d: len(d.parts), reverse=True):
        fsync_dir(directory)


def _create_temp(directory: Path) -> tuple[int, Path]:
    """A new, empty temp file in *directory*, opened for writing.

    Created with mode ``0o666``, which the kernel masks with the umask — the
    mode a plain ``open()`` gives a new file — where ``tempfile.mkstemp``
    would force ``0o600``. Reading the umask instead (``os.umask`` sets it
    to read it) would race every other thread creating a file.
    """
    for _ in range(_TEMP_ATTEMPTS):
        candidate = directory / f"tmp{secrets.token_hex(8)}.tmp"
        try:
            return os.open(candidate, _TEMP_FLAGS, 0o666), candidate
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no unused temporary file name", str(directory))


def _replaced_mode(path: Path) -> int | None:
    """The permission bits of the file *path* already holds, if any."""
    try:
        return os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        return None


def _make_dirs(directory: Path) -> list[Path]:
    """Create *directory* and any missing ancestors; return the ones created, top-down.

    ``mkdir(parents=True)`` does not say which levels it created, and those
    are exactly the ones whose names still need a directory ``fsync`` in
    their parent. A level another writer creates concurrently is not
    reported — it is that writer's entry to make durable.
    """
    missing: list[Path] = []
    probe = directory
    while not probe.is_dir():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    created: list[Path] = []
    for level in reversed(missing):
        try:
            level.mkdir()
        except FileExistsError:
            continue
        created.append(level)
    return created
