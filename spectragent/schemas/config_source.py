"""The source file of a run's configuration, independent of its effective settings."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigSource(BaseModel):
    """A canonical source path and the version of its bytes that was consumed.

    The path groups every historical version of one source file. The digest
    distinguishes versions; neither field contains the YAML text or its secrets.
    Readers preserve the recorded path instead of resolving it against today's
    filesystem, where files or symlinks may have changed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="Canonical absolute path of the source YAML file.")
    sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="SHA256 of the source YAML bytes consumed."
    )

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        forms = (PurePosixPath(value), PureWindowsPath(value))
        if not any(path.is_absolute() and ".." not in path.parts for path in forms):
            raise ValueError("The configuration source path must be absolute and normalized")
        return value

    @classmethod
    def from_path(cls, path: Path, *, data: bytes | None = None) -> ConfigSource:
        """Capture source identity using the exact bytes the caller parsed.

        Supplying ``data`` avoids rereading a file that may change between
        parsing and recording. When omitted, read the canonical path once.
        """
        canonical = path.expanduser().resolve()
        consumed = canonical.read_bytes() if data is None else data
        return cls(path=str(canonical), sha256=hashlib.sha256(consumed).hexdigest())
