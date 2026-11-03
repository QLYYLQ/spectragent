"""The typed clients of the service and the helper, and the tree models' alignment with android_env.

The clients (``spectragent/core/capabilities/android/device/clients.py``) talk to
:class:`~tests._helpers.fake_device_side.FakeProgram` through the fake ADB
server, which records every request's method and params, so each test checks
both what went to the device and what came back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import pytest

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.device.channel import (
    Channel,
    Component,
    JsonObject,
    RemoteError,
)
from spectragent.core.capabilities.android.device.clients import (
    HelperClient,
    ServiceClient,
    Stroke,
)
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
    AndroidAccessibilityWindowInfo,
    ProtoRect,
)
from tests._helpers.fake_adb import FakeAdbServer, SocketServer, scripted
from tests._helpers.fake_device_side import Answer, Failure, FakeProgram, Method

SERIAL = "emulator-5554"
TOKEN = "session-token-0123456789"
_WAIT_S = 10

#: android_env 1.2.3's ``android_env/proto/a11y/*.proto``, each message's fields
#: in field-number order.
PROTO_FIELDS = {
    "AndroidAccessibilityForest": ["windows"],
    "AndroidAccessibilityWindowInfo": [
        "bounds_in_screen",
        "display_id",
        "id",
        "layer",
        "title",
        "window_type",
        "is_accessibility_focused",
        "is_active",
        "is_focused",
        "is_in_picture_in_picture_mode",
        "tree",
    ],
    "AndroidAccessibilityTree": ["nodes"],
    "AndroidAccessibilityNodeInfo": [
        "unique_id",
        "bounds_in_screen",
        "class_name",
        "content_description",
        "hint_text",
        "package_name",
        "text",
        "text_selection_start",
        "text_selection_end",
        "view_id_resource_name",
        "window_id",
        "is_checkable",
        "is_checked",
        "is_clickable",
        "is_editable",
        "is_enabled",
        "is_focusable",
        "is_focused",
        "is_long_clickable",
        "is_password",
        "is_scrollable",
        "is_selected",
        "is_visible_to_user",
        "actions",
        "child_ids",
        "clickable_spans",
        "depth",
        "labeled_by_id",
        "label_for_id",
        "drawing_order",
        "tooltip_text",
    ],
    "AndroidAccessibilityAction": ["id", "label"],
    "AndroidAccessibilityNodeInfoClickableSpan": ["text", "url", "source", "start", "node_id"],
    "ProtoRect": ["left", "top", "right", "bottom"],
}


def test_every_tree_model_has_its_protos_fields_in_order_and_nodes_add_only_is_important() -> None:
    from spectragent.core.capabilities.android.device import forest

    for message, fields in PROTO_FIELDS.items():
        model = getattr(forest, message)
        extra = ["is_important"] if message == "AndroidAccessibilityNodeInfo" else []
        wire = [field.alias or name for name, field in model.model_fields.items()]
        assert wire == fields + extra, message


def test_a_forest_in_proto3_json_form_takes_the_protos_defaults_for_what_it_leaves_out() -> None:
    forest = AndroidAccessibilityForest.model_validate(
        {
            "windows": [
                {
                    "bounds_in_screen": {"right": 1080, "bottom": 2400},
                    "id": 12,
                    "window_type": "TYPE_APPLICATION",
                    "is_active": True,
                    "tree": {
                        "nodes": [
                            {"child_ids": [1], "class_name": "android.widget.FrameLayout"},
                            {
                                "unique_id": 1,
                                "depth": 1,
                                "text": "Search settings",
                                "view_id_resource_name": "com.android.settings:id/search",
                                "actions": [{"id": 16}],
                                "is_clickable": True,
                                "is_visible_to_user": True,
                                "is_important": True,
                            },
                        ]
                    },
                }
            ]
        }
    )

    (window,) = forest.windows
    root, search = window.tree.nodes
    assert (window.display_id, window.layer, window.title) == (0, 0, "")
    assert (root.unique_id, root.is_important, root.child_ids) == (0, False, (1,))
    assert search.accessibility_actions[0].label == "" and search.is_important
    assert search.bounds_in_screen.right == 0


def test_a_tree_field_the_proto_does_not_have_is_refused() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        AndroidAccessibilityNodeInfo.model_validate({"unique_id": 0, "importance": 1})
    with pytest.raises(ValueError, match="window_type"):
        AndroidAccessibilityWindowInfo.model_validate({"window_type": "TYPE_DESKTOP"})


@asynccontextmanager
async def _client(
    component: Component, methods: Mapping[str, Method]
) -> AsyncIterator[tuple[FakeProgram, Channel]]:
    program = FakeProgram(component=component, token=TOKEN, methods=methods)
    listening: dict[str, SocketServer] = {"socket": program.serve}
    async with FakeAdbServer({SERIAL: scripted({})}, sockets={SERIAL: listening.get}) as adb:
        reader, writer = await AdbClient(adb.host, adb.port).connect_abstract(SERIAL, "socket")
        async with asyncio.timeout(_WAIT_S):  # the whole test's body: a deadlock fails
            channel = await Channel.open(reader, writer, token=TOKEN, component=component)
            try:
                yield program, channel
            finally:
                await channel.close()


async def _ok(params: JsonObject) -> Answer:
    return Answer({"elapsed_ms": 50})


async def _empty(params: JsonObject) -> Answer:
    return Answer()


async def _screenshot(params: JsonObject) -> Answer:
    return Answer(
        {"width": 1080, "height": 2400, "format": "png", "capture_ms": 9, "encode_ms": 80},
        b"\x89PNG data",
    )


async def test_service_gestures_are_timed_strokes() -> None:
    async with _client("service", {"gesture": _ok}) as (program, channel):
        service = ServiceClient(channel)
        assert await service.tap(540, 660) == 50, "the device's elapsed ms"
        await service.tap(10.5, 20, duration_ms=1000)  # a long press
        await service.swipe((540, 1800), (540, 600), duration_ms=300)
        await service.gesture(
            [Stroke.hold((100, 100), 500), Stroke.line((900, 100), (900, 200), 400, start_ms=100)]
        )
        # One finger whose speed changes: slow, then fast.
        await service.gesture([Stroke(((100, 1000, 0), (150, 1000, 200), (900, 1000, 250)))])

    assert [params for _, params in program.calls] == [
        {"strokes": [{"points": [[540, 660, 0], [540, 660, 50]]}]},
        {"strokes": [{"points": [[10.5, 20, 0], [10.5, 20, 1000]]}]},
        {"strokes": [{"points": [[540, 1800, 0], [540, 600, 300]]}]},
        {
            "strokes": [
                {"points": [[100, 100, 0], [100, 100, 500]]},
                {"points": [[900, 100, 100], [900, 200, 500]]},
            ]
        },
        {"strokes": [{"points": [[100, 1000, 0], [150, 1000, 200], [900, 1000, 250]]}]},
    ]


async def test_a_cancelled_gesture_raises_with_the_services_code() -> None:
    async def cancelled(params: JsonObject) -> Answer:
        raise Failure("gesture_cancelled", "the system cancelled the gesture")

    async with _client("service", {"gesture": cancelled}) as (_, channel):
        with pytest.raises(RemoteError) as error:
            await ServiceClient(channel).tap(1, 1)

    assert error.value.code == "gesture_cancelled"


async def test_service_screenshots_tree_text_and_global_actions_come_back_typed() -> None:
    async def tree(params: JsonObject) -> Answer:
        return Answer(
            {
                "forest": {"windows": [{"id": 3, "tree": {"nodes": [{"text": "OK"}]}}]},
                "node_count": 1,
                "omitted_offscreen": 2,
                "capture_ms": 7,
            }
        )

    async def set_text(params: JsonObject) -> Answer:
        return Answer(
            {
                "package_name": "com.android.settings",
                "class_name": "android.widget.EditText",
                "view_id_resource_name": None,
            }
        )

    async def screenshot(params: JsonObject) -> Answer:
        return Answer(
            {
                "width": 1080,
                "height": 2400,
                "format": "png",
                "waited_ms": 120,
                "capture_ms": 30,
                "encode_ms": 90,
            },
            b"\x89PNG data",
        )

    methods = {
        "tree": tree,
        "set_text": set_text,
        "commit_text": _empty,
        "screenshot": screenshot,
        "global_action": _empty,
    }
    async with _client("service", methods) as (program, channel):
        service = ServiceClient(channel)
        capture = await service.tree(max_nodes=300)
        target = await service.set_text("蓝牙 bluetooth")
        await service.set_text("at the cursor", replace=False)
        committed = await service.commit_text("more")
        await service.commit_text("over it all", replace=True)
        shot = await service.screenshot()
        await service.global_action("recents")

    assert capture.forest.windows[0].tree.nodes[0].text == "OK"
    assert (capture.node_count, capture.omitted_offscreen) == (1, 2)
    assert target.class_name == "android.widget.EditText" and committed.package_name is None
    assert (shot.png, shot.producer, shot.waited_ms, shot.encode_ms) == (
        b"\x89PNG data",
        "accessibility_service",
        120,
        90,
    )
    assert program.calls[:5] == [
        ("tree", {"max_nodes": 300}),
        ("set_text", {"text": "蓝牙 bluetooth", "replace": True}),
        ("set_text", {"text": "at the cursor", "replace": False}),
        ("commit_text", {"text": "more", "replace": False}),
        ("commit_text", {"text": "over it all", "replace": True}),
    ]
    assert program.calls[-1] == ("global_action", {"action": "recents"})


async def test_the_node_with_input_focus_comes_back_with_its_bounds_or_as_none() -> None:
    asked: list[JsonObject] = []
    nodes: list[JsonObject | None] = [
        {
            "bounds_in_screen": {"left": 30, "top": 120, "right": 1050, "bottom": 246},
            "field_bounds_in_screen": {"left": 0, "top": 63, "right": 1080, "bottom": 300},
            "label_bounds_in_screen": {"left": 30, "top": 400, "right": 300, "bottom": 450},
            "window_at_point": 13,
            "is_editable": True,
            "window_id": 13,
            "node_hash": 1240117,
            "package_name": "com.google.android.settings.intelligence",
            "class_name": "android.widget.EditText",
            "view_id_resource_name": None,
        },
        None,
    ]

    async def focus(params: JsonObject) -> Answer:
        asked.append(params)
        return Answer({"focused": nodes.pop(0)})

    async with _client("service", {"focus": focus}) as (_, channel):
        service = ServiceClient(channel)
        field, nothing = await service.focus(field_bounds=True, at=(540, 70)), await service.focus()

    assert asked == [{"field_bounds": True, "x": 540, "y": 70}, {"field_bounds": False}]
    assert field is not None and nothing is None
    assert field.is_editable and field.class_name == "android.widget.EditText"
    assert field.field_holds(540, 70) and field.field_holds(1080, 300), "the field as drawn"
    assert field.field_holds(540, 660) is False, "below it"
    assert field.field_holds(100, 420), "on its own label, outside the field as drawn"
    unbounded = field.model_copy(update={"field_bounds_in_screen": None})
    assert unbounded.field_holds(540, 183) is None and unbounded.field_holds(100, 420)
    covered = field.model_copy(update={"window_at_point": 3})
    assert covered.field_holds(540, 70) is False, "another window is on top at the point"
    moved = field.model_copy(update={"bounds_in_screen": ProtoRect(top=900, bottom=1026)})
    assert field.same_node(moved), "a keyboard moved it: the same field"
    assert not field.same_node(field.model_copy(update={"node_hash": 7})), "another view"
    assert not field.same_node(field.model_copy(update={"window_id": 14})), "another window"


async def test_service_events_come_back_typed_in_order() -> None:
    async with _client("service", {}) as (program, channel):
        events = await ServiceClient(channel).events()
        for package in ("com.android.settings", "com.google.android.apps.nexuslauncher"):
            program.emit(
                type="TYPE_WINDOW_STATE_CHANGED",
                time_ms=1234,
                package_name=package,
                class_name=None,
                window_id=7,
                content_change_types=0,
                window_changes=0,
                text=["Settings"],
                content_description=None,
                before_text=None,
                from_index=-1,
                to_index=-1,
                item_count=-1,
                added_count=-1,
                removed_count=-1,
                scroll_delta_x=-1,
                scroll_delta_y=-1,
            )
        async with asyncio.timeout(_WAIT_S):
            first, second = await events.get(), await events.get()
        assert events.get_nowait() is None

    assert (first.seq, first.package_name, first.text) == (0, "com.android.settings", ("Settings",))
    assert second.seq == 1 and second.package_name == "com.google.android.apps.nexuslauncher"


async def test_helper_keys_timed_strokes_and_screenshots() -> None:
    methods = {"key": _empty, "gesture": _ok, "screenshot": _screenshot}
    async with _client("helper", methods) as (program, channel):
        helper = HelperClient(channel)
        await helper.key("KEYCODE_ENTER")
        await helper.tap(540, 660)
        await helper.swipe((1, 2), (3, 4), duration_ms=300)
        await helper.gesture(Stroke(((1, 2, 0), (2, 4, 16), (3, 9, 24))))
        shot = await helper.screenshot()

    assert [call for call in program.calls if call[0] != "screenshot"] == [
        ("key", {"code": "KEYCODE_ENTER"}),
        ("gesture", {"strokes": [{"points": [[540, 660, 0], [540, 660, 50]]}]}),
        ("gesture", {"strokes": [{"points": [[1, 2, 0], [3, 4, 300]]}]}),
        ("gesture", {"strokes": [{"points": [[1, 2, 0], [2, 4, 16], [3, 9, 24]]}]}),
    ]
    assert (shot.producer, shot.waited_ms, shot.width) == ("shell_helper", 0, 1080)


async def test_helper_types_text_as_key_events_and_raises_for_a_character_without_a_key() -> None:
    async def type_text(params: JsonObject) -> Answer:
        if params["text"] == "蓝牙":
            raise Failure("unmappable_text", "the device's key map has no key for '蓝' (U+84DD)")
        return Answer()

    async with _client("helper", {"type_text": type_text}) as (program, channel):
        helper = HelperClient(channel)
        await helper.type_text("Wi-Fi & more")
        await helper.type_text("over it all", replace=True)
        with pytest.raises(RemoteError) as refused:
            await helper.type_text("蓝牙")

    assert program.calls == [
        ("type_text", {"text": "Wi-Fi & more", "replace": False}),
        ("type_text", {"text": "over it all", "replace": True}),
        ("type_text", {"text": "蓝牙", "replace": False}),
    ]
    assert refused.value.code == "unmappable_text"


async def test_helper_reports_what_enabling_and_disabling_did_to_the_setting() -> None:
    async def enable(params: JsonObject) -> Answer:
        return Answer(
            {"previous": None, "current": "org.spectragent.device/.DeviceService", "changed": True}
        )

    async def disable(params: JsonObject) -> Answer:
        return Answer(
            {"previous": "org.spectragent.device/.DeviceService", "current": None, "changed": True}
        )

    async with _client("helper", {"enable_service": enable, "disable_service": disable}) as (
        _,
        channel,
    ):
        helper = HelperClient(channel)
        enabled = await helper.enable_service()
        disabled = await helper.disable_service()

    assert (enabled.previous, enabled.changed) == (None, True)
    assert (disabled.current, disabled.changed) == (None, True)
