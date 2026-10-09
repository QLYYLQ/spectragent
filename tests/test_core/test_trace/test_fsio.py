"""The durable-write primitives every trace file goes through.

``atomic_write`` used to live privately inside the WAL, with a directory
``fsync`` that swallowed every ``OSError`` — so an ``EIO`` on the
directory left a commit point that said "on disk" about a name that was
not. These tests pin the errno policy that replaced it, the
data-vs-directory split that lets a commit ``fsync`` each directory once,
and that each data ``fsync`` covers the file's bytes rather than an empty
file whose data is still in Python's buffer.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from pathlib import Path

import pytest

from spectragent.core.trace import fsio
from spectragent.core.trace.fsio import atomic_write, fsync_dir, write_durable


def _is_dir_fd(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


class _FsyncLog:
    """Wraps ``os.fsync``: records which kind of descriptor each call was for.

    For a file it also records ``st_size`` at that moment — what the kernel
    holds, which is all the ``fsync`` can make durable. *dir_error* (an
    errno) makes every *directory* ``fsync`` fail with it, so a test can
    break the directory half of a write while the data half keeps working.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, dir_error: int | None = None):
        self.calls: list[str] = []
        self.file_sizes: list[int] = []
        self._real = os.fsync
        self._dir_error = dir_error
        monkeypatch.setattr(os, "fsync", self)

    def __call__(self, fd: int) -> None:
        kind = "dir" if _is_dir_fd(fd) else "file"
        self.calls.append(kind)
        if kind == "file":
            self.file_sizes.append(os.fstat(fd).st_size)
        if kind == "dir" and self._dir_error is not None:
            raise OSError(self._dir_error, os.strerror(self._dir_error))
        self._real(fd)


# ── atomic_write ─────────────────────────────────────────────


class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "test.json"
        atomic_write(target, b'{"ok": true}')
        assert target.read_bytes() == b'{"ok": true}'

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "test.json"
        target.write_bytes(b"old")
        atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_no_leftover_tmp_on_success(self, tmp_path):
        atomic_write(tmp_path / "test.json", b"data")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_rename_failure_surfaces_the_real_error(self, tmp_path, monkeypatch):
        """The caller must see the disk error, not EBADF from the cleanup.

        Closing the fd manually and then probing it with
        ``os.get_inheritable`` in the error handler raised
        ``OSError(EBADF)`` first, masking the actual failure.
        """

        def boom(*a, **kw):
            raise OSError("DISK FULL")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="DISK FULL"):
            atomic_write(tmp_path / "test.json", b"data")

    def test_no_leftover_tmp_on_failure(self, tmp_path, monkeypatch):
        target = tmp_path / "test.json"

        def boom(*a, **kw):
            raise OSError("DISK FULL")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write(target, b"data")
        assert list(tmp_path.glob("*.tmp")) == []
        assert not target.exists()

    def test_a_failed_rewrite_leaves_the_previous_file_whole(self, tmp_path, monkeypatch):
        """Atomic means a reader sees the old file or the new one — never half."""
        target = tmp_path / "test.json"
        atomic_write(target, b'{"version": 1}')

        def boom(*a, **kw):
            raise OSError("DISK FULL")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write(target, b'{"version": 2, "much": "longer"}')
        assert target.read_bytes() == b'{"version": 1}'

    def test_data_and_directory_are_both_fsynced(self, tmp_path, monkeypatch):
        log = _FsyncLog(monkeypatch)
        atomic_write(tmp_path / "test.json", b"data")
        assert log.calls == ["file", "dir"]

    def test_the_data_fsync_covers_the_bytes(self, tmp_path, monkeypatch):
        """A manifest-sized rewrite: the commit point must not be fsynced empty.

        The temp file is new and empty even though *path* exists, so a
        replace is no safer than a first write (``TestWriteDurable``).
        """
        log = _FsyncLog(monkeypatch)
        target = tmp_path / "manifest.json"
        target.write_bytes(b'{"steps": []}')

        atomic_write(target, b'{"steps": [0]}')

        assert log.file_sizes == [len(b'{"steps": [0]}')]

    def test_an_eio_on_the_directory_fsync_is_not_swallowed(self, tmp_path, monkeypatch):
        """The defect this module exists for.

        The old helper passed on *any* ``OSError`` from the directory
        ``fsync`` and returned normally, so the WAL committed a manifest
        whose rename was never confirmed durable.
        """
        _FsyncLog(monkeypatch, dir_error=errno.EIO)
        with pytest.raises(OSError) as caught:
            atomic_write(tmp_path / "test.json", b"data")
        assert caught.value.errno == errno.EIO

    def test_missing_parents_are_created_and_made_durable(self, tmp_path, monkeypatch):
        """A new directory's name lives in its parent, which needs an fsync too."""
        synced: list[Path] = []
        real = fsio.fsync_dir
        monkeypatch.setattr(fsio, "fsync_dir", lambda d: (synced.append(d), real(d)))

        target = tmp_path / "evals" / "nested" / "verdict.json"
        atomic_write(target, b"{}")

        assert target.read_bytes() == b"{}"
        assert sorted(synced) == sorted(
            [tmp_path / "evals" / "nested", tmp_path / "evals", tmp_path]
        )
        # Deepest first: a directory's entries before the entry naming it.
        assert synced[0] == tmp_path / "evals" / "nested"


