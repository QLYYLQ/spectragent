"""Android's checkpoint restoration: re-finding recorded elements, its evidence, pinning the clock.

``docs/architecture_design.md`` §4.9 (``RestorationSupport``, ``RestorationAlignment``,
restoration evidence per capability) and §11.2 "分支回放": an element index is
re-found by bounds, class, resource id, text and description, everything else
runs as recorded; the evidence measures the app in front, the device clocks,
the UI tree without the status bar, the keyboard and the input focus, and the
screenshot with the status bar painted out, and judges itself against the
limits the capability's knobs set; each restored capture pins the device clock
to the recorded one.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw

from spectragent.core.capabilities.android.actions import ACTIONS
from spectragent.core.capabilities.android.bridge import ANIMATION_SCALES
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
    AndroidAccessibilityTree,
    AndroidAccessibilityWindowInfo,
    ProtoRect,
)
from spectragent.core.capabilities.android.observations import (
    SCREENSHOT,
    UI_TREE,
    AndroidScreenshotObs,
    AndroidUITreeObs,
    DeviceAtCapture,
)
from spectragent.core.capabilities.android.restoration import (
    GEOMETRY_SIGNATURE,
    measure,
    retarget_action,
    screenshot_distance,
)
from spectragent.core.capabilities.android.restoration_evidence import (
    AndroidRestorationEvidence,
    ClockAlignment,
    InputState,
)
from spectragent.core.capabilities.android.ui_tree.models import UITreeElement
from spectragent.core.loops.restore.checks import judge
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.step_record import StepRecord
from tests._helpers.android_device import (
    ScriptedPhone,
    android_capability,
    configured,
    fake_service_apk,
    phone_server,
)
from tests._helpers.trace_builder import fragment, new_wal

ACTIONS_BY_NAME = {action.__registry_key__: action() for action in ACTIONS}
STATUS_BAR = (0, 0, 1080, 128)
CLOCK = 1_697_384_040_000

# ── building screens ──────────────────────────────────────────────────

#: (class, resource id, text, description, bounds); a row's extra fields go in a dict.
Row = tuple[str, str, str, str, tuple[int, int, int, int]]


def node(unique_id: int, row: Row, window: int, **extra: Any) -> AndroidAccessibilityNodeInfo:
    class_name, view_id, text, description, (left, top, right, bottom) = row
    return AndroidAccessibilityNodeInfo(
        unique_id=unique_id,
        class_name=class_name,
        view_id_resource_name=view_id,
        text=text,
        content_description=description,
        bounds_in_screen=ProtoRect(left=left, top=top, right=right, bottom=bottom),
        window_id=window,
        **extra,
    )


def window(
    window_id: int,
    nodes: list[AndroidAccessibilityNodeInfo],
    *,
    kind: str = "TYPE_APPLICATION",
    bounds: tuple[int, int, int, int] = (0, 0, 1080, 2400),
) -> AndroidAccessibilityWindowInfo:
    left, top, right, bottom = bounds
    return AndroidAccessibilityWindowInfo.model_validate(
        {
            "id": window_id,
            "window_type": kind,
            "bounds_in_screen": {"left": left, "top": top, "right": right, "bottom": bottom},
            "tree": AndroidAccessibilityTree(nodes=tuple(nodes)),
        }
    )


SETTINGS: list[Row] = [
    ("android.widget.TextView", "android:id/title", "Network & internet", "", (0, 300, 1080, 450)),
    ("android.widget.TextView", "android:id/title", "Display", "", (0, 450, 1080, 600)),
    ("android.widget.TextView", "android:id/title", "Sound", "", (0, 600, 1080, 750)),
]


def screen(
    rows: list[Row],
    *,
    clock: str = "15:34",
    keyboard: bool = False,
    focused: int | None = None,
    checked: int | None = None,
) -> AndroidUITreeObs:
    """A cleaned tree: the app's *rows*, numbered from 0, then the status bar's clock."""
    app = [
        node(i + 1, row, 7, is_focused=i == focused, is_checked=i == checked)
        for i, row in enumerate(rows)
    ]
    bar = [
        node(
            1,
            (
                "android.widget.TextView",
                "com.android.systemui:id/clock",
                clock,
                clock,
                (50, 1, 148, 128),
            ),
            6,
        )
    ]
    windows = [window(7, app), window(6, bar, kind="TYPE_SYSTEM", bounds=STATUS_BAR)]
    if keyboard:
        key = node(1, ("android.widget.TextView", "", "q", "", (0, 1600, 108, 1750)), 11)
        windows.insert(1, window(11, [key], kind="TYPE_INPUT_METHOD", bounds=(0, 1517, 1080, 2400)))
    elements = [UITreeElement(index=i, window_id=7, unique_id=i + 1) for i in range(len(rows))]
    elements.append(UITreeElement(index=len(rows), window_id=6, unique_id=1))
    return AndroidUITreeObs(
        forest=AndroidAccessibilityForest(windows=tuple(windows)), elements=tuple(elements)
    )


def picture(*, clock_shade: int = 0, body_shade: int = 0) -> bytes:
    """A 1080x2400 screenshot: a patterned body, a black status bar with its clock in *clock_shade*."""
    image = Image.new("L", (1080, 2400))
    draw = ImageDraw.Draw(image)
    for row in range(8):
        for column in range(9):
            shade = (row * 37 + column * 53 + body_shade) % 256
            draw.rectangle(
                (column * 120, row * 300, column * 120 + 119, row * 300 + 299), fill=shade
            )
    draw.rectangle(STATUS_BAR, fill=0)
    draw.rectangle((50, 1, 400, 127), fill=clock_shade)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def device(
    clock_ms: int = CLOCK,
    package: str = "com.android.settings",
    activity: str = "com.android.settings.Settings",
) -> DeviceAtCapture:
    return DeviceAtCapture(
        clock_ms=clock_ms, package=package, activity=activity, window=f"{package}/{activity}"
    )


def observation(
    tree: AndroidUITreeObs | None = None,
    *,
    image: bytes | None = None,
    record: DeviceAtCapture | None = None,
) -> Any:
    """A ``NegotiatedObservation`` stand-in: the fragments under their local names, one capture's record on each."""
    shot = AndroidScreenshotObs(image_bytes=image, viewport_width=1080, viewport_height=2400)
    if tree is not None:
        tree = tree.model_copy(update={"device": record})
    return SimpleNamespace(screenshot=shot.model_copy(update={"device": record}), ui_tree=tree)


