"""VenusBench's launcher stops a GSATracking task's background thread when the task is torn down.

``venusbench_service/serve.py`` runs under VenusBench's interpreter; its top level
imports only the standard library, so it is loaded here to call the patch it
applies to VenusBench's ``TaskEval`` (``docs/architecture_design.md`` §11.2
"VenusBench-Mobile 的接入方案": an official-harness bug fixed on our side).
"""

from __future__ import annotations

import importlib.util
import threading
import time
from types import ModuleType

from spectragent.benchmarks.venusbench.service import LAUNCHER


def _launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("venusbench_launcher", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tasks() -> tuple[type, type, type]:
    """A fresh ``TaskEval`` stand-in and two tasks with threads, so each test wraps its own."""

    class TaskEval:
        """Stands in for VenusBench's ``TaskEval``: its ``tear_down`` records that it ran."""

        torn_down: object = None

        def tear_down(self, env: object) -> None:
            self.torn_down = env

    class Calls(TaskEval):
        """Like GSATrackingPhoneCall: a thread that calls until ``_stop_calling`` is set."""

        def start(self) -> None:
            self._stop_calling = False
            self.calls = 0

            def call() -> None:
                while not self._stop_calling:
                    self.calls += 1
                    time.sleep(0.01)

            self.call_thread = threading.Thread(target=call, daemon=True)
            self.call_thread.start()

        def tear_down(self, env: object) -> None:
            super().tear_down(env)

    class Texts(TaskEval):
        """Like the SMS tasks: a thread that texts until ``_stop_sending`` is set."""

        def start(self) -> None:
            self._stop_sending = False

            def text() -> None:
                while not self._stop_sending:
                    time.sleep(0.01)

            self.sms_thread = threading.Thread(target=text, daemon=True)
            self.sms_thread.start()

    return TaskEval, Calls, Texts


def test_tear_down_stops_and_joins_a_tasks_call_or_sms_thread_first() -> None:
    task_eval, calls_task, texts_task = _tasks()
    notes: list[str] = []
    _launcher().stop_background_threads(task_eval, warn=notes.append, join_s=2.0)
    calls, texts = calls_task(), texts_task()
    calls.start()
    texts.start()
    calls.tear_down("env")
    texts.tear_down("env")
    assert not calls.call_thread.is_alive() and not texts.sms_thread.is_alive()
    assert calls.torn_down == texts.torn_down == "env", "the task's own tear-down still runs"
    stopped = calls.calls
    time.sleep(0.05)
    assert calls.calls == stopped, "no call after the tear-down"
    assert notes == []


def test_a_task_without_a_thread_is_torn_down_as_before() -> None:
    task_eval, _, _ = _tasks()
    _launcher().stop_background_threads(task_eval, warn=lambda note: None)
    task = task_eval()
    task.tear_down("env")
    assert task.torn_down == "env"
    assert not hasattr(task, "_stop_calling") and not hasattr(task, "_stop_sending")


def test_a_thread_that_does_not_stop_in_time_is_named() -> None:
    task_eval, _, _ = _tasks()
    release = threading.Event()

    class _Stubborn(task_eval):
        call_thread = threading.Thread(target=release.wait, daemon=True)

    notes: list[str] = []
    _launcher().stop_background_threads(task_eval, warn=notes.append, join_s=0.05)
    task = _Stubborn()
    task.call_thread.start()
    task.tear_down("env")
    release.set()
    assert notes == ["_Stubborn's call_thread still runs 0.05 s after it was stopped"]
    assert task.torn_down == "env"