# ── write_durable ────────────────────────────────────────────


class TestWriteDurable:
    def test_writes_the_data(self, tmp_path):
        target = tmp_path / "a.bin"
        write_durable(target, b"\x00\x01")
        assert target.read_bytes() == b"\x00\x01"

    def test_fsyncs_the_data_but_no_directory(self, tmp_path, monkeypatch):
        """The directory half is the caller's, so a batch can do it once per directory."""
        log = _FsyncLog(monkeypatch)
        write_durable(tmp_path / "a.bin", b"data")
        assert log.calls == ["file"]

    def test_the_data_is_durable_before_the_name_is_published(self, tmp_path, monkeypatch):
        """fsync then rename — the reverse lets a crash publish an empty file.

        Order alone is not enough: an ``fsync`` that runs while the bytes are
        still in Python's buffer is in the right place and covers nothing.
        ``test_the_bytes_are_in_the_file_when_it_is_fsynced`` pins that half.
        """
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
        monkeypatch.setattr(
            os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
        )

        write_durable(tmp_path / "a.bin", b"data")

        assert order == ["fsync", "replace"]

    @pytest.mark.parametrize(
        "size",
        [1, 110, 4096, 64 * 1024 + 1],
        ids=["1B", "a-step-record", "one-buffer", "past-the-buffer"],
    )
    def test_the_bytes_are_in_the_file_when_it_is_fsynced(self, tmp_path, monkeypatch, size):
        """The ``fsync`` covers what the kernel holds, not what Python still buffers.

        ``os.fdopen(fd, "wb")`` holds up to ``st_blksize`` (4 KiB on APFS and
        ext4) and writes anything larger straight through. Drop the flush, or
        move the ``fsync`` ahead of the write, and every file up to that size
        is fsynced empty — the bytes reach the kernel at ``close()``, after
        the ``fsync``, and a power loss can leave a committed name on an
        empty file. The count of ``fsync`` calls and their order relative to
        the rename are unchanged, so the size at the moment of the ``fsync``
        is the only thing that tells.
        """
        log = _FsyncLog(monkeypatch)
        data = os.urandom(size)

        write_durable(tmp_path / "a.bin", data)

        assert log.file_sizes == [size]
        assert (tmp_path / "a.bin").read_bytes() == data

    def test_reports_the_directory_it_wrote_into(self, tmp_path):
        assert write_durable(tmp_path / "a.bin", b"x") == {tmp_path}

    def test_reports_the_parent_of_every_directory_it_created(self, tmp_path):
        (tmp_path / "env").mkdir()
        target = tmp_path / "env" / "steps" / "000" / "x.y" / "name"

        touched = write_durable(target, b"x")

        assert touched == {
            tmp_path / "env" / "steps" / "000" / "x.y",  # holds `name`
            tmp_path / "env" / "steps" / "000",  # holds `x.y`
            tmp_path / "env" / "steps",  # holds `000`
            tmp_path / "env",  # holds `steps`
        }

    def test_an_existing_directory_is_not_reported_as_created(self, tmp_path):
        (tmp_path / "blobs" / "ab").mkdir(parents=True)
        assert write_durable(tmp_path / "blobs" / "ab" / "cdef", b"x") == {
            tmp_path / "blobs" / "ab"
        }

    def test_a_failed_data_fsync_leaves_nothing_behind(self, tmp_path, monkeypatch):
        target = tmp_path / "a.bin"

        def eio(fd: int) -> None:
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(os, "fsync", eio)
        with pytest.raises(OSError) as caught:
            write_durable(target, b"data")
        assert caught.value.errno == errno.EIO
        assert list(tmp_path.iterdir()) == []


