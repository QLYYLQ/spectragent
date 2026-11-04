"""What a benchmark declares about its data and installation, and the downloads its fetch uses (``docs/architecture_design.md`` §4.10.1 "数据").

A benchmark's class declares both, each for one validated benchmark block:

- :meth:`BenchmarkBase.pinned_data <spectragent.benchmarks._registrar.BenchmarkBase.pinned_data>`:
  where its task loader's data comes from, pinned (a Hugging Face dataset at
  a commit, a git repository at a commit, a file at a URL with its SHA-256),
  and how to fetch it (:class:`PinnedData`); ``None`` when it pins none and
  its data is put in place by hand.
- :meth:`BenchmarkBase.installation <spectragent.benchmarks._registrar.BenchmarkBase.installation>`:
  what it installs beside the data, too heavy to install per run
  (AndroidWorld's checkout and venv, :class:`BenchmarkInstallation`); ``None``
  when it installs nothing.

A fetch downloads with :func:`download_huggingface_file` and
:func:`download_file`, which check what they downloaded against the pin. What
the commands do with these declarations is in
:mod:`spectragent.benchmarks.operations`. Adding a benchmark never edits either
module: its class declares its data and its installation.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import httpx

from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.refusal import Refusal
from spectragent.schemas.data_version import DataVersion

#: Where a fetch or an installation says what it is doing, one line at a time.
Say = Callable[[str], None]


def to_stderr(line: str) -> None:
    """Say *line* on stderr: a command's stdout holds its one result (``--json``)."""
    print(line, file=sys.stderr, flush=True)


#: The variable holding a Hugging Face token, for a dataset gated behind accepting its terms.
HF_TOKEN_ENV = "HF_TOKEN"
_HUGGING_FACE = "https://huggingface.co"
#: How long one download may take to connect and between reads.
_DOWNLOAD_TIMEOUT_S = 60.0
#: How long a program a fetch or an installation runs may take: installing
#: AndroidWorld's packages without a warm cache is the longest, minutes.
PROGRAM_TIMEOUT_S = 1800.0


class PinnedData(Protocol):
    """A benchmark's data at its pinned version, and how to fetch it."""

    @property
    def version(self) -> DataVersion:
        """The pinned source and version."""
        ...

    def fetch(self, into: Path, say: Say) -> None:
        """Write the pinned version's data into *into*, an empty directory, as its task loader reads it.

        Only files, at the top of *into*: they replace the files of the same
        name in the data directory. Checks what it downloaded against the pin.

        Raises:
            Refusal: It cannot (a download failed or is not the pinned one,
                something it needs is not installed); the message says why
                and what to do.
        """
        ...


class BenchmarkInstallation(Protocol):
    """What a benchmark installs beside its data, and how."""

    def problems(self) -> list[str]:
        """What is missing or wrong, one entry each, saying how to fix it; ``[]`` when installed."""
        ...

    def install(self, say: Say) -> None:
        """Install what it can, skipping what is already in place.

        What it cannot install itself (a system program, an emulator image)
        it leaves to :meth:`problems` to name.

        Raises:
            Refusal: A step failed; the message says which and why.
        """
        ...


class BenchmarkSetupError(Refusal, RuntimeError):
    """A benchmark's installation failed, or left something only the user can install; every problem named."""


class ProgramError(Refusal, RuntimeError):
    """A program a fetch or an installation runs could not start or failed; the message names it."""


