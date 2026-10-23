"""Recorded actions as marks on an image, in that image's pixels, and as Chinese lines."""

from __future__ import annotations

from pathlib import Path

import pytest

import spectragent.core.capabilities.browser.actions  # noqa: F401 — action effects registered
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    PointerEffect,
    TextInputEffect,
    ViewportPoint,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.inspection.marks import (
    ACTION_VERBS,
    MarkRequest,
    action_lines,
    action_text,
    action_verb,
    default_requests,
    marks_on,
    observation_marks,
)
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.step_record import ActionSpan
from tests.test_lookahead.traces import (
    SIZE,
    StepSpec,
    click,
    done,
    frame,
    gradient,
    scroll_down,
    settled,
    wait,
    write_run,
)

IMAGE = gradient()
#: Screenshots are SIZE (128 x 96) pixels of a 64 x 48 CSS viewport: device scale 2.
VIEWPORT = (SIZE[0] // 2, SIZE[1] // 2)


def _drag() -> tuple[Action, ActionResult]:
    effect = PointerEffect(
        effect_id="browser.drag",
        point=ViewportPoint(x=10, y=12),
        release_point=ViewportPoint(x=40, y=30),
        hit=ElementFacts(role="slider", name="Price"),
        focus_after="nothing",
    )
    return (
        Action(name="browser.drag", params={"start_x": 9, "start_y": 12, "end_x": 41, "end_y": 30}),
        ActionResult(success=True, effect=effect),
    )


def _type_at(x: int, y: int, text: str, *, sensitive: bool = False) -> tuple[Action, ActionResult]:
    effect = TextInputEffect(
        effect_id="browser.type_coordinate",
        target=ElementFacts(role="searchbox", name="Search"),
        sensitive=sensitive,
        value_changed=True,
        text_entered=True,
        focus_after=ElementFacts(role="searchbox", name="Search"),
    )
    return (
        Action(
            name="browser.type_coordinate",
            params={"coordinate_x": x, "coordinate_y": y, "text": text},
        ),
        ActionResult(success=True, effect=effect, typed_into_sensitive_field=sensitive),
    )


def _refused() -> tuple[Action, ActionResult]:
    return (
        Action(name="hover", params={"x": 1}),
        ActionResult(success=False, error="Action 'hover' is not in the active action set."),
    )


def _measured_click() -> tuple[Action, ActionResult]:
    """A click whose measured point differs from its parameters."""
    action, result = click(30, 20, "button", "最多播放")
    return action.model_copy(update={"params": {"coordinate_x": 29, "coordinate_y": 21}}), result


SPANS = [
    ActionSpan(action=name, started_at=1.0 + i, ended_at=1.5 + i, outcome="returned")
    for i, name in enumerate(["browser.click_coordinate", "browser.scroll"])
]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> RunTrace:
    base = tmp_path_factory.mktemp("marks")
    path = write_run(
        base,
        "marks-run",
        task="在 B 站找播放量最高的视频",
        viewport=VIEWPORT,
        pointer=(6, 7),
        steps=[
            StepSpec(
                step=0,
                image=IMAGE,
                url="https://www.bilibili.com/",
                settle=settled(),
                # the scroll is dispatched where the pointer was: at the start
                actions=[scroll_down(), _type_at(20, 5, "贵州茅台"), _drag(), wait()],
            ),
            StepSpec(
                step=1,
                image=IMAGE,
                url="https://www.bilibili.com/search",
                settle=settled(),
                # the guard refused the hover: it never reached the environment
                actions=[_refused(), _measured_click(), scroll_down()],
                action_spans=SPANS,
                frames=[
                    frame(
                        "before_action",
                        3,
                        image=IMAGE,
                        action_index=1,
                        action="browser.scroll",
                        requested_at=2.0,
                    ),
                    frame("after_observation", 3, image=IMAGE, requested_at=1.0),
                ],
            ),
            StepSpec(
                step=2,
                image=IMAGE,
                url="https://www.bilibili.com/video",
                settle=settled(),
                actions=[_type_at(1, 1, "secret", sensitive=True), done("播放量 120 万")],
            ),
            StepSpec(step=3, origin="terminal", image=IMAGE, url="https://www.bilibili.com/"),
        ],
    )
    return RunTrace(path)


def test_marks_are_in_image_pixels_through_the_recorded_device_scale(run: RunTrace) -> None:
    found = observation_marks(run, "r", 0)
    assert (found.width, found.height, found.problem) == (SIZE[0], SIZE[1], None)
    (marks,) = found.sets
    scroll, typed, drag, waited = marks.marks
    # the pointer the observation recorded (6, 7) CSS px, at device scale 2
    assert (scroll.kind, scroll.x, scroll.y, scroll.direction) == ("scroll", 12, 14, "down")
    assert scroll.text == "向下滚动，页面移动了 400 像素"
    assert (typed.kind, typed.x, typed.y, typed.typed) == ("type", 40, 10, "贵州茅台")
    assert typed.text == "输入“贵州茅台” → 搜索框“Search”"
    # a drag's measured press and release, not its parameters
    assert (drag.kind, drag.x, drag.y, drag.end_x, drag.end_y) == ("drag", 20, 24, 80, 60)
    assert drag.measured and drag.text == "拖动 (10, 12) → (40, 30)，按在滑块“Price”上"
    assert (waited.kind, waited.x, waited.text) == (None, None, "等待 1 秒")
    assert [mark.number for mark in marks.marks] == [1, 2, 3, 4]


def test_a_measured_point_wins_and_a_scroll_follows_the_pointer(run: RunTrace) -> None:
    refused, clicked, scroll = observation_marks(run, "r", 1).sets[0].marks
    assert refused.kind is None and refused.succeeded is False
    assert refused.text.endswith("（没有成功）")
    assert (clicked.x, clicked.y, clicked.measured) == (60, 40, True)
    assert clicked.text == "点击 (30, 20) → 按钮“最多播放”"
    assert (scroll.x, scroll.y) == (60, 40)  # where the click left the pointer


def test_sensitive_text_is_withheld_and_done_reads_as_an_answer(run: RunTrace) -> None:
    typed, finished = observation_marks(run, "r", 2).sets[0].marks
    assert typed.typed is None and typed.text == "输入（敏感字段，内容不显示） → 搜索框“Search”"
    assert finished.kind is None and finished.text == "完成并回答：播放量 120 万"
    record = run.step(2)
    assert record is not None
    assert action_lines(record) == (
        "1. 输入（敏感字段，内容不显示） → 搜索框“Search”\n2. 完成并回答：播放量 120 万"
    )


def test_a_before_action_frame_shows_the_action_it_preceded(run: RunTrace) -> None:
    # the frame before env.act call 1: the scroll, the step's third recorded action
    geometry, requests = default_requests(run, "r", 1, "browser.frames", "frame-3-before-1.png")
    assert requests == [MarkRequest("r", 1, "这张画面之后马上执行的动作", only=frozenset({2}))]
    found = marks_on(geometry, requests, lambda key: run)
    assert [(mark.number, mark.kind) for mark in found.sets[0].marks] == [(3, "scroll")]
    assert (found.width, found.height) == SIZE
    # a delayed frame shows nothing by default
    _, none = default_requests(run, "r", 1, "browser.frames", "frame-3-after.png")
    assert none == []


def test_marks_of_another_step_use_the_image_geometry(run: RunTrace) -> None:
    geometry, _ = default_requests(run, "r", 0, None, None)
    found = marks_on(geometry, [MarkRequest("r", 0, "原运行"), MarkRequest("r", 1)], lambda k: run)
    assert [mark_set.label for mark_set in found.sets] == ["原运行", ""]
    assert observation_marks(run, "r", 0).sets[0].label == "第 0 步在这一屏上做的动作"
    assert found.sets[1].marks[1].x == 60


def test_an_image_without_a_viewport_lists_actions_and_says_why(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        "no-viewport",
        task="t",
        viewport=(0, 0),
        pointer=None,
        steps=[StepSpec(step=0, image=IMAGE, settle=settled(), actions=[click(3, 4, "link", "")])],
    )
    found = observation_marks(RunTrace(path), "x", 0)
    assert found.problem is not None and "视口" in found.problem
    (mark,) = found.sets[0].marks
    assert (mark.kind, mark.x, mark.unplaced) == ("click", None, found.problem)
    assert mark.text == "点击 (3, 4) → 链接"


def test_a_missing_step_is_a_set_problem(run: RunTrace) -> None:
    geometry, _ = default_requests(run, "r", 0, None, None)
    found = marks_on(geometry, [MarkRequest("r", 9)], lambda key: run)
    assert found.sets[0].problem == "这次运行没有已提交的第 9 步"


@pytest.mark.parametrize("name", sorted(set(ACTION_VERBS) - {"browser.done"}))
def test_each_line_says_the_verb_the_viewer_labels_its_step_with(name: str) -> None:
    # One wording: the line and the step label (the run API's action_verbs) agree.
    assert action_verb(name) in action_text(Action(name=name), None)


def test_an_action_without_a_verb_keeps_its_recorded_name() -> None:
    assert action_verb("desktop.click") == "desktop.click"
    assert action_text(Action(name="desktop.click", params={"x": 1}), None) == (
        'desktop.click {"x": 1}'
    )