# ── fsync_dir: the errno policy ──────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is skipped on Windows by design")
class TestFsyncDirErrnoPolicy:
    @pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EBADF])
    def test_a_real_io_error_propagates(self, tmp_path, monkeypatch, code):
        _FsyncLog(monkeypatch, dir_error=code)
        with pytest.raises(OSError) as caught:
            fsync_dir(tmp_path)
        assert caught.value.errno == code

    @pytest.mark.parametrize(
        "code",
        [errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP],
        ids=["EINVAL", "ENOTSUP", "EOPNOTSUPP"],
    )
    def test_a_filesystem_without_directory_fsync_is_tolerated(self, tmp_path, monkeypatch, code):
        _FsyncLog(monkeypatch, dir_error=code)
        fsync_dir(tmp_path)  # does not raise

    def test_a_failing_open_propagates(self, tmp_path):
        """The old helper returned silently here — on POSIX that hid EMFILE/EACCES/ENOENT."""
        with pytest.raises(FileNotFoundError):
            fsync_dir(tmp_path / "never-created")

    def test_running_out_of_descriptors_propagates(self, tmp_path, monkeypatch):
        def emfile(*a, **kw):
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(os, "open", emfile)
        with pytest.raises(OSError) as caught:
            fsync_dir(tmp_path)
        assert caught.value.errno == errno.EMFILE

    def test_a_file_is_refused_rather_than_synced_in_its_place(self, tmp_path):
        """Syncing the file would leave the directory entry that needed it in the cache."""
        not_a_dir = tmp_path / "file.json"
        not_a_dir.write_bytes(b"{}")
        with pytest.raises(NotADirectoryError):
            fsync_dir(not_a_dir)

    @pytest.mark.parametrize("code", [errno.EIO, errno.EINVAL])
    def test_the_descriptor_is_closed_on_every_path(self, tmp_path, monkeypatch, code):
        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def tracking_open(*a, **kw):
            fd = real_open(*a, **kw)
            opened.append(fd)
            return fd

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd)))
        _FsyncLog(monkeypatch, dir_error=code)

        expectation = pytest.raises(OSError) if code == errno.EIO else contextlib.nullcontext()
        with expectation:
            fsync_dir(tmp_path)

        assert opened and opened == closed


def test_windows_skips_directory_fsync_without_opening_anything(tmp_path, monkeypatch):
    """Windows cannot open a directory — the one platform-level skip."""

    def must_not_open(*a, **kw):
        raise AssertionError("fsync_dir opened a directory on Windows")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "open", must_not_open)
    fsync_dir(tmp_path / "anything")


# ── permissions: a plain write's ─────────────────────────────


@pytest.fixture()
def umask():
    """``os.umask`` for one test; the previous mask is put back after it."""
    previous = os.umask(0o022)  # reading the umask means setting it
    yield os.umask
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
class TestPermissionsAreAPlainWrites:
    """``mkstemp`` creates ``0o600`` whatever the umask says.

    Every trace file and every verdict came out owner-only: a results
    directory a teammate, a dashboard or a CI uploader reads as another
    user returned ``PermissionError``, and a re-score reset a mode an
    operator had set on purpose. Whether a file is private is the umask's
    decision, as for a plain ``write_bytes``.
    """

    @pytest.mark.parametrize("mask", [0o022, 0o027, 0o077])
    def test_a_new_file_gets_what_a_plain_write_would(self, tmp_path, umask, mask):
        umask(mask)
        atomic_write(tmp_path / "durable.json", b"{}")
        (tmp_path / "plain.json").write_bytes(b"{}")

        assert _mode(tmp_path / "durable.json") == _mode(tmp_path / "plain.json") == 0o666 & ~mask

    def test_write_durable_too(self, tmp_path, umask):
        umask(0o022)
        write_durable(tmp_path / "a.bin", b"x")
        assert _mode(tmp_path / "a.bin") == 0o644

    @pytest.mark.parametrize("mode", [0o640, 0o600, 0o664])
    def test_a_replaced_file_keeps_its_mode(self, tmp_path, umask, mode):
        """An operator's ``chmod`` survives a re-score, as it survives ``open(path, "w")``."""
        umask(0o022)
        target = tmp_path / "verdict.json"
        atomic_write(target, b"v1")
        target.chmod(mode)

        atomic_write(target, b"v2")

        assert target.read_bytes() == b"v2"
        assert _mode(target) == mode

    def test_the_temp_file_is_never_wider_than_the_file_it_replaces(
        self, tmp_path, umask, monkeypatch
    ):
        """The mode is set before a byte is written, not after."""
        umask(0o022)
        target = tmp_path / "private.json"
        atomic_write(target, b"v1")
        target.chmod(0o600)
        seen: list[int] = []
        real_fsync = os.fsync

        def checking(fd: int) -> None:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", checking)
        atomic_write(target, b"secret v2")

        assert seen == [0o600]
