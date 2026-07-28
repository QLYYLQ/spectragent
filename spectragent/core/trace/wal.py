"""WALWriter — Write-Ahead Log persistence engine for agent traces.

Pure I/O engine with no awareness of the Hook/AOP system.
TraceWriter hook (M11) calls this; WALWriter itself is independently testable.

Write protocol per step (must execute in order):
1. Write observation fragment files (observations/NNN_key.json|.png)
2. Write LLM message files (messages/NNN_input.json, NNN_output.json)
3. Write step record (steps/NNN.json)
4. Atomic manifest update (write-tmp -> fsync -> rename) — commit point

See docs/execution_dag.md M10 for design details.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from spectragent.core.trace.models import (
    RunStatus,
    StepManifestEntry,
    TraceConfig,
    TraceManifest,
)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename inside it survives power loss.

    ``os.rename`` is atomic, but atomic is not durable: the directory
    entry can still be sitting in the page cache. Without this the WAL's
    commit point is only crash-consistent, not crash-durable. Platforms
    that cannot open a directory (Windows) are skipped.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write data atomically via write-tmp -> fsync -> rename -> fsync dir.

    POSIX guarantees that rename() is atomic within a filesystem.

    ``os.fdopen`` takes ownership of the descriptor so it is closed
    exactly once, on every path. The previous version closed the fd
    manually and then tried to close it again in the error handler via
    ``os.get_inheritable(fd)`` — which raises ``OSError(EBADF)`` on an
    already-closed fd, replacing the *real* failure (say, a full disk
    during rename) with a bogus "Bad file descriptor" and leaving the
    temp file behind.
    """
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    _fsync_dir(parent)