def run_program(
    argv: Sequence[str | Path],
    purpose: str,
    *,
    cwd: Path | None = None,
    timeout_s: float = PROGRAM_TIMEOUT_S,
) -> None:
    """Run *argv* for *purpose* (``check out AndroidWorld``), its output on this process's stderr.

    Stdout is left to the command's one result (``--json``), so the program
    writes both of its streams to file descriptor 2, whatever ``sys.stderr``
    is. It gets *timeout_s* (killed after it), and git asks for no password:
    a fetch or an installation runs unattended.

    Raises:
        ProgramError: It could not start, ran out of time, or exited with a
            status other than 0.
    """
    command = [str(part) for part in argv]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(
            command, stdout=2, stderr=2, cwd=cwd, env=environment, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        raise ProgramError(
            f"cannot {purpose}: {shlex.join(command)} did not finish within {timeout_s:g} s and "
            f"was stopped"
        ) from None
    except OSError as exc:
        raise ProgramError(f"cannot {purpose}: {command[0]} does not start: {exc}") from exc
    if completed.returncode != 0:
        raise ProgramError(
            f"cannot {purpose}: {shlex.join(command)} exited with {completed.returncode} (its "
            f"output is above)"
        )


def download_huggingface_file(
    repository: str, revision: str, path: str, into: Path, say: Say
) -> Path:
    """Download *path* of the Hugging Face dataset *repository* at commit *revision* into *into*.

    The file is checked against the dataset's own record of it at that
    commit: an LFS file's SHA-256, any other file's git blob hash. A gated
    dataset needs ``$HF_TOKEN``, a token of an account that accepted its
    terms. Returns the downloaded file, named as in the dataset.

    Raises:
        BenchmarkDataError: The download failed, was refused, or is not the
            file the dataset holds at *revision*.
    """
    token = os.environ.get(HF_TOKEN_ENV)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    parent, _, name = path.rpartition("/")
    listing_url = f"{_HUGGING_FACE}/api/datasets/{repository}/tree/{revision}/{parent}".rstrip("/")
    file_url = f"{_HUGGING_FACE}/datasets/{repository}/resolve/{revision}/{path}"
    say(f"downloading {path} of the Hugging Face dataset {repository} at {revision}")
    with _client(headers) as client:
        listing = _get(client, listing_url, f"{repository} at {revision}")
        entries = [
            entry
            for entry in _json_list(listing, f"the file list of {repository} at {revision}")
            if isinstance(entry, dict) and entry.get("path") == path
        ]
        if not entries:
            raise BenchmarkDataError(f"{repository} at {revision} holds no file {path}")
        response = _send(client, file_url, f"{path} of {repository} at {revision}")
        if response.status_code in (401, 403):
            raise BenchmarkDataError(
                f"Hugging Face refused {path} of {repository} (HTTP {response.status_code}): the "
                f"dataset is gated; accept its terms at {_HUGGING_FACE}/datasets/{repository} "
                f"and set {HF_TOKEN_ENV} to a token of that account"
                + ("" if token else f" ({HF_TOKEN_ENV} is not set)")
            )
        _raise_for(response, f"{path} of {repository} at {revision}")
    data = response.content
    (entry,) = entries
    lfs = entry.get("lfs")
    if isinstance(lfs, dict) and lfs.get("oid"):
        found, expected, kind = hashlib.sha256(data).hexdigest(), str(lfs["oid"]), "SHA-256"
    else:
        found, expected, kind = git_blob_hash(data), str(entry.get("oid")), "git blob hash"
    if found != expected:
        raise BenchmarkDataError(
            f"{path} downloaded from {repository} has {kind} {found}, and the dataset records "
            f"{expected} for it at {revision}"
        )
    target = into / name
    target.write_bytes(data)
    return target


def download_file(url: str, sha256: str, target: Path, say: Say) -> Path:
    """Download *url* to *target*, checked against its pinned *sha256*.

    Raises:
        BenchmarkDataError: The download failed or its SHA-256 is not *sha256*.
    """
    say(f"downloading {url}")
    with _client({}) as client:
        response = _get(client, url, url)
    data = response.content
    found = hashlib.sha256(data).hexdigest()
    if found != sha256:
        raise BenchmarkDataError(
            f"{url} downloaded with SHA-256 {found}, and the benchmark pins {sha256}: the file "
            f"there changed since it was pinned"
        )
    target.write_bytes(data)
    return target


def file_sha256(path: Path) -> str:
    """The SHA-256 of the file at *path*, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_hash(data: bytes) -> str:
    """The hash git gives *data* as a file: SHA-1 over ``blob <size>\\0`` and the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _client(headers: Mapping[str, str]) -> httpx.Client:
    # The environment's proxy settings apply (``trust_env``): these are remote hosts.
    return httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_S, headers=dict(headers))


def _get(client: httpx.Client, url: str, what: str) -> httpx.Response:
    """*url*'s answer, refused unless it is 200."""
    response = _send(client, url, what)
    _raise_for(response, what)
    return response


def _send(client: httpx.Client, url: str, what: str) -> httpx.Response:
    """*url*'s answer, whatever its status; a request that gets none is refused."""
    try:
        return client.get(url)
    except httpx.HTTPError as exc:
        raise BenchmarkDataError(f"cannot download {what}: {type(exc).__name__}: {exc}") from exc


def _json_list(response: httpx.Response, what: str) -> list[object]:
    """*response*'s body, a JSON list."""
    try:
        body: object = response.json()
    except ValueError:
        body = None
    if not isinstance(body, list):
        raise BenchmarkDataError(f"{what} is not a JSON list")
    return body


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.status_code != 200:
        raise BenchmarkDataError(f"cannot download {what}: HTTP {response.status_code}")


__all__ = [
    "HF_TOKEN_ENV",
    "PROGRAM_TIMEOUT_S",
    "BenchmarkInstallation",
    "BenchmarkSetupError",
    "PinnedData",
    "ProgramError",
    "Say",
    "download_file",
    "download_huggingface_file",
    "file_sha256",
    "git_blob_hash",
    "run_program",
    "to_stderr",
]
