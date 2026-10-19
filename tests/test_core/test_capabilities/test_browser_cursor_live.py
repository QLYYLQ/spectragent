"""Real Chrome: clean scene evidence and cursor model input stay separate."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import ContentPartImageParam
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action
from tests._helpers.fixture_site import FixtureSite
from tests._helpers.scripted_llm import ScriptedLLMServer
from tests._helpers.trace_builder import as_chat

pytestmark = pytest.mark.requires_chrome


async def look(cap: BrowserCapability, cycle: int) -> ScreenshotObs:
    await cap.begin_observation(cycle)
    image = await cap.resolve_fragment("browser.screenshot")
    assert isinstance(image, ScreenshotObs)
    return image


async def act(cap: BrowserCapability, name: str, **params: Any) -> None:
    result = await cap.execute_action(Action(name=f"browser.{name}", params=params))
    assert result.success, result.error


async def test_click_drag_scroll_tabs_and_two_runs_keep_confirmed_positions_separate() -> None:
    cap = BrowserCapability(screenshot_cursor=True, viewport_width=800, viewport_height=600)
    cap.backend = LocalChromeProvider(headless=True, force_device_scale_factor=1)  # type: ignore[assignment]
    for _run in range(2):
        try:
            await cap.setup(negotiation={"browser.screenshot"})
            initial = await look(cap, 1)
            assert initial.cursor is not None and initial.cursor.position is None
            first_target = cap._session_to_target[cap.default_session_id]
            await act(cap, "click_coordinate", coordinate_x=120, coordinate_y=130)
            clicked = await look(cap, 2)
            assert clicked.cursor is not None and clicked.cursor.position == (120, 130)
            assert clicked.model_image_bytes() != clicked.image_bytes
            # The model overlay is not in the page or the environment screenshot.
            assert initial.image_bytes == clicked.image_bytes
            await act(cap, "drag", start_x=120, start_y=130, end_x=240, end_y=260)
            dragged = await look(cap, 3)
            assert dragged.cursor is not None and dragged.cursor.position == (240, 260)
            await act(cap, "scroll", direction="down", amount=50)
            scrolled = await look(cap, 4)
            assert scrolled.cursor is not None and scrolled.cursor.position == (240, 260)
            created = await cap.cdp.send("Target.createTarget", {"url": "about:blank"})
            await cap.switch_tab(created["targetId"])
            fresh = await look(cap, 5)
            assert fresh.cursor is not None and fresh.cursor.position is None
            await act(cap, "click_coordinate", coordinate_x=350, coordinate_y=360)
            second = await look(cap, 6)
            assert second.cursor is not None and second.cursor.position == (350, 360)
            await cap.switch_tab(first_target)
            previous = await look(cap, 7)
            assert previous.cursor is not None and previous.cursor.position == (240, 260)
            await cap.close_tab(cap._target_to_session[created["targetId"]])
            # Repeated fragment reads in one cycle reuse the same image and metadata.
            assert await cap.resolve_fragment("browser.screenshot") is previous
        finally:
            await cap.teardown()


@pytest.mark.parametrize("trajectory", [True, False], ids=["full-prompt", "no-prompt-recording"])
async def test_runtime_trace_records_clean_original_and_actual_model_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trajectory: bool,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    raw = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3] / "configs/flywheel/record_ui_tars_pinned.yaml"
        ).read_text()
    )
    with (
        FixtureSite() as site,
        ScriptedLLMServer(
            [
                "Action: click(point='(1100,650)')",
                "Action: finished(content='cursor checked')",
            ]
        ) as llm,
    ):
        raw["agent"][0]["llm"].update(
            model=llm.model, base_url=llm.base_url, api_key="scripted", max_retries=0
        )
        capability = raw["environment"][0]["capabilities"][0]
        capability.update(screenshot_cursor=True, screenshot_cursor_size=40, settle_quiet_ms=30)
        capability["backend"]["extra_args"] = site.chrome_args()
        writer_config = raw["hook"][0]
        writer_config.pop("archive")
        writer_config.update(trace_dir=str(tmp_path), recording={"trajectory": trajectory})
        run = ConfigComposer(registrars=collect_registrars()).compose(raw)
        writer = next(hook for hook in run.instances("hook") if isinstance(hook, TraceWriter))
        result = await AgentRuntime.for_agent(run, "main").execute(
            "Look at the pointer.",
            timeout=90,
            env_context={"start_url": site.url("new_tab")},
        )
        assert result.answer == "cursor checked", result
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert trace.manifest.status.value == "complete"
    image = trace.observation_fragment(1, "browser.screenshot")
    assert isinstance(image, ScreenshotObs)
    assert image.cursor is not None and image.cursor.position == (1100, 650)
    assert trace.screenshot(1) == image.image_bytes
    sent = [
        part["image_url"]["url"]
        for message in llm.requests[1]["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ][-1]
    assert base64.b64decode(sent.split(",", 1)[1]) == image.model_image_bytes()
    assert image.model_image_bytes() != trace.screenshot(1)
    if trajectory:
        call = trace.llm_messages(1)[0]
        recorded = [
            part.image_url.url
            for message in as_chat(call).messages
            if isinstance(message.content, list)
            for part in message.content
            if isinstance(part, ContentPartImageParam)
        ][-1]
        assert recorded == sent
    else:
        assert trace.manifest.prompt_images == []
    resolved = trace.config.resolved["environment"][0]["capabilities"][0]
    assert resolved["screenshot_cursor"] is True and resolved["screenshot_cursor_size"] == 40
