"""``browser.select_dropdown`` reports a measured ``SelectionEffect``.

The selection script itself reads the ``<select>`` right before and right
after it selects (``effect_probes.SELECT_FACTS_JS``), so the effect costs no
probe of its own: the option selected after the action (index and visible
text) and whether the selection changed (``docs/architecture_design.md``
§4.5, "动作效果"). A control the shared sensitivity rule
(``dom/sensitivity.py``) judges sensitive keeps its option text in the page
when the observation it was indexed from already said so, and has it dropped
when only the page's current attributes say so. Here the page's answers are
scripted; the real-Chrome half is ``test_browser_effects_navigation_live.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from agent_cdp import ScopeGroup

from spectragent.core.capabilities.browser.actions.select_dropdown import SelectDropdownAction
from spectragent.core.capabilities.browser.dom.models import DOMElementNode
from spectragent.core.capabilities.browser.effect_probes import selection_facts
from spectragent.core.capabilities.browser.effects import OptionFacts, SelectionEffect
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from tests.test_core.test_capabilities.test_browser_actions.conftest import FakeCapability


class DropdownPage:
    """Answers the selection script with a scripted reading of the ``<select>``."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.scripts: list[str] = []

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Any:
        params = params or {}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "select-1"}}
        if method == "Runtime.callFunctionOn":
            self.scripts.append(params["functionDeclaration"])
            return {"result": {"value": self.answer}}
        return {}


def reading(
    before: int, index: int, text: str | None, *, attrs: dict[str, str] | None = None
) -> dict[str, Any]:
    """What the selection script answers: it selected, and the ``<select>`` before and after."""
    after = {"index": index, "text": text, "tag": "SELECT", "attrs": attrs or {}}
    return {"selected": True, "before": before, "after": after}


@asynccontextmanager
async def dropdown(
    page: DropdownPage, *, attributes: dict[str, str] | None = None
) -> AsyncIterator[FakeCapability]:
    """A focused tab whose selector map holds one ``<select>`` at index 7."""
    node = DOMElementNode(
        tag_name="select", backend_node_id=7, attributes=attributes or {"id": "fruit"}
    )
    group = ScopeGroup("dropdown-effects")
    scope = await group.create_scope("tab")
    watchdog = ActionWatchdog(
        page,  # type: ignore[arg-type]
        input_platform="linux",
        selector_map_getter=lambda: {7: node},
    )
    watchdog.attach(scope, "sid_main")
    capability = FakeCapability()
    capability._scopes["sid_main"] = scope  # type: ignore[assignment]
    capability._selector_maps["sid_main"] = {7: node}  # type: ignore[dict-item]
    capability._default_session_id = "sid_main"
    try:
        yield capability
    finally:
        await group.close_all()


async def _select(capability: FakeCapability, **params: Any) -> Any:
    return await SelectDropdownAction().execute({"index": 7, **params}, capability=capability)  # type: ignore[arg-type]


class TestSelectionEffect:
    async def test_reports_the_option_selected_and_that_it_changed(self) -> None:
        page = DropdownPage(reading(0, 1, "Banana"))
        async with dropdown(page) as capability:
            result = await _select(capability, text="Banana")
        assert result.success, result.error
        effect = result.effect
        assert isinstance(effect, SelectionEffect)
        assert effect.effect_id == "browser.select_dropdown"
        assert (effect.selected, effect.value_changed) == (
            OptionFacts(index=1, text="Banana"),
            True,
        )
        assert (
            effect.describe() == 'The dropdown now shows option 1 "Banana"; the selection changed.'
        )

    async def test_selecting_the_option_already_shown_did_not_change_it(self) -> None:
        page = DropdownPage(reading(1, 1, "Banana"))
        async with dropdown(page) as capability:
            result = await _select(capability, option_index=1)
        assert result.effect.value_changed is False
        assert result.effect.describe() == (
            'The dropdown now shows option 1 "Banana"; the selection did not change.'
        )

    async def test_nothing_selected_before_is_a_change(self) -> None:
        page = DropdownPage(reading(-1, 0, "Apple"))
        async with dropdown(page) as capability:
            result = await _select(capability, value="apple")
        assert result.effect.value_changed is True

    async def test_a_sensitive_control_keeps_its_option_text_in_the_page(self) -> None:
        """The observation already judged it sensitive, so the script is told not to read the text."""
        page = DropdownPage(reading(0, 2, None, attrs={"autocomplete": "cc-exp-month"}))
        async with dropdown(page, attributes={"autocomplete": "cc-exp-month"}) as capability:
            result = await _select(capability, option_index=2)
        (script,) = page.scripts
        assert "selectFacts(this, true)" in script
        assert result.effect.selected == OptionFacts(index=2, text=None)
        assert result.effect.describe() == (
            "The dropdown now shows option 2 (text withheld: a sensitive field); "
            "the selection changed."
        )

    async def test_a_control_the_page_made_sensitive_since_the_observation_drops_the_text(
        self,
    ) -> None:
        page = DropdownPage(reading(0, 1, "03", attrs={"name": "card-pin"}))
        async with dropdown(page) as capability:
            result = await _select(capability, option_index=1)
        (script,) = page.scripts
        assert "selectFacts(this, false)" in script
        assert result.effect.selected == OptionFacts(index=1, text=None)
        assert "03" not in result.effect.describe()

    async def test_a_reading_without_the_selection_facts_leaves_them_unknown(self) -> None:
        page = DropdownPage({"selected": True})
        async with dropdown(page) as capability:
            result = await _select(capability, option_index=1)
        assert result.success, "the selection happened; only its measurement is missing"
        assert (result.effect.selected, result.effect.value_changed) == ("unknown", "unknown")
        assert result.effect.describe() == (
            "The dropdown's selected option is unknown; whether the selection changed is unknown."
        )

    @pytest.mark.parametrize("answer", [False, {"selected": False, "before": 0}])
    async def test_a_failed_selection_reports_no_effect(self, answer: Any) -> None:
        page = DropdownPage(answer)
        async with dropdown(page) as capability:
            result = await _select(capability, text="Durian")
        assert result.success is False
        assert "no enabled option" in (result.error or "")
        assert result.effect is None


class TestSelectionFacts:
    def test_an_unreadable_sensitivity_never_quotes_the_text(self) -> None:
        facts = selection_facts(0, {"index": 1, "text": "Banana"}, withheld=False)
        assert facts == {"selected": "unknown", "value_changed": True}

    def test_a_boolean_is_not_an_index(self) -> None:
        after = {"index": True, "text": "Banana", "tag": "SELECT", "attrs": {}}
        assert selection_facts(False, after, withheld=False) == {
            "selected": "unknown",
            "value_changed": "unknown",
        }
