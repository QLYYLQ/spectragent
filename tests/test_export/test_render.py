"""Student rendering on WAL-built traces (arch §4.15 "学生渲染", "round-trip").

The student is the shipped ``ui_tars_student.yaml`` (UI-TARS, ``normalized_1000``,
pinned DPR 1), composed by the real Composer and only assembled. Each test
builds a recorded UI-TARS (or DOM) run with the real ``WALWriter``,
canonicalises and translates it with the real pipeline pieces, and renders it.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from spectragent.agents._registrar import AgentRegistrar
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import AssistantMessage, SystemMessage, UserMessage
from spectragent.core.loops.validation import IncompatibleError
from spectragent.core.negotiation.observation import IncompatibleFragmentError
from spectragent.core.trace.models import TraceNegotiation
from spectragent.core.trace.reader import RunTrace
from spectragent.export import canonicalise
from spectragent.export.errors import (
    ExportConfigError,
    RoundTripError,
    StudentCaptureMismatchError,
    UntranslatableActionError,
)
from spectragent.export.render import StudentRenderer, TrajectoryStep
from spectragent.export.translate.identity import IdentityTranslator
from spectragent.schemas.action import Action
from spectragent.schemas.observation.fragments import StepInfoObs
from tests._helpers.export_runs import STUDENT_CONFIG, student_root
from tests._helpers.export_traces import (
    UI_TARS_NEGOTIATION,
    ExportStep,
    click_at,
    finished,
    screenshot,
    ui_tars_step,
    write_ui_tars_run,
)


def _renderer(
    composer: ConfigComposer,
    *,
    untranslatable: str = "drop_step",
    thought_source: str = "teacher",
    thought_index_refs: str = "drop",
    root: dict[str, Any] | None = None,
) -> StudentRenderer:
    return StudentRenderer(
        composer.compose(root if root is not None else student_root()),
        untranslatable=untranslatable,  # type: ignore[arg-type]
        thought_source=thought_source,  # type: ignore[arg-type]
        thought_index_refs=thought_index_refs,  # type: ignore[arg-type]
    )


def _trajectory(trace: RunTrace, *, thought_source: str = "teacher") -> list[TrajectoryStep]:
    samples = canonicalise(
        trace,
        rel_path=trace.run_id,
        thought_source=thought_source,  # type: ignore[arg-type]
        teacher_cls=AgentRegistrar.get(trace.config.agent_id),
    )
    translator = IdentityTranslator()
    by_step = {s.step: translator.translate(s, trace).applied(s) for s in samples}
    return [
        TrajectoryStep(trace, trace.run_id, record, by_step.get(record.step))
        for record in trace.step_records
    ]


def _three_steps(tmp_path: Path, middle: Action | None = None, **kwargs: Any) -> RunTrace:
    return RunTrace(
        write_ui_tars_run(
            tmp_path,
            "run-1",
            [
                ui_tars_step(click_at(640, 360), thought="open the menu", color=10),
                ui_tars_step(middle or click_at(100, 200), thought="pick the kettle", color=20),
                ui_tars_step(finished("added"), thought="done", color=30),
            ],
            **kwargs,
        )
    )


class TestAssembly:
    def test_render_makes_no_llm_call_and_no_setup(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sentinels on the model and on every setup: rendering reaches none of them."""

        class Sentinel(Exception):
            pass

        calls: list[str] = []

        async def no_model(*_args: Any, **_kwargs: Any) -> Any:
            calls.append("llm")
            raise Sentinel("the export called the model")

        async def no_setup(*_args: Any, **_kwargs: Any) -> None:
            calls.append("setup")
            raise Sentinel("the export set the environment up")

        monkeypatch.setattr(BrowserCapability, "setup", no_setup)
        renderer = _renderer(composer)
        llm = renderer.runtime.agent.llm
        monkeypatch.setattr(llm, "ainvoke", no_model)
        rendered = renderer.render(_trajectory(_three_steps(tmp_path)))
        assert [r.sample.step for r in rendered] == [0, 1, 2]
        assert calls == []
        # The sentinels sit exactly where a run would reach them.
        with pytest.raises(Sentinel):
            asyncio.run(renderer.agent.llm.ainvoke([]))
        with pytest.raises(Sentinel):
            asyncio.run(renderer.runtime.setup_environment(None))

    def test_the_context_is_the_students_own_assembly(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        trace = _three_steps(tmp_path)
        renderer = _renderer(composer)
        rendered = renderer.render(_trajectory(trace))
        seed = renderer.runtime.seed_messages(trace.config.task)
        assert len(seed) == 1 and isinstance(seed[0], SystemMessage)
        first, second, third = rendered
        assert first.context[0].text == seed[0].text  # the student's own prepare()
        assert [type(m).__name__ for m in third.context] == [
            "SystemMessage",
            "UserMessage",
            "AssistantMessage",
            "UserMessage",
            "AssistantMessage",
            "UserMessage",
        ]
        # Earlier assistant turns are the student's replies to the earlier steps.
        assert [m.text for m in third.context if isinstance(m, AssistantMessage)] == [
            first.target,
            second.target,
        ]
        assert first.target == "Thought: open the menu\nAction: click(point='(500, 500)')"
        assert third.target == "Thought: done\nAction: finished(content='added')"
        # Each observation turn shows that step's recorded screenshot.
        for step, item in enumerate(rendered):
            last = item.context[-1]
            assert isinstance(last, UserMessage)
            assert isinstance(last.content, list)
            url = last.content[1].image_url.url  # type: ignore[union-attr]
            shot = trace.observation_fragment(step, "browser.screenshot")
            assert shot is not None
            assert base64.b64decode(url.partition(",")[2]) == shot.image_bytes  # type: ignore[union-attr]
        assert [r.context_steps for r in rendered] == [(), (0,), (0, 1)]
        assert {r.segment for r in rendered} == {0}

    def test_the_target_label_is_the_quantised_point(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """x=3 on a 1280 px viewport is not on the 0-1000 grid: the label is where it lands."""
        trace = RunTrace(write_ui_tars_run(tmp_path, "run-q", [ui_tars_step(click_at(3, 700))]))
        (item,) = _renderer(composer).render(_trajectory(trace))
        assert item.target.endswith("click(point='(2, 972)')")
        executed = item.target_actions[0].params
        assert (executed["coordinate_x"], executed["coordinate_y"]) == (3, 700)
        assert item.quantization == (0,)
        trace2 = RunTrace(
            write_ui_tars_run(tmp_path / "b", "run-r", [ui_tars_step(click_at(7, 700))])
        )
        (lossy,) = _renderer(composer).render(_trajectory(trace2))
        assert lossy.target.endswith("click(point='(5, 972)')")
        # x=7 is one of the 280 the grid cannot give back: grid 5 executes at x=6.
        assert lossy.target_actions[0].params["coordinate_x"] == 6
        assert lossy.quantization == (1,)

    def test_the_assembly_is_prepare_itself(self, composer: ConfigComposer) -> None:
        """The student's descriptors are the run's: narrowed by its fragments, bound, guarded."""
        renderer = _renderer(composer)
        descriptors = {d.name: d for d in renderer.runtime.active_descriptors}
        assert "browser.click" not in descriptors, "a vision student offers no index click"
        scroll = descriptors["browser.scroll"].params_schema or {}
        assert "index" not in (scroll.get("properties") or {}), (
            "scroll's index needs browser.dom, which a vision student does not negotiate"
        )
        assert renderer.guarded == frozenset(descriptors)
        assert renderer.bindings["click_coordinate"] == "browser.click_coordinate"
        assert renderer.env_fragments == frozenset({"browser.screenshot", "browser.platform"})

    def test_missing_format_slot_names_training_export(self, composer: ConfigComposer) -> None:
        root = student_root(agent_id="browser_use", observation_mode="vision")
        for key in ("prompt_template", "coordinate_space"):
            root["agent"][0].pop(key)
        with pytest.raises(IncompatibleError) as excinfo:
            _renderer(composer, root=root)
        assert excinfo.value.missing_slots == ["format_react_output"]
        assert "needed for training export (optional slots of loop 'react')" in str(excinfo.value)

    def test_only_one_hookless_react_student(self, composer: ConfigComposer) -> None:
        root = student_root()
        root["hook"] = [{"hook_id": "metrics"}]
        with pytest.raises(ExportConfigError, match="hooks"):
            _renderer(composer, root=root)
        root = student_root()
        root["agent"].append({**root["agent"][0], "id": "second"})
        with pytest.raises(ExportConfigError, match="exactly one agent"):
            _renderer(composer, root=root)

    def test_from_config_reads_the_file(self, composer: ConfigComposer) -> None:
        renderer = StudentRenderer.from_config(
            STUDENT_CONFIG,
            composer=composer,
            untranslatable="drop_step",
            thought_source="none",
            thought_index_refs="drop",
        )
        assert (renderer.agent_id, renderer.loop_id, renderer.coordinate_space) == (
            "ui_tars",
            "react",
            "normalized_1000",
        )
        assert len(renderer.config_sha256) == 64


class TestWhatTheStudentNeeds:
    def test_student_fragment_missing_from_trace_is_named(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        dom_only = TraceNegotiation(
            active_fragments=["browser.dom", "browser.platform", "loop.task"],
            env_sourced_fragments=["browser.dom", "browser.platform"],
            loop_sourced_fragments=["loop.task"],
        )
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path, "run-dom", [ui_tars_step(click_at(1, 1))], negotiation=dom_only
            )
        )
        with pytest.raises(IncompatibleFragmentError) as excinfo:
            _renderer(composer).check_source(trace, "recorded/run-dom")
        assert excinfo.value.missing == {"browser.screenshot"}
        assert excinfo.value.provider == "source run 'recorded/run-dom' (as recorded)"
        assert "browser.screenshot ← ui_tars (the student)" in str(excinfo.value)

    def test_capture_mismatch_is_named(self, composer: ConfigComposer, tmp_path: Path) -> None:
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path,
                "run-dpr2",
                [ExportStep(actions=[click_at(10, 10)], fragments=[screenshot(dpr=2)])],
            )
        )
        with pytest.raises(StudentCaptureMismatchError) as excinfo:
            _renderer(composer).render(_trajectory(trace))
        assert (excinfo.value.source, excinfo.value.step) == ("run-dpr2", 0)
        assert excinfo.value.mismatches == (
            "browser.screenshot: device scale factor 1 configured, 2 recorded",
            "browser.screenshot: image size 1280x720 implied by the configuration, "
            "2560x1440 recorded",
        )

    def test_a_step_without_the_screenshot_ends_the_segment(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        negotiation = UI_TARS_NEGOTIATION.model_copy(update={"required_fragments": ["loop.task"]})
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path,
                "run-gap",
                [
                    ui_tars_step(click_at(1, 1)),
                    ExportStep(actions=[click_at(2, 2)], failed=["browser.screenshot"]),
                    ui_tars_step(click_at(3, 3)),
                ],
                negotiation=negotiation,
            )
        )
        renderer = _renderer(composer)
        rendered = renderer.render(_trajectory(trace))
        assert [(r.sample.step, r.segment, r.context_steps) for r in rendered] == [
            (0, 0, ()),
            (2, 1, ()),
        ]
        assert renderer.stats.skipped == {"fragment_unavailable": 1}


