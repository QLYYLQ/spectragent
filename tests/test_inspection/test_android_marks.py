"""Android actions as plain lines and as marks on the step's screenshot, through the shared marks."""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.capabilities.android.actions import ACTIONS
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.effects import (
    AndroidTextEffect,
    AndroidTouchEffect,
    ScreenPoint,
    Touch,
)
from spectragent.core.capabilities.android.observations import (
    AndroidScreenshotObs,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.ui_tree.models import UITreeElement
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.graph import run_graph
from spectragent.inspection.marks import (
    ACTION_VERBS,
    action_lines,
    action_text,
    addressed_elements,
    observation_marks,
)
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.step_record import StepRecord
from tests._helpers.fragment_samples import PNG_BYTES
from tests._helpers.trace_builder import fragment

#: The screen is 1080 x 2400 physical px; the screenshot was stored at half that.
IMAGE = (540, 1200)


def _node(unique_id: int, cls: str, box: tuple[int, int, int, int], **text: str) -> dict:
    left, top, right, bottom = box
    return {
        "unique_id": unique_id,
        "bounds_in_screen": {"left": left, "top": top, "right": right, "bottom": bottom},
        "class_name": f"android.widget.{cls}",
        "window_id": 7,
        "is_visible_to_user": True,
        **text,
    }


def _tree() -> AndroidUITreeObs:
    forest = AndroidAccessibilityForest.model_validate(
        {
            "windows": [
                {
                    "bounds_in_screen": {"right": 1080, "bottom": 2400},
                    "id": 7,
                    "tree": {
                        "nodes": [
                            _node(1, "Button", (800, 100, 1000, 300), text="Save"),
                            _node(2, "EditText", (0, 400, 1080, 600), hint_text="First name"),
                            _node(3, "RecyclerView", (0, 700, 1080, 2300)),
                        ]
                    },
                }
            ]
        }
    )
    return AndroidUITreeObs(
        forest=forest,
        elements=tuple(
            UITreeElement(index=index, window_id=7, unique_id=node)
            for index, node in ((0, 1), (1, 2), (4, 3))
        ),
    )


def _touch(gesture: str, start: tuple[int, int], end: tuple[int, int], ms: int = 50) -> Touch:
    return Touch(
        gesture=gesture,  # type: ignore[arg-type]
        start=ScreenPoint(x=start[0], y=start[1]),
        end=ScreenPoint(x=end[0], y=end[1]),
        duration_ms=ms,
        elapsed_ms=ms,
        delivered_by="shell_injection",
    )


def _touched(name: str, touch: Touch) -> ActionResult:
    return ActionResult(success=True, effect=AndroidTouchEffect(effect_id=name, touch=touch))


@pytest.fixture
def run(tmp_path: Path) -> RunTrace:
    wal = WALWriter(tmp_path, "android")
    wal.init_run(TraceConfig(agent_id="m3a", loop_id="react"))
    shot = AndroidScreenshotObs(
        image_bytes=PNG_BYTES,
        image_width=IMAGE[0],
        image_height=IMAGE[1],
        viewport_width=1080,
        viewport_height=2400,
    )
    typed = AndroidTextEffect(
        effect_id="android.type",
        tap=_touch("tap", (540, 500), (540, 500)),
        text_entered=True,
        entered_by="key_events",
        replaced=False,
    )
    record = StepRecord(
        step=0,
        actions=[
            Action(name="android.click", params={"index": 0}),
            Action(name="android.scroll", params={"direction": "down", "index": 4}),
            Action(name="android.type", params={"index": 1, "text": "Emilia"}),
            Action(name="android.press_key", params={"key": "back"}),
            Action(name="android.open_app", params={"app_name": "Contacts"}),
            Action(name="android.long_press", params={"coordinate_x": 100, "coordinate_y": 200}),
            Action(
                name="android.drag",
                params={"start_x": 10, "start_y": 20, "end_x": 30, "end_y": 40},
            ),
        ],
        action_results=[
            _touched("android.click", _touch("tap", (900, 200), (900, 200))),
            _touched("android.scroll", _touch("swipe", (540, 1500), (540, 700))),
            ActionResult(success=True, effect=typed),
            ActionResult(success=True),
            ActionResult(success=False, error="no app 'Contacts' is installed"),
            _touched("android.long_press", _touch("long_press", (100, 200), (100, 200), 1000)),
            ActionResult(success=True),
        ],
    )
    wal.commit_step(0, record, fragments=[fragment(0, shot), fragment(0, _tree())])
    wal.commit_step(1, StepRecord(step=1, origin="terminal"), fragments=[fragment(1, shot)])
    return RunTrace(wal.run_dir)


def test_every_android_action_has_a_verb() -> None:
    assert {action.__registry_key__ for action in ACTIONS} <= set(ACTION_VERBS)


def test_android_lines_name_the_element_the_step_recorded(run: RunTrace) -> None:
    record = run.step(0)
    assert record is not None
    lines = action_lines(record, addressed_elements(run, record)).splitlines()
    assert lines == [
        "1. 点击 (900, 200) → #0 按钮“Save”",
        "2. 向下滚动 → #4 列表，手指从 (540, 1500) 滑到 (540, 700)",
        "3. 输入“Emilia” → #1 输入框“First name”",
        "4. 按键 返回",
        "5. 打开应用 Contacts（没有成功）",
        "6. 长按 (100, 200)，按住 1 秒",
        "7. 拖动 (10, 20) → (30, 40)",
    ]
    # without the step's tree an element is named by its index alone
    assert action_text(record.actions[0], record.action_results[0]) == "点击 (900, 200) → #0"


def test_android_marks_are_in_image_pixels_with_the_element_bounds(run: RunTrace) -> None:
    found = observation_marks(run, "r", 0)
    assert (found.width, found.height, found.problem) == (*IMAGE, None)
    click, scroll, typed, key, app, press, drag = found.sets[0].marks
    # the screen's physical px at half scale; the element's recorded bounds as a box
    assert (click.kind, click.x, click.y, click.measured) == ("click", 450, 100, True)
    assert click.box == (400, 50, 500, 150)
    assert (scroll.kind, scroll.x, scroll.y, scroll.end_x, scroll.end_y) == (
        "scroll",
        270,
        750,
        270,
        350,
    )
    assert (scroll.direction, scroll.box) == ("down", (0, 350, 540, 1150))
    assert (typed.kind, typed.x, typed.y, typed.typed) == ("type", 270, 250, "Emilia")
    assert typed.box == (0, 200, 540, 300)
    assert [mark.kind for mark in (key, app)] == [None, None]
    assert app.succeeded is False
    assert (press.kind, press.x, press.y, press.box) == ("click", 50, 100, None)
    assert (drag.kind, drag.x, drag.y, drag.end_x, drag.end_y) == ("drag", 5, 10, 15, 20)


def test_the_run_graph_words_android_commits_with_their_elements(run: RunTrace) -> None:
    graph = run_graph(run)
    texts = [action["text"] for action in graph["steps"][0]["actions"]]
    assert texts[0] == "点击 (900, 200) → #0 按钮“Save”"
    assert texts[4] == "打开应用 Contacts（没有成功）"


def test_a_browser_index_click_never_reads_the_dom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a fragment this view can name an element from is read; a DOM can be tens of MB."""
    import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers browser.click

    wal = WALWriter(tmp_path, "browser")
    wal.init_run(TraceConfig())
    click = Action(name="browser.click", params={"index": 7})
    wal.commit_step(
        0, StepRecord(step=0, actions=[click], action_results=[ActionResult(success=True)])
    )
    trace = RunTrace(wal.run_dir)
    record = trace.step(0)
    assert record is not None

    def forbid(*args: object, **kwargs: object) -> None:
        pytest.fail("the DOM fragment must not be read")

    monkeypatch.setattr(RunTrace, "observation_fragment", forbid)
    (element,) = addressed_elements(trace, record)
    assert element is not None and element.text == "#7"


def test_a_click_that_recorded_no_touch_gets_no_point(run: RunTrace, tmp_path: Path) -> None:
    """Only its element's recorded bounds are drawn; no point is made up at their centre."""
    wal = WALWriter(tmp_path / "untouched", "untouched")
    wal.init_run(TraceConfig())
    shot = AndroidScreenshotObs(
        image_bytes=PNG_BYTES,
        image_width=540,
        image_height=1200,
        viewport_width=1080,
        viewport_height=2400,
    )
    refused = ActionResult(success=False, error="the element is not on the screen")
    record = StepRecord(
        step=0,
        actions=[Action(name="android.click", params={"index": 0})],
        action_results=[refused],
    )
    wal.commit_step(0, record, fragments=[fragment(0, shot), fragment(0, _tree())])
    (click,) = observation_marks(RunTrace(wal.run_dir), "r", 0).sets[0].marks
    assert (click.kind, click.x, click.y, click.box) == ("click", None, None, (400, 50, 500, 150))
    assert click.unplaced is not None and "虚线框" in click.unplaced