def evidence(recorded: Any, restored: Any, **knobs: Any) -> AndroidRestorationEvidence:
    limits: dict[str, Any] = {"screenshot_max_distance": 5, "clock_max_difference_s": 2.0}
    return measure(
        recorded, restored, alignment=knobs.pop("alignment", None), **{**limits, **knobs}
    )


def through_trace(item: Any) -> Any:
    """*item* as the restorer reads it from the source trace."""
    encoded = encode_fragment(0, item)
    assert encoded is not None
    artifact = encoded.artifact.data if encoded.artifact is not None else None
    return decode_fragment(encoded.key, type(item), json.loads(encoded.file.data), artifact)


def test_the_device_record_rides_through_the_trace_and_is_left_out_when_absent() -> None:
    shot = AndroidScreenshotObs(image_bytes=picture(), viewport_width=1080, viewport_height=2400)
    recorded = through_trace(shot.model_copy(update={"device": device()}))
    assert recorded.device == device()
    assert through_trace(shot).device is None
    assert "device" not in json.loads(encode_fragment(0, shot).file.data)  # type: ignore[union-attr]


# ── retarget ──────────────────────────────────────────────────────────


def retarget(action: Action, recorded: Any, restored: Any, tolerance_px: int = 2) -> Any:
    return retarget_action(
        action, recorded, restored, actions=ACTIONS_BY_NAME, tolerance_px=tolerance_px
    )


def test_an_element_index_is_refound_where_the_restored_screen_numbers_it_otherwise() -> None:
    recorded = observation(screen(SETTINGS))
    # The same screen with a row more before Display: Display is element 2 here, not 1.
    extra: Row = ("android.widget.TextView", "android:id/title", "Apps", "", (0, 150, 1080, 300))
    restored = observation(screen([extra, *SETTINGS]))

    result = retarget(Action(name="android.click", params={"index": 1}), recorded, restored)

    assert result.method == GEOMETRY_SIGNATURE and result.failure is None
    assert result.action is not None and result.action.params == {"index": 2}
    assert "element 1 -> 2" in result.detail and "'Display'" in result.detail


