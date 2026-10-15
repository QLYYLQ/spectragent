"""Transient progress snapshots, never evidence for evaluation or replay."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from typing import Any


class ProgressState:
    """One session's state; used on its event loop with no global registry or I/O.

    The CLI owns task scheduling. The hook owns observed method calls. A
    snapshot reports both without treating a finished method as a WAL commit.
    """

    def __init__(self) -> None:
        self._tasks: list[dict[str, Any]] = []
        self._active_task: int | None = None
        self._active: dict[int, dict[str, Any]] = {}
        self._events: deque[str] = deque(maxlen=100)
        self._steps: deque[dict[str, Any]] = deque(maxlen=50)
        self._next_token = 0
        self._identity: dict[str, Any] = {}
        self._started = time.monotonic()
        self._run_started = self._started
        self._finished_at: float | None = None
        self._status = "preparing"
        self._task_id = ""
        self._run_outcome: str | None = None
        self._calls = 0
        self._tokens = 0
        self._usage_missing = 0
        self._policy_steps = 0
        self._error: str | None = None
        self._max_steps: int | None = None
        self._token_limit: int | str | None = None
        self._trace_path: str | None = None
        self._trace_locator: Callable[[], str | None] | None = None

    def configure(
        self, *, max_steps: int | None = None, token_limit: int | str | None = None
    ) -> None:
        self._max_steps = max_steps
        self._token_limit = token_limit

    def set_trace(self, path: str | None) -> None:
        self._trace_path = path

    def set_trace_locator(self, locator: Callable[[], str | None]) -> None:
        self._trace_locator = locator
        self._trace_path = None

    def set_tasks(self, task_ids: list[str]) -> None:
        self._tasks = [
            {"row_key": str(index), "task_id": task_id, "status": "queued"}
            for index, task_id in enumerate(task_ids)
        ]
        self._active_task = None

    def start_task(self, task_id: str) -> None:
        self._task_id = task_id
        index = next(
            (
                index
                for index, row in enumerate(self._tasks)
                if row["task_id"] == task_id and row["status"] == "queued"
            ),
            None,
        )
        if index is None:
            index = len(self._tasks)
            self._tasks.append({"row_key": str(index), "task_id": task_id})
        self._active_task = index
        self._tasks[index]["status"] = "running"
        self._trace_path = None
        self._trace_locator = None
        self._identity = {}
        self._steps.clear()
        self._active.clear()
        self._policy_steps = 0
        self._run_outcome = None
        self._error = None
        self._max_steps = None
        self._run_started = time.monotonic()
        self._status = "running"

    def finish_task(self, task_id: str, outcome: dict[str, Any]) -> None:
        index = self._active_task
        if index is None or self._tasks[index]["task_id"] != task_id:
            self.start_task(task_id)
            index = self._active_task
        assert index is not None
        self._tasks[index].update(
            {**deepcopy(outcome), "status": "finished", "row_key": str(index), "task_id": task_id}
        )
        self._active_task = None
        if "trace_dir" in outcome:
            self._trace_locator = None
            self.set_trace(str(outcome["trace_dir"]) if outcome["trace_dir"] is not None else None)

    def finish(self, reason: str = "finished") -> None:
        self._status = reason
        self._finished_at = time.monotonic()
        for row in self._tasks:
            if row.get("status") == "running":
                row.update(status="interrupted", stop_reason=reason)
        self._active_task = None

    @property
    def finished(self) -> bool:
        return self._finished_at is not None

    def cancelling(self) -> None:
        self._status = "cancelling"
        self._events.append("Cancellation requested; waiting for trace and environment cleanup.")

    def log(self, text: str) -> None:
        """A display-only line from the CLI; never a structured trace record."""
        if text.strip():
            self._events.append(text.rstrip()[:1000])

    def run_started(self, **identity: Any) -> None:
        max_steps = identity.pop("max_steps", None)
        if isinstance(max_steps, int):
            self._max_steps = max_steps
        self._identity = {key: str(value or "") for key, value in identity.items()}
        self._task_id = self._identity.get("task_id") or self._task_id
        self._active.clear()
        self._steps.clear()
        self._policy_steps = 0
        self._run_started = time.monotonic()
        self._run_outcome = None
        self._error = None
        self._status = "running"
        self._events.append(f"Started {self._task_id or 'task'}")

    def run_ended(self, result: Any) -> None:
        self._run_outcome = str(getattr(result, "agent_done", "unknown"))
        self._error = getattr(result, "error", None)
        self._events.append(f"Agent ended: {self._run_outcome}")
        if self._error:
            self._events.append(str(self._error)[:500])

    def begin(self, phase: str, detail: str = "") -> int:
        self._next_token += 1
        self._active[self._next_token] = {
            "phase": phase,
            "detail": detail[:160],
            "started": time.monotonic(),
        }
        return self._next_token

    def end(self, token: int, *, error: BaseException | None = None) -> None:
        operation = self._active.pop(token, None)
        if error is not None:
            phase = operation["phase"] if operation else "operation"
            self._events.append(f"{phase}: {type(error).__name__}: {str(error)[:300]}")

    def called(self, usage: Any) -> None:
        self._calls += 1
        if usage is None:
            self._usage_missing += 1
            return
        total = getattr(usage, "total_tokens", None)
        self._tokens += int(
            total
            or (getattr(usage, "prompt_tokens", 0) or 0)
            + (getattr(usage, "completion_tokens", 0) or 0)
        )

    def recorded(self, record: Any) -> None:
        origin = str(getattr(record, "origin", "policy"))
        self._policy_steps += origin == "policy"
        self._steps.append(
            {
                "step": getattr(record, "step", None),
                "origin": origin,
                "actions": ", ".join(
                    str(getattr(action, "name", "?")) for action in getattr(record, "actions", [])
                )[:160],
                "error": getattr(record, "error", None),
                "_record": record,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        now = self._finished_at or time.monotonic()
        active = [
            {**operation, "elapsed_s": max(0.0, now - operation["started"])}
            for operation in self._active.values()
        ]
        if self._trace_locator is not None:
            try:
                located = self._trace_locator()
                if located is not None:
                    self._trace_path = located
            except Exception:
                pass  # A display cannot prevent the run's close-out.
        steps = []
        for row in self._steps:
            record = row["_record"]
            detail = record.model_dump(mode="json") if hasattr(record, "model_dump") else {}
            steps.append(
                {**{key: value for key, value in row.items() if key != "_record"}, "detail": detail}
            )
        return deepcopy(
            {
                "status": self._status,
                "task_id": self._task_id,
                "identity": self._identity,
                "elapsed_s": max(0.0, now - self._started),
                "run_elapsed_s": max(0.0, now - self._run_started),
                "active": active,
                "phase": active[-1]["phase"] if active else "between operations",
                "tasks": self._tasks,
                "steps": steps,
                "max_steps": self._max_steps,
                "token_limit": self._token_limit,
                "trace_path": self._trace_path,
                "tasks_completed": sum(row.get("status") == "finished" for row in self._tasks),
                "tasks_total": len(self._tasks),
                "policy_steps": self._policy_steps,
                "calls": self._calls,
                "tokens": self._tokens,
                "usage_missing": self._usage_missing,
                "agent_done": self._run_outcome,
                "error": self._error,
                "events": list(self._events),
            }
        )
