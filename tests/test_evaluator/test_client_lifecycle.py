"""The standalone judge owns a batch's model use, independently of AgentRuntime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.evaluator._registrar import LLMJudgeEvaluator
from spectragent.evaluator.exact_match import ExactMatchEvaluator
from spectragent.evaluator.runner import EvaluationRunner
from spectragent.schemas.eval_reference import EvalReference
from tests.test_evaluator.conftest import write_trace


class _Judge(LLMJudgeEvaluator):
    __abstract__ = True
    __registry_key__ = "client_lifecycle_test"

    async def evaluate(self, trace: Any, reference: EvalReference) -> Any:
        await self.ask(text="first judge turn")
        await self.ask(text="second judge turn")
        return await ExactMatchEvaluator().evaluate(trace, reference)


class _Loader:
    def get(self, task_id: str) -> EvalReference:
        return EvalReference(task_id=task_id, benchmark="offline_bench", expected_answer="answer")


class _Transport(httpx.AsyncBaseTransport):
    def __init__(
        self, *, fail: bool = False, pause: asyncio.Event | None = None, close_error: bool = False
    ) -> None:
        self.calls = 0
        self.closed = False
        self.fail = fail
        self.pause = pause
        self.close_error = close_error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.pause:
            self.pause.set()
            await asyncio.Event().wait()
        if self.fail:
            raise httpx.ConnectError("judge request failed")
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("judge cleanup failed")


def _runner(
    monkeypatch: Any,
    *,
    fail: bool = False,
    pause: asyncio.Event | None = None,
    close_error: bool = False,
):
    from openai import AsyncOpenAI

    transports: list[_Transport] = []

    def factory() -> AsyncOpenAI:
        transport = _Transport(fail=fail, pause=pause, close_error=close_error)
        transports.append(transport)
        return AsyncOpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=transport),
        )

    model = ChatOpenAI(model="test", api_key="test", max_retries=0)
    monkeypatch.setattr(model, "get_client", factory)
    evaluator = _Judge()
    evaluator.llm = model  # type: ignore[assignment]
    return EvaluationRunner(evaluator=evaluator, eval_loader=_Loader()), transports


async def test_judge_batch_reuses_client_and_releases_before_return(
    tmp_path: Path, monkeypatch: Any
):
    paths = [write_trace(tmp_path, run_id=f"run-{i}", task_id=f"task-{i}") for i in range(2)]
    runner, transports = _runner(monkeypatch)
    await runner.run(paths)
    assert len(transports) == 1
    assert transports[0].calls == 4
    assert transports[0].closed
    await runner.run(paths[:1])
    assert len(transports) == 2
    assert transports[1].calls == 2
    assert transports[1].closed


async def test_direct_trace_evaluation_also_ends_judge_execution(tmp_path: Path, monkeypatch: Any):
    runner, transports = _runner(monkeypatch)
    await runner.evaluate_trace(write_trace(tmp_path))
    assert transports[0].calls == 2
    assert transports[0].closed


async def test_judge_request_failure_remains_evaluation_error(tmp_path: Path, monkeypatch: Any):
    runner, transports = _runner(monkeypatch, fail=True)
    report = await runner.run([write_trace(tmp_path)])
    assert report.errors
    assert transports[0].closed


async def test_external_cancellation_still_releases_judge(tmp_path: Path, monkeypatch: Any):
    pause = asyncio.Event()
    runner, transports = _runner(monkeypatch, pause=pause)
    task = asyncio.create_task(runner.run([write_trace(tmp_path)]))
    await asyncio.wait_for(pause.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transports[0].closed


@pytest.mark.parametrize("fail", [False, True])
async def test_judge_cleanup_error_does_not_change_verdict_or_request_failure(
    tmp_path: Path, monkeypatch: Any, caplog: Any, fail: bool
):
    runner, transports = _runner(monkeypatch, fail=fail, close_error=True)
    report = await runner.run([write_trace(tmp_path)])
    assert "judge cleanup failed" in caplog.text
    assert transports[0].calls == (1 if fail else 2)
    if fail:
        assert report.errors
        error = report.errors[0].error
        assert error is not None
        assert "judge cleanup failed" not in error
    else:
        assert not report.errors
        assert len(report.graded) == 1


async def test_unused_judge_cleanup_does_not_construct_client(monkeypatch: Any):
    runner, transports = _runner(monkeypatch)
    await runner.aclose()
    assert transports == []