@pytest.mark.parametrize(
    "action",
    [
        Action(name="android.long_press", params={"index": 1}),
        Action(name="android.type", params={"index": 1, "text": "hi"}),
        Action(name="android.scroll", params={"direction": "down", "index": 1}),
    ],
    ids=lambda action: action.name,
)
def test_every_action_that_names_an_element_is_refound(action: Action) -> None:
    recorded, restored = observation(screen(SETTINGS)), observation(screen(SETTINGS[1:]))
    result = retarget(action, recorded, restored)
    assert result.action is not None and result.action.params["index"] == 0
    assert {k: v for k, v in result.action.params.items() if k != "index"} == {
        k: v for k, v in action.params.items() if k != "index"
    }


def test_an_element_that_moved_or_changed_is_not_found() -> None:
    recorded = observation(screen(SETTINGS))
    moved = [
        SETTINGS[0],
        ("android.widget.TextView", "android:id/title", "Display", "", (0, 460, 1080, 610)),
        SETTINGS[2],
    ]
    renamed = [
        SETTINGS[0],
        ("android.widget.TextView", "android:id/title", "Display & touch", "", (0, 450, 1080, 600)),
        SETTINGS[2],
    ]
    click = Action(name="android.click", params={"index": 1})

    for restored in (moved, renamed):
        result = retarget(click, recorded, observation(screen(restored)))
        assert (result.action, result.failure) == (None, "not_found")
    # Where the element went says what a swipe's inertia did to the screen.
    assert (
        "the nearest element that has them lies 10 px off"
        in retarget(click, recorded, observation(screen(moved))).detail
    )
    assert "no element has them" in retarget(click, recorded, observation(screen(renamed))).detail
    assert retarget(click, recorded, observation(screen(moved)), tolerance_px=10).failure is None


def test_two_elements_alike_in_the_same_place_are_ambiguous() -> None:
    twin = SETTINGS[1]
    result = retarget(
        Action(name="android.click", params={"index": 1}),
        observation(screen(SETTINGS)),
        observation(screen([SETTINGS[0], twin, twin])),
    )
    assert (result.action, result.failure) == (None, "ambiguous")


def test_an_index_with_no_tree_or_no_recorded_element_is_not_found() -> None:
    click = Action(name="android.click", params={"index": 9})
    assert (
        retarget(click, observation(screen(SETTINGS)), observation(screen(SETTINGS))).failure
        == "not_found"
    )
    no_tree = retarget(
        Action(name="android.click", params={"index": 0}),
        observation(screen(SETTINGS)),
        observation(),
    )
    assert (
        no_tree.failure == "not_found"
        and "restored observation has no android.ui_tree" in no_tree.detail
    )


@pytest.mark.parametrize(
    "action",
    [
        Action(name="android.click_coordinate", params={"x": 5, "y": 6}),
        Action(name="android.scroll", params={"direction": "down"}),
        Action(name="android.press_key", params={"key": "back"}),
        Action(name="android.open_app", params={"app_name": "Settings"}),
        Action(name="android.wait", params={}),
        Action(name="loop.done", params={"answer": "x"}),
    ],
    ids=lambda action: action.name,
)
def test_everything_else_runs_as_recorded(action: Action) -> None:
    result = retarget(action, observation(screen(SETTINGS)), observation())
    assert result.method == "identity" and result.action == action and result.action is not action


# ── the evidence ──────────────────────────────────────────────────────


def test_the_capability_names_this_type_and_its_checks() -> None:
    assert AndroidCapability.RestorationEvidence is AndroidRestorationEvidence
    assert AndroidRestorationEvidence.REASONS == (
        "foreground_mismatch",
        "clock_difference",
        "ui_tree_mismatch",
        "keyboard_focus_mismatch",
        "screenshot_distance",
    )


def test_the_same_screen_passes_whatever_the_status_bar_shows() -> None:
    recorded = observation(screen(SETTINGS), image=picture(), record=device())
    restored = observation(
        screen(SETTINGS, clock="15:41"), image=picture(clock_shade=255), record=device(CLOCK + 400)
    )
    found = evidence(recorded, restored)

    assert found.failed_checks() == []
    assert (found.foreground_match, found.ui_tree_match, found.clock_difference_ms) == (
        True,
        True,
        400,
    )
    assert found.screenshot_distance == 0 and found.status_bar == STATUS_BAR
    assert found.recorded_input == found.restored_input == InputState(keyboard_shown=False)