def _default_json(obj: Any) -> Any:
    """JSON default handler for Pydantic models and other non-serializable types."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    return str(obj)


def _serialize_fragment(data: Any) -> bytes:
    """Serialize a fragment value to bytes.

    - Pydantic BaseModel -> .model_dump_json() -> UTF-8 bytes
    - bytes -> as-is (screenshots, binary data)
    - dict/list -> json.dumps -> UTF-8 bytes (with Pydantic model support)
    - str -> UTF-8 encode
    """
    if isinstance(data, BaseModel):
        return data.model_dump_json(indent=2).encode("utf-8")
    if isinstance(data, bytes):
        return data
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2, default=_default_json).encode("utf-8")
    if isinstance(data, str):
        return data.encode("utf-8")
    # Fallback
    return json.dumps(data, ensure_ascii=False, default=_default_json, indent=2).encode("utf-8")


class WALWriter:
    """Stateful WAL writer for a single agent run.

    Creates and manages the trace directory structure:
        traces/{run_id}/
            manifest.json
            config.json
            result.json
            steps/
            observations/
            messages/
            evals/
    """

    def __init__(self, base_dir: Path, run_id: str) -> None:
        self._run_dir = base_dir / run_id
        self._run_id = run_id
        self._manifest: TraceManifest | None = None

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def manifest(self) -> TraceManifest | None:
        return self._manifest

    def init_run(self, config: TraceConfig) -> None:
        """Create directory structure, write config snapshot and initial manifest."""
        for subdir in ("steps", "observations", "messages", "evals"):
            (self._run_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Write config snapshot
        config_bytes = config.model_dump_json(indent=2).encode("utf-8")
        config_hash = hashlib.sha256(config_bytes).hexdigest()[:16]
        _atomic_write(self._run_dir / "config.json", config_bytes)

        # Write initial manifest
        now = datetime.now(UTC)
        self._manifest = TraceManifest(
            run_id=self._run_id,
            last_step_completed=-1,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
            config_hash=config_hash,
        )
        self._write_manifest()

    def write_observation(
        self,
        step: int,
        fragment_key: str,
        data: Any,
        *,
        suffix: str | None = None,
    ) -> str:
        """Serialize a fragment to the observations directory.

        Args:
            step: Step index.
            fragment_key: Fragment field name (e.g. ``"screenshot"``).
            data: Payload. ``bytes`` is written verbatim.
            suffix: File extension including the dot. Defaults to
                ``".png"`` for bytes (back-compatible) and ``".json"``
                otherwise. Fragments carrying non-PNG binaries pass their
                own via :meth:`FragmentBase.trace_binary`.

        Returns the relative path within the trace directory.
        """
        if suffix is None:
            suffix = ".png" if isinstance(data, bytes) else ".json"
        filename = f"{step:03d}_{fragment_key}{suffix}"
        rel_path = f"observations/{filename}"
        abs_path = self._run_dir / rel_path

        serialized = _serialize_fragment(data)
        _atomic_write(abs_path, serialized)
        return rel_path

    def write_messages(
        self, step: int, input_msgs: Any, output: Any, *, call: int = 0
    ) -> tuple[str, str]:
        """Write LLM input and output messages for one call of a step.

        The first call of a step (``call=0``) keeps the canonical
        ``NNN_input.json`` / ``NNN_output.json`` names. Subsequent calls
        are suffixed so a loop that invokes the LLM more than once per
        step does not overwrite its own record.

        Returns (input_rel_path, output_rel_path).
        """
        tag = "" if call == 0 else f"__{call}"
        input_rel = f"messages/{step:03d}_input{tag}.json"
        output_rel = f"messages/{step:03d}_output{tag}.json"

        input_bytes = _serialize_fragment(input_msgs)
        output_bytes = _serialize_fragment(output)

        _atomic_write(self._run_dir / input_rel, input_bytes)
        _atomic_write(self._run_dir / output_rel, output_bytes)
        return input_rel, output_rel

    def write_step_record(self, step: int, record: Any) -> str:
        """Write a step record JSON file.

        Returns the relative path within the trace directory.
        """
        rel_path = f"steps/{step:03d}.json"
        data = _serialize_fragment(record)
        _atomic_write(self._run_dir / rel_path, data)
        return rel_path

    def commit_step(
        self,
        step: int,
        step_file: str,
        observation_files: list[str] | None = None,
        message_input_file: str | None = None,
        message_output_file: str | None = None,
        message_files: list[str] | None = None,
    ) -> None:
        """Atomic manifest update — the commit point for a step.

        After this call, the step is durably committed. Any crash after
        commit but before the next step means we lose at most the
        in-progress step.
        """
        if self._manifest is None:
            msg = "WALWriter not initialized — call init_run() first"
            raise RuntimeError(msg)

        entry = StepManifestEntry(
            step=step,
            step_file=step_file,
            observation_files=observation_files or [],
            message_input_file=message_input_file,
            message_output_file=message_output_file,
            message_files=message_files or [],
        )
        self._manifest.steps.append(entry)
        self._manifest.last_step_completed = step
        self._manifest.updated_at = datetime.now(UTC)
        self._write_manifest()

    def commit_unfinished_messages(self, files: list[str]) -> None:
        """Record message files belonging to a step that never committed.

        Deliberately *not* ``commit_step``: no ``StepRecord`` exists, so
        claiming a completed step would put an action-less phantom in the
        manifest and advance ``last_step_completed`` past a step that
        never happened. The files are real and worth keeping — they are
        the prompt and completion of whatever the agent was doing when
        the run was killed — so they are listed on their own.
        """
        if self._manifest is None:
            msg = "WALWriter not initialized — call init_run() first"
            raise RuntimeError(msg)
        if not files:
            return

        self._manifest.unfinished_message_files.extend(files)
        self._manifest.updated_at = datetime.now(UTC)
        self._write_manifest()

    def write_result(self, result: Any) -> str:
        """Write the final result.json at run end."""
        rel_path = "result.json"
        data = _serialize_fragment(result)
        _atomic_write(self._run_dir / rel_path, data)
        return rel_path

    def finalize(self, status: RunStatus) -> None:
        """Update manifest status to completed or error."""
        if self._manifest is None:
            msg = "WALWriter not initialized — call init_run() first"
            raise RuntimeError(msg)

        self._manifest.status = status
        self._manifest.updated_at = datetime.now(UTC)
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Atomic manifest write."""
        if self._manifest is None:
            return
        data = self._manifest.model_dump_json(indent=2).encode("utf-8")
        _atomic_write(self._run_dir / "manifest.json", data)
