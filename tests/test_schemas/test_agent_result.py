"""``AgentResult``'s self-report: the agent's claim about the task, or ``None`` for none.

Version 4.0 reads ``agent_reported_success`` from the done result's claim,
not from the done action's execution outcome, so a 3.0 result is refused
rather than adapted (``docs/architecture_design.md`` §4.1, "agent 的任务结论").
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.agent_result import AgentMetrics, AgentResult


def _result(**fields: object) -> AgentResult:
    base: dict[str, object] = {
        "task_id": "t-1",
        "timestamp": datetime(2026, 10, 23, tzinfo=UTC),
        "env_status": EnvironmentStatus.SUCCESS,
        "agent_done": AgentDoneStatus.DONE,
        "answer": "无法完成",
        "metrics": AgentMetrics(steps=1),
    }
    return AgentResult.model_validate({**base, **fields})


@pytest.mark.parametrize("claim", [True, False, None])
def test_the_claim_round_trips(claim: bool | None) -> None:
    result = _result(agent_reported_success=claim)
    back = AgentResult.model_validate_json(result.model_dump_json())
    assert back == result
    assert back.schema_version == "4.0"
    assert back.agent_reported_success is claim


def test_a_run_that_never_called_done_has_no_claim() -> None:
    with pytest.raises(ValidationError, match="agent_reported_success must be None"):
        _result(agent_done=AgentDoneStatus.MAX_STEPS, agent_reported_success=False)


def test_a_result_from_before_the_claim_was_its_own_fact_is_refused() -> None:
    written = _result(agent_reported_success=None).model_dump(mode="json")
    with pytest.raises(ValidationError, match="schema_version"):
        AgentResult.model_validate({**written, "schema_version": "3.0"})
