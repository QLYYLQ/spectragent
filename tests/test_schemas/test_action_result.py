"""``ActionResult``: execution apart from the agent's claim about the task.

``success`` is whether an action executed, the done action included. The
claim a done carries is ``reported_success`` (``docs/architecture_design.md``
§4.1, "agent 的任务结论"), and only a result that ends the run can carry one.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from spectragent.schemas.action import ActionResult


@pytest.mark.parametrize("claim", [True, False])
def test_a_done_result_writes_its_claim_and_reads_it_back(claim: bool) -> None:
    result = ActionResult(success=True, is_done=True, extracted_content="x", reported_success=claim)
    written = json.loads(result.model_dump_json())
    assert written["success"] is True
    assert written["reported_success"] is claim
    assert ActionResult.model_validate(written) == result


def test_no_claim_writes_what_a_result_always_wrote() -> None:
    """A result with no claim leaves the key out: every other action's record is unchanged."""
    for result in (
        ActionResult(success=True),
        ActionResult(success=False, error="no target"),
        ActionResult(success=True, is_done=True, extracted_content="x"),
    ):
        written = json.loads(result.model_dump_json())
        assert "reported_success" not in written
        assert ActionResult.model_validate(written).reported_success is None


def test_only_the_action_that_ends_the_run_carries_a_claim() -> None:
    with pytest.raises(ValidationError, match="is_done=False cannot carry one"):
        ActionResult(success=True, reported_success=True)