def test_without_the_status_bar_painted_out_its_pixels_would_count() -> None:
    def distance(bar: tuple[int, int, int, int] | None) -> int | None:
        return screenshot_distance(
            observation(image=picture()), observation(image=picture(clock_shade=255)), bar
        )

    assert distance(STATUS_BAR) == 0
    assert (distance(None) or 0) > 0


def test_another_app_in_front_explains_the_rest() -> None:
    recorded = observation(screen(SETTINGS), image=picture(), record=device())
    restored = observation(
        screen(SETTINGS[:1]),
        image=picture(body_shade=90),
        record=device(
            package="com.google.android.deskclock", activity="com.android.deskclock.DeskClock"
        ),
    )
    found = evidence(recorded, restored)

    assert found.failed_checks() == [
        "foreground_mismatch",
        "ui_tree_mismatch",
        "screenshot_distance",
    ]
    assert found.restored_package == "com.google.android.deskclock"
    assert (found.ui_tree_missing, found.ui_tree_extra) == (2, 0)
    assert found.ui_tree_differences == [
        "missing: TextView #android:id/title 'Display'",
        "missing: TextView #android:id/title 'Sound'",
    ]


def test_a_time_on_screen_that_ran_on_is_named() -> None:
    """A stopwatch runs on its own clock: the pinned wall clock cannot put it back."""

    def stopwatch(time: str) -> list[Row]:
        return [
            (
                "android.widget.TextView",
                "com.google.android.deskclock:id/stopwatch_time_text",
                time,
                "",
                (100, 800, 980, 1000),
            )
        ]

    found = evidence(
        observation(screen(stopwatch("00:05")), record=device()),
        observation(screen(stopwatch("00:41")), record=device(CLOCK + 300)),
    )
    assert found.failed_checks() == ["ui_tree_mismatch"]
    assert found.ui_tree_differences == [
        "missing: TextView #com.google.android.deskclock:id/stopwatch_time_text '00:05'",
        "extra: TextView #com.google.android.deskclock:id/stopwatch_time_text '00:41'",
    ]


def test_a_switch_left_in_another_state_differs() -> None:
    found = evidence(observation(screen(SETTINGS, checked=1)), observation(screen(SETTINGS)))
    assert found.failed_checks() == ["ui_tree_mismatch"]
    assert found.ui_tree_differences[0] == "missing: TextView #android:id/title 'Display' checked"


def test_the_keyboard_and_the_input_focus() -> None:
    recorded = observation(screen(SETTINGS, keyboard=True, focused=0))
    assert (
        evidence(recorded, observation(screen(SETTINGS, keyboard=True, focused=0))).failed_checks()
        == []
    )
    hidden = evidence(recorded, observation(screen(SETTINGS, focused=0)))
    assert "keyboard_focus_mismatch" in hidden.failed_checks()
    assert hidden.recorded_input == InputState(
        keyboard_shown=True, focused="TextView #android:id/title at [0,300][1080,450]"
    )
    elsewhere = evidence(recorded, observation(screen(SETTINGS, keyboard=True, focused=2)))
    assert elsewhere.failed_checks() == ["keyboard_focus_mismatch"]


def test_the_clock_limit_is_the_evidences_own_and_inclusive() -> None:
    recorded = observation(record=device())
    assert evidence(recorded, observation(record=device(CLOCK + 2000))).failed_checks() == []
    late = evidence(recorded, observation(record=device(CLOCK + 2001)))
    assert late.failed_checks() == ["clock_difference"] and late.clock_max_difference_s == 2.0
    assert (
        evidence(
            recorded, observation(record=device(CLOCK - 9000)), clock_max_difference_s=10
        ).failed_checks()
        == []
    )


def test_a_measure_not_taken_decides_nothing() -> None:
    """A recording from before the device record, a run without a tree or a screenshot."""
    found = evidence(observation(), observation(record=device()))
    assert found.failed_checks() == []
    assert (found.foreground_match, found.clock_difference_ms, found.ui_tree_match) == (
        None,
        None,
        None,
    )
    assert found.screenshot_distance is None and found.recorded_input is None


