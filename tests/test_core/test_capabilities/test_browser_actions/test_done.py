"""DoneAction — verifies the non-CDP terminal action contract.

Unlike other actions, ``DoneAction`` does not emit an event. It takes
``extracted_content`` and ``success`` from the action params and returns
``ActionResult(success=True, is_done=True, extracted_content=...,
reported_success=...)``: its ``success`` says it executed (the run closed),
and the agent's claim about the task is ``reported_success``, ``None`` when
the agent made none (``docs/architecture_design.md`` §4.1, "agent 的任务结论").
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.actions.done import DoneAction
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestDoneDescriptor:
    def test_descriptor_returns_extracted_content_param(self) -> None:
        desc = DoneAction().descriptor()
        assert desc.name == "browser.done"
        assert "extracted_content" in desc.params


class TestDoneExecute:
    async def test_sets_is_done_true(self, fake_capability: FakeCapability) -> None:
        result = await DoneAction().execute(
            {"extracted_content": "42"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.success is True
        assert result.extracted_content == "42"

    async def test_missing_extracted_content_is_none(self, fake_capability: FakeCapability) -> None:
        result = await DoneAction().execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.extracted_content is None

    @pytest.mark.parametrize("claim", [True, False, None])
    async def test_the_claim_is_carried_apart_from_execution(
        self, fake_capability: FakeCapability, claim: bool | None
    ) -> None:
        """An executed done always succeeded; what the agent said about the task rides beside it."""
        params: dict[str, object] = {"extracted_content": "answer"}
        if claim is not None:
            params["success"] = claim
        result = await DoneAction().execute(
            params,
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.is_done is True
        assert result.success is True
        assert result.reported_success is claim

    def test_the_success_param_says_what_it_means(self) -> None:
        """A model that sees params is told the param is a claim, and that it may leave it unset."""
        text = DoneAction().descriptor().params["success"]
        assert "whether the task was accomplished" in text
        assert "leave it unset if you cannot tell" in text

    async def test_emits_no_cdp_event(self, fake_capability: FakeCapability) -> None:
        scope = fake_capability._scopes["sid_main"]
        await DoneAction().execute(
            {"extracted_content": "x"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert scope.emitted == []
