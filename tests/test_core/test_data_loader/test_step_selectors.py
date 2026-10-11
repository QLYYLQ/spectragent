"""Step selectors: where a ``trace_branches`` sweep forks (``docs/architecture_design.md`` §4.10).

A selector chooses among a source's candidate steps; the loader owns skipping
(and reporting) a chosen step that is not a candidate. These tests pin each
strategy's choice and the YAML surface of its knobs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spectragent.core.data_loader.task.step_selector import (
    AllSteps,
    EveryKSteps,
    ExplicitSteps,
    LastStep,
    StepSelector,
    StepSelectorRegistrar,
    default_step_selector,
)


def test_the_four_shipped_selectors_are_registered() -> None:
    shipped = {
        key
        for key, cls in StepSelectorRegistrar.get_all().items()
        if cls.__module__.startswith("spectragent.")
    }
    assert shipped == {"all", "every_k", "explicit", "last"}


@pytest.mark.parametrize(
    "selector", [AllSteps(), EveryKSteps(), ExplicitSteps(steps=[1]), LastStep()]
)
def test_each_is_a_step_selector(selector: StepSelector) -> None:
    assert isinstance(selector, StepSelector)
    assert StepSelectorRegistrar.get(selector.selector_id) is type(selector)


def test_the_default_is_all() -> None:
    selector = default_step_selector()
    assert isinstance(selector, AllSteps) and selector.selector_id == "all"


class TestAll:
    def test_every_candidate_ascending_once(self) -> None:
        assert AllSteps().select([3, 0, 1, 3]) == [0, 1, 3]

    def test_no_candidates(self) -> None:
        assert AllSteps().select([]) == []


class TestEveryK:
    def test_k_1_is_every_candidate(self) -> None:
        assert EveryKSteps().select([0, 1, 2, 5]) == [0, 1, 2, 5]

    def test_counted_by_step_number_not_by_position(self) -> None:
        """Step 4 is not branchable: the choice stays at 0, 2, 6 rather than shifting."""
        assert EveryKSteps(k=2).select([0, 1, 2, 3, 5, 6, 7]) == [0, 2, 6]

    def test_the_offset_is_the_first_step_counted(self) -> None:
        assert EveryKSteps(k=3, offset=1).select(list(range(10))) == [1, 4, 7]
        assert EveryKSteps(k=1, offset=8).select(list(range(10))) == [8, 9]


class TestExplicit:
    def test_the_named_steps_whether_or_not_they_are_candidates(self) -> None:
        """The loader, not the selector, skips and reports a step a source lacks."""
        assert ExplicitSteps(steps=[7, 2, 2]).select([0, 1, 2]) == [2, 7]

    def test_at_least_one_step(self) -> None:
        with pytest.raises(ValueError, match="at least one step"):
            ExplicitSteps(steps=[])


class TestLast:
    """Each source's last candidate — what the metrics chain's same-path replay forks at (M33)."""

    def test_the_latest_candidate_only(self) -> None:
        assert LastStep().select([0, 1, 2, 5]) == [5]

    def test_unordered_candidates(self) -> None:
        assert LastStep().select([3, 0, 7, 1]) == [7]

    def test_no_candidates(self) -> None:
        assert LastStep().select([]) == []

    def test_its_yaml_block_has_no_knobs(self) -> None:
        from spectragent.generated._composed import LastStep_SelectorConfig

        assert LastStep_SelectorConfig.model_validate({}).model_dump() == {"selector_id": "last"}
        with pytest.raises(ValidationError):
            LastStep_SelectorConfig.model_validate({"steps": [1]})


class TestYamlSurface:
    """The knobs as the generated config types them (regenerate after a change)."""

    def test_every_k_bounds(self) -> None:
        from spectragent.generated._composed import EveryKStep_SelectorConfig

        assert EveryKStep_SelectorConfig.model_validate({}).model_dump() == {
            "k": 1,
            "offset": 0,
            "selector_id": "every_k",
        }
        with pytest.raises(ValidationError):
            EveryKStep_SelectorConfig.model_validate({"k": 0})
        with pytest.raises(ValidationError):
            EveryKStep_SelectorConfig.model_validate({"offset": -1})

    def test_explicit_steps_are_required(self) -> None:
        from spectragent.generated._composed import ExplicitStep_SelectorConfig

        with pytest.raises(ValidationError, match="steps"):
            ExplicitStep_SelectorConfig.model_validate({})
        assert ExplicitStep_SelectorConfig.model_validate({"steps": [0, 3]}).steps == [0, 3]

    def test_a_misspelled_selector_id_is_rejected(self) -> None:
        from pydantic import TypeAdapter

        from spectragent.generated._composed import StepSelectorConfig

        adapter: TypeAdapter[object] = TypeAdapter(StepSelectorConfig)
        assert adapter.validate_python({"selector_id": "every_k", "k": 2}).k == 2  # type: ignore[attr-defined]
        with pytest.raises(ValidationError, match="every_kk"):
            adapter.validate_python({"selector_id": "every_kk", "k": 2})