def test_the_restorer_judges_android_evidence_and_round_trips_it_through_the_trace(
    tmp_path: Path,
) -> None:
    found = evidence(
        observation(screen(SETTINGS), record=device()),
        observation(screen(SETTINGS[:2]), record=device(CLOCK + 5000)),
        alignment=ClockAlignment(outcome="failed", target_ms=CLOCK, detail="RuntimeError: no"),
    )
    check = judge(3, [found], [], verify="warn")
    assert (check.verdict, check.reasons) == ("warn", ["clock_difference", "ui_tree_mismatch"])

    record = StepRecord(step=0, origin="restored", restoration=check)
    wal = new_wal(tmp_path)
    wal.commit_step(0, record, fragments=[fragment(0, HistoryObs(items=[record]))])
    read = RunTrace(wal.run_dir).step(0)
    assert read is not None and read.restoration is not None and read.restoration == check
    assert isinstance(read.restoration.evidence[0], AndroidRestorationEvidence)
    written = json.loads((wal.run_dir / "steps" / "000.json").read_text())
    assert written["restoration"]["evidence"][0]["clock_alignment"]["outcome"] == "failed"


# ── pinning the clock, on the scripted phone ──────────────────────────


async def test_a_restored_capture_pins_the_clock_to_the_recorded_one() -> None:
    # The phone runs an hour after the recording, in the recorded app.
    phone = ScriptedPhone(clock_ms=CLOCK + 3_600_000, focused="com.android.settings/.Settings")
    recorded = observation(record=device())
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={SCREENSHOT, UI_TREE}) as capability,
    ):
        await capability.align_next_observation(recorded)
        assert phone.clock_sets == [], "only the request is kept"
        await capability.begin_observation(1)
        restored = SimpleNamespace(
            screenshot=await capability.resolve_fragment(SCREENSHOT),
            ui_tree=await capability.resolve_fragment(UI_TREE),
        )
        found = await capability.verify_restoration(recorded, restored)

        # A later capture never pins: the request belonged to that one.
        await capability.begin_observation(2)
        await capability.resolve_fragment(SCREENSHOT)
        again = await capability.verify_restoration(recorded, restored)

    assert phone.clock_sets == [CLOCK]
    assert found.clock_alignment == ClockAlignment(outcome="aligned", target_ms=CLOCK)
    assert found.clock_difference_ms is not None and 0 <= found.clock_difference_ms < 2000
    assert found.failed_checks() == []
    assert again.clock_alignment is None


async def test_with_the_wait_on_the_clock_is_pinned_on_a_still_screen_that_then_settles_again() -> (
    None
):
    """What shows the time is drawn anew after the pinning, before the sources are asked."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb, fragments={SCREENSHOT}, settle_max_wait_s=5, settle_quiet_ms=100
        ) as capability,
    ):
        await capability.align_next_observation(observation(record=device()))
        await capability.begin_observation(1)
        shot = await capability.resolve_fragment(SCREENSHOT)

    # Pinned, a quiet period waited for, pinned again: the wait's own time is taken back.
    assert phone.clock_sets == [CLOCK, CLOCK]
    assert isinstance(shot, AndroidScreenshotObs) and shot.device is not None
    assert 0 <= shot.device.clock_ms - CLOCK < 100
    assert shot.settle is not None and shot.settle.outcome == "settled"


async def test_a_pinning_that_cannot_be_done_is_reported() -> None:
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={SCREENSHOT}) as capability,
    ):
        # The recording has no device clock (recorded before the record existed).
        await capability.align_next_observation(observation())
        await capability.begin_observation(1)
        await capability.resolve_fragment(SCREENSHOT)
        unknown = await capability.verify_restoration(observation(), observation())
        # A request no capture reached: the observation failed before it.
        await capability.align_next_observation(observation(record=device()))
        lost = await capability.verify_restoration(observation(), observation())

    assert phone.clock_sets == []
    assert unknown.clock_alignment is not None and unknown.clock_alignment.outcome == "unknown"
    assert lost.clock_alignment == ClockAlignment(
        outcome="failed",
        target_ms=CLOCK,
        detail="no capture reached the pinning: the restored observation failed first",
    )


@pytest.mark.parametrize(("scale", "written"), [(0.0, "0"), (None, None)])
async def test_the_animation_scales_are_set_at_setup_when_configured(
    tmp_path: Path, scale: float | None, written: str | None
) -> None:
    phone = ScriptedPhone()
    async with phone_server(phone) as adb:
        capability = AndroidCapability(
            service_apk=str(fake_service_apk(tmp_path)), animation_scale=scale
        )
        configured(capability, adb)
        await capability.setup()
        await capability.teardown()
    expected = dict.fromkeys(ANIMATION_SCALES, written) if written is not None else {}
    assert phone.global_settings == expected