class TestUntranslatable:
    """``untranslatable`` decides what a step the student cannot take does to its trajectory."""

    #: The student does not offer ``browser.navigate``: no counterpart.
    MIDDLE = Action(name="browser.navigate", params={"url": "https://shop.example.test/cart"})

    def test_drop_step_segments(self, composer: ConfigComposer, tmp_path: Path) -> None:
        renderer = _renderer(composer, untranslatable="drop_step")
        rendered = renderer.render(_trajectory(_three_steps(tmp_path, self.MIDDLE)))
        assert [(r.sample.step, r.segment) for r in rendered] == [(0, 0), (2, 1)]
        assert renderer.stats.untranslatable == {"no_counterpart": 1}

    def test_history_after_dropped_middle_step(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        """The step after a dropped one starts over: re-seeded, nothing before it."""
        renderer = _renderer(composer, untranslatable="drop_step")
        _first, last = renderer.render(_trajectory(_three_steps(tmp_path, self.MIDDLE)))
        assert last.context_steps == ()
        assert [type(m).__name__ for m in last.context] == ["SystemMessage", "UserMessage"]
        assert last.context[0].text == renderer.runtime.seed_messages(last.sample.task)[0].text

    def test_truncate(self, composer: ConfigComposer, tmp_path: Path) -> None:
        renderer = _renderer(composer, untranslatable="truncate")
        rendered = renderer.render(_trajectory(_three_steps(tmp_path, self.MIDDLE)))
        assert [r.sample.step for r in rendered] == [0]
        assert renderer.stats.untranslatable == {"no_counterpart": 1}
        assert renderer.stats.skipped == {"truncated": 1}

    def test_keep_as_text(self, composer: ConfigComposer, tmp_path: Path) -> None:
        renderer = _renderer(composer, untranslatable="keep_as_text")
        first, last = renderer.render(_trajectory(_three_steps(tmp_path, self.MIDDLE)))
        assert (first.sample.step, last.sample.step) == (0, 2)
        assert last.segment == 0 and last.context_steps == (0, 1)
        kept = [m.text for m in last.context if isinstance(m, AssistantMessage)][1]
        assert kept == (
            '[not expressible: no_counterpart] [{"name":"browser.navigate",'
            '"params":{"url":"https://shop.example.test/cart"}}]'
        )

    def test_fail(self, composer: ConfigComposer, tmp_path: Path) -> None:
        renderer = _renderer(composer, untranslatable="fail")
        with pytest.raises(UntranslatableActionError) as excinfo:
            renderer.render(_trajectory(_three_steps(tmp_path, self.MIDDLE)))
        assert (excinfo.value.sample_id, excinfo.value.reason) == ("run-1/s1", "no_counterpart")

    def test_more_actions_than_the_students_budget(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path,
                "run-two",
                [ExportStep(actions=[click_at(1, 1), click_at(2, 2)], fragments=[screenshot()])],
            )
        )
        renderer = _renderer(composer)
        assert renderer.render(_trajectory(trace)) == []
        assert renderer.stats.untranslatable == {"exceeds_action_budget": 1}

    def test_a_param_the_student_cannot_write(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        scroll = Action(name="browser.scroll", params={"direction": "down", "amount": 300})
        renderer = _renderer(composer)
        assert renderer.render(_trajectory(_three_steps(tmp_path, scroll)))[1].sample.step == 2
        assert renderer.stats.untranslatable == {"unrepresentable": 1}


class TestStepInfo:
    def _seen(self, monkeypatch: pytest.MonkeyPatch) -> list[StepInfoObs | None]:
        seen: list[StepInfoObs | None] = []
        original = UITarsAgent.build_react_prompt

        def spy(self: UITarsAgent, observation: Any) -> Any:
            seen.append(getattr(observation, "step_info", None))
            return original(self, observation)

        monkeypatch.setattr(UITarsAgent, "build_react_prompt", spy)
        return seen

    def test_the_recorded_step_info_is_pinned(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._seen(monkeypatch)
        renderer = _renderer(composer)
        rendered = renderer.render(_trajectory(_three_steps(tmp_path)))
        assert [(i.step, i.max_steps, i.today) for i in seen if i] == [
            (1, 30, "2026-10-11"),
            (2, 30, "2026-10-11"),
            (3, 30, "2026-10-11"),
        ]
        assert {r.step_info for r in rendered} == {"recorded"}

    def test_today_override_wins_and_the_fallback_is_the_manifest_date(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._seen(monkeypatch)
        root = student_root()
        root["agent"][0]["agent_loop"]["today_override"] = "2030-01-02"
        _renderer(composer, root=root).render(_trajectory(_three_steps(tmp_path / "a")))
        assert {(i.max_steps, i.today) for i in seen if i} == {(30, "2030-01-02")}

        seen.clear()
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path / "b",
                "run-old",
                [
                    ExportStep(
                        actions=[click_at(5, 5)], fragments=[screenshot()], loop_fragments=False
                    )
                ],
            )
        )
        renderer = _renderer(composer)
        (item,) = renderer.render(_trajectory(trace))
        created = trace.manifest.created_at.date().isoformat()
        assert [(i.step, i.max_steps, i.today) for i in seen if i] == [(1, 30, created)]
        assert item.step_info == "fallback"
        assert renderer.stats.step_info == {"fallback": 1}


class TestThought:
    def test_none_writes_no_thought(self, composer: ConfigComposer, tmp_path: Path) -> None:
        trace = _three_steps(tmp_path)
        rendered = _renderer(composer, thought_source="none").render(
            _trajectory(trace, thought_source="none")
        )
        assert all("Thought:" not in r.target for r in rendered)
        assert all(r.thought is None for r in rendered)

    def test_index_references_are_dropped_when_the_student_cannot_see_them(
        self, composer: ConfigComposer, tmp_path: Path
    ) -> None:
        dom_teacher = UI_TARS_NEGOTIATION.model_copy(
            update={"active_actions": [*UI_TARS_NEGOTIATION.active_actions, "browser.click"]}
        )
        trace = RunTrace(
            write_ui_tars_run(
                tmp_path,
                "run-idx",
                [
                    ui_tars_step(click_at(10, 10), thought="click [12] the cart"),
                    ui_tars_step(click_at(20, 20), thought="then the button"),
                ],
                negotiation=dom_teacher,
            )
        )
        renderer = _renderer(composer)
        first, second = renderer.render(_trajectory(trace))
        assert first.thought is None and first.thought_dropped
        assert "Thought:" not in first.target
        assert second.thought is not None and second.thought.text == "then the button"
        assert renderer.stats.thoughts_dropped == 1
        assert renderer.stats.thought_fields == {"Thought": 1}
        kept = _renderer(composer, thought_index_refs="keep").render(_trajectory(trace))
        assert kept[0].target.startswith("Thought: click [12] the cart")


class TestRoundTrip:
    def test_a_formatter_that_is_not_a_fixed_point_is_a_round_trip_error(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = UITarsAgent.format_react_output

        def off_by_twenty(self: UITarsAgent, actions: Any, *, thought: Any = None) -> str:
            return original(self, _shifted(actions, lambda x: x + 20), thought=thought)

        monkeypatch.setattr(UITarsAgent, "format_react_output", off_by_twenty)
        with pytest.raises(RoundTripError, match=r"run-1/s0: .*no fixed point"):
            _renderer(composer).render(_trajectory(_three_steps(tmp_path)))

    def test_a_formatter_that_moves_a_point_past_the_quantum_is_a_round_trip_error(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapping to 100 px is a fixed point, so only the quantum clause can catch it."""
        original = UITarsAgent.format_react_output

        def snapped(self: UITarsAgent, actions: Any, *, thought: Any = None) -> str:
            return original(self, _shifted(actions, lambda x: x // 100 * 100), thought=thought)

        monkeypatch.setattr(UITarsAgent, "format_react_output", snapped)
        with pytest.raises(
            RoundTripError, match=r"run-1/s0: .*moved 40 px, more than the grid's quantum 1"
        ):
            _renderer(composer).render(_trajectory(_three_steps(tmp_path)))

    def test_a_formatter_that_drops_a_param_is_a_round_trip_error(
        self, composer: ConfigComposer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = UITarsAgent.format_react_output

        def forgets_the_answer(self: UITarsAgent, actions: Any, *, thought: Any = None) -> str:
            stripped = [
                a.model_copy(update={"params": {**a.params, "extracted_content": None}})
                if a.name == "browser.done"
                else a
                for a in actions
            ]
            return original(self, stripped, thought=thought)

        monkeypatch.setattr(UITarsAgent, "format_react_output", forgets_the_answer)
        with pytest.raises(RoundTripError, match="extracted_content"):
            _renderer(composer).render(_trajectory(_three_steps(tmp_path)))


def _shifted(actions: Any, move: Any) -> list[Action]:
    """*actions* with every ``coordinate_x`` moved by *move*."""
    return [
        a.model_copy(
            update={"params": {**a.params, "coordinate_x": move(a.params["coordinate_x"])}}
        )
        if "coordinate_x" in a.params
        else a
        for a in actions
    ]
