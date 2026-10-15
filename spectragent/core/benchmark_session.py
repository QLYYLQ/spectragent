"""The selected tasks and their disposition, outside every run's WAL.

This small execution ledger lets a viewer distinguish an intentionally short
batch from one stopped before every selected task ran. It is not a heartbeat:
``running`` in a file says only that the owner did not record its termination.
Task instructions, configuration, credentials and error messages are never copied.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from spectragent.monitoring.protocols import ProgressSink
from spectragent.schemas.config_source import ConfigSource


class TaskProgress(ProgressSink, Protocol):
    """The batch scheduler's transient display surface."""

    def set_tasks(self, task_ids: list[str]) -> None: ...

    def start_task(self, task_id: str) -> None: ...

    def finish_task(self, task_id: str, outcome: dict[str, Any]) -> None: ...

    def finish(self, reason: str) -> None: ...


class SessionLedger(Protocol):
    """A batch plan and task dispositions, separate from run evidence."""

    def start(self, task_ids: list[str]) -> None: ...

    def start_task(self, task_id: str) -> None: ...

    def finish_task(self, task_id: str, outcome: dict[str, Any]) -> None: ...

    def finish(self, reason: str) -> None: ...


class BatchSession:
    def __init__(
        self,
        directory: Path,
        *,
        benchmark: str,
        subset: str,
        config_source: ConfigSource | None = None,
    ) -> None:
        self.path = directory / f"{uuid.uuid4().hex}.json"
        self._data: dict[str, Any] = {
            "format": "af-session-1",
            "session_id": self.path.stem,
            "benchmark": benchmark,
            "subset": subset,
            "status": "planned",
            "stop_reason": None,
            "tasks": [],
        }
        self._active: int | None = None
        if config_source is not None:
            self._data["config_source"] = config_source.model_dump(mode="json")

    def start(self, task_ids: list[str]) -> None:
        self._data["tasks"] = [
            {"task_id": task_id, "status": "not_started"} for task_id in task_ids
        ]
        self._data["status"] = "running"
        self._data["started_at"] = self._now()
        self._write()

    def start_task(self, task_id: str) -> None:
        for index, row in enumerate(self._data["tasks"]):
            if row["task_id"] == task_id and row["status"] == "not_started":
                self._active = index
                row.update(status="running", started_at=self._now())
                self._write()
                return
        raise ValueError(f"Task {task_id!r} is not pending in this batch")

    def finish_task(self, task_id: str, outcome: dict[str, Any]) -> None:
        if self._active is None:
            raise ValueError("No task is active in this batch")
        row = self._data["tasks"][self._active]
        if row["task_id"] != task_id:
            raise ValueError("The completed task differs from the active task")
        row.update(
            status="finished",
            finished_at=self._now(),
            **{
                key: outcome.get(key)
                for key in (
                    "agent_done",
                    "env_status",
                    "trace_status",
                    "trace_dir",
                    "elapsed_ms",
                    "infrastructure_failure",
                    "vetoed",
                    "tokens",
                )
            },
        )
        self._active = None
        self._write()

    def finish(self, reason: str) -> None:
        if self._active is not None:
            self._data["tasks"][self._active].update(status="interrupted", finished_at=self._now())
        self._data.update(status="closed", stop_reason=reason, finished_at=self._now())
        self._write()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
