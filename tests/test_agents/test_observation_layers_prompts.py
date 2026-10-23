"""What each agent reads, by consumer: effects, capture state, browser page state.

``docs/architecture_design.md`` §4.4, "观测增强按消费方分层":

- action effects reach every agent, as each effect's ``describe()`` line;
- the platform-neutral ``capture_state`` reaches every agent, declared optional;
- browser page state (``browser_meta``: URL, title, tabs) reaches only the
  browser-specific agents, Skyvern and BrowserUse, which require it.

Each agent's ``show_action_effects`` and ``show_capture_state`` switch (both
default on) decide what the model reads; the capture-state switch does it by
narrowing the agent's negotiation demand, so the fragment is not even captured.
The prompt snapshots below pin the exact placement in each agent's message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar, Literal

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import demand_of, negotiate_observation
from spectragent.schemas.action import (
    Action,
    ActionEffect,
    ActionResult,
    PointWriter,
    write_viewport_point,
)
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.capture_state import (
    CaptureStateFragment,
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
    SettleState,
)
from spectragent.schemas.observation.dynamic_model import build_observation_model_from_result
from spectragent.schemas.observation.fragments import HistoryObs, StepInfoObs, TaskObs
from spectragent.schemas.prompt import RestoredHistory
from spectragent.schemas.restoration import RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent
from tests._helpers.fragment_formats import formats_for
from tests._helpers.frames import step_context

AGENT_CLASSES: tuple[type[Any], ...] = (UITarsAgent, SkyvernAgent, BrowserUseAgent)
BROWSER_AGENTS: tuple[type[Any], ...] = (SkyvernAgent, BrowserUseAgent)

#: A raw effect field value no ``describe()`` line contains.
RAW = "RAW-EFFECT-FIELD"

_PLATFORM = (
    "<input_platform>\n"
    "Actual browser input platform: macOS (macos).\n"
    "Keyboard modifiers are literal: Ctrl/Control sends Control; Cmd/Command/Meta sends "
    "Meta. On macOS, Command is Meta: use Meta+A for Select All. Ctrl+A still sends "
    "Control+A; it is not rewritten to Command+A.\n"
    "</input_platform>"
)
_CAPTURE_STATE = (
    "<capture_state>\n"
    "Settle: settled after waiting 40 ms.\n"
    "Foreground change: none.\n"
    "Pointer: (1, 2) in viewport pixels.\n"
    "Failed visible resources: none failed.\n"
    "</capture_state>"
)
#: UI-TARS writes the pointer on its own grid: here the screenshot's pixels,
#: 2000x1000 over a 1000x500 viewport, so the viewport's (1, 2) is (2, 4).
_UI_TARS_CAPTURE_STATE = _CAPTURE_STATE.replace("(1, 2) in viewport pixels.", "(2, 4).")
_PAGE_STATE = "URL: https://f.test/\n\nTitle: F\n\nTabs: [T1] F (https://f.test/)"


class ProbeEffect(ActionEffect):
    """A test effect: its line is the moved distance, its raw field a marker."""

    moved: int
    raw_marker: str = RAW

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return f"Probe moved {self.moved} px."


def _history(*, effects: bool = True) -> list[StepRecord]:
    def effect(action: str, moved: int) -> ProbeEffect | None:
        return ProbeEffect(effect_id=action, moved=moved) if effects else None

    return [
        StepRecord(
            step=0,
            llm_output="reply",
            actions=[
                Action(name="browser.click", params={"index": 29}),
                Action(name="browser.scroll", params={"direction": "down"}),
            ],
            action_results=[
                ActionResult(success=True, effect=effect("browser.click", 0)),
                ActionResult(success=False, error="blocked", effect=effect("browser.scroll", 600)),
            ],
        )
    ]


def _capture_state() -> SimpleNamespace:
    return SimpleNamespace(
        settle_state=SettleState(outcome="settled", waited_ms=40),
        foreground_change=None,
        pointer=PointerState(status="known", x=1, y=2),
        failed_visible_resources=FailedVisibleResources(status="measured", count=0),
    )


def _observation(*, effects: bool = True, capture_state: bool = True) -> SimpleNamespace:
    observation = SimpleNamespace(
        task=TaskObs(user_request="Open"),
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        history=HistoryObs(items=_history(effects=effects)),
        step_info=StepInfoObs(step=2, max_steps=10, today="2026-10-20"),
        dom=SimpleNamespace(element_tree="[29]<button>Live</button>"),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            image_width=2000,
            image_height=1000,
            viewport_width=1000,
            viewport_height=500,
        ),
        browser_meta=BrowserMetaObs(
            url="https://f.test/",
            page_title="F",
            tabs=[{"tab_id": "T1", "title": "F", "url": "https://f.test/"}],
        ),
    )
    if capture_state:
        observation.capture_state = _capture_state()
    return observation


def _prepared(agent_class: type[Any], **switches: Any) -> tuple[Any, ActionSpace]:
    """*agent_class* prepared with the actions of a browser it would negotiate them from.

    Only the ones its own demand admits: a DOM-mode BrowserUse never holds a
    coordinate action, so it teaches no grid and writes its feedback in
    viewport pixels. The space is framed as a run frames it.
    """
    agent = make_agent(agent_class, memory=SimpleMemory(max_messages=20), **switches)
    required, optional = agent.negotiation_demand("action")
    keys = {
        "browser.click",
        "browser.click_coordinate",
        "browser.scroll",
        "browser.wait",
        "browser.done",
    }
    descriptors = [
        action.descriptor(fragments=frozenset({"browser.dom", "browser.screenshot"}))
        for action in BrowserCapability().actions
        if action.action_id in keys and action.action_id.split(".")[-1] in required | optional
    ]
    space = space_for(agent, descriptors)
    agent.prepare(
        task="Open",
        action_space=space,
        execution_limits=ExecutionLimits(max_actions_per_step=2),
        **formats_for(agent),
    )
    return agent, space


def _text(messages: list[BaseMessage]) -> str:
    """Every user message's text, the image parts left out."""
    texts: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            texts.append(message.content)
        else:
            texts.extend(
                part.text for part in message.content if isinstance(part, ContentPartTextParam)
            )
    return "\n".join(texts)


async def _prompt(agent_class: type[Any], observation: Any = None, **switches: Any) -> str:
    agent, space = _prepared(agent_class, **switches)
    observation = observation if observation is not None else _observation()
    messages = await agent.build_react_prompt(
        observation, context=step_context(space, observation, step=1)
    )
    return _text(messages)


# ── Prompt snapshots, everything on ───────────────────────────────────


async def test_ui_tars_step_text() -> None:
    """Effects inside each returned result; the capture state after the feedback.

    Each action is the call UI-TARS writes for it; one it has no verb for
    (``browser.click`` by index) keeps its JSON.
    """
    feedback = {
        "actions_without_returned_results": [],
        "discarded_action_count": 0,
        "error": None,
        "error_phase": None,
        "returned_results": [
            {
                "action": {"name": "browser.click", "params": {"index": 29}},
                "result": {
                    "effect": "Probe moved 0 px.",
                    "error": None,
                    "extracted_content": None,
                    "is_done": False,
                    "success": True,
                },
            },
            {
                "action": "scroll(direction='down')",
                "result": {
                    "effect": "Probe moved 600 px.",
                    "error": "blocked",
                    "extracted_content": None,
                    "is_done": False,
                    "success": False,
                },
            },
        ],
        "step": 1,
    }
    assert await _prompt(UITarsAgent) == (
        "Step: 2/10\n"
        "<action_results>\n"
        + json.dumps(feedback, ensure_ascii=False, sort_keys=True)
        + "\n</action_results>\n"
        + _UI_TARS_CAPTURE_STATE
        + "\nScreenshot: 2000x1000 pixels."
    )


async def test_skyvern_user_message() -> None:
    """The window lists what ran with its result and effect; the page state precedes the tree."""
    assert await _prompt(SkyvernAgent) == (
        "<user_request>\nOpen\n</user_request>\n"
        f"{_PLATFORM}\n"
        "<step_info>step 2/10 (today=2026-10-20)</step_info>\n"
        "<action_history>\n"
        "browser.click({'index': 29}) -> ok | effect: Probe moved 0 px.\n"
        "browser.scroll({'direction': 'down'}) -> error: blocked | effect: Probe moved 600 px.\n"
        "</action_history>\n\n"
        f"{_PAGE_STATE}\n\n"
        f"{_CAPTURE_STATE}\n\n"
        "Element Tree:\n[29]<button>Live</button>"
    )


async def test_browser_use_user_message() -> None:
    """An ``<effects>`` element after a step's results; the page state opens the browser state."""
    assert await _prompt(BrowserUseAgent) == (
        "<agent_history>\n"
        "<step_1>\n"
        "<llm_output>reply</llm_output>\n"
        "<actions>browser.click({'index': 29}), browser.scroll({'direction': 'down'})</actions>\n"
        "<results>ok=True; ok=False blocked</results>\n"
        "<effects>\n"
        "1. browser.click: Probe moved 0 px.\n"
        "2. browser.scroll: Probe moved 600 px.\n"
        "</effects>\n"
        "</step_1>\n"
        "</agent_history>\n"
        "<agent_state>\n"
        "<user_request>\nOpen\n</user_request>\n"
        "<step_info>step 2/10, today=2026-10-20</step_info>\n"
        "</agent_state>\n"
        f"{_PLATFORM}\n"
        "<browser_state>\n"
        f"{_PAGE_STATE}\n\n"
        f"{_CAPTURE_STATE}\n\n"
        "DOM:\n[29]<button>Live</button>\n"
        "</browser_state>"
    )


# ── Switches off ──────────────────────────────────────────────────────


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_effects_off_reads_as_if_nothing_was_measured(agent_class: type[Any]) -> None:
    """Off is exactly the prompt of a run whose actions measured no effect."""
    off = await _prompt(agent_class, show_action_effects=False)
    unmeasured = await _prompt(agent_class, _observation(effects=False))
    assert off == unmeasured
    assert "Probe moved" not in off
    assert "effect" not in off.lower()


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_effects_on_render_lines_never_fields(agent_class: type[Any]) -> None:
    text = await _prompt(agent_class)
    assert "Probe moved 0 px." in text and "Probe moved 600 px." in text
    assert RAW not in text
    assert "raw_marker" not in text and "effect_id" not in text


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_no_capture_state_renders_no_block(agent_class: type[Any]) -> None:
    """What ``show_capture_state: false`` leaves the agent: no fragment, so no block."""
    shown = await _prompt(agent_class)
    absent = await _prompt(agent_class, _observation(capture_state=False))
    block = _UI_TARS_CAPTURE_STATE if agent_class is UITarsAgent else _CAPTURE_STATE
    assert block in shown
    separator = "\n" if agent_class is UITarsAgent else "\n\n"
    assert absent == shown.replace(separator + block, "")


async def test_ui_tars_never_renders_browser_page_state() -> None:
    """The observation carries ``browser_meta``; UI-TARS never reads it."""
    text = await _prompt(UITarsAgent)
    assert "https://f.test/" not in text
    assert "URL:" not in text and "Tabs:" not in text and "Title:" not in text


@pytest.mark.parametrize("agent_class", BROWSER_AGENTS)
async def test_browser_agents_render_the_page_state(agent_class: type[Any]) -> None:
    assert _PAGE_STATE in await _prompt(agent_class)


# ── Media playback state ─────────────────────────────────────────────


def _with_media(observation: SimpleNamespace) -> SimpleNamespace:
    from spectragent.core.capabilities.browser.media import (
        MediaElementState,
        MediaReading,
        MediaRect,
        MediaState,
    )

    reading = MediaReading(current_time=19.4, paused=True, seeking=False, ready_state=4)
    observation.screenshot = observation.screenshot.model_copy(
        update={
            "media": MediaState(
                read_before="measured",
                read_after="measured",
                elements=[
                    MediaElementState(
                        frame="main",
                        order=0,
                        tag="video",
                        rect=MediaRect(x=240, y=24, width=400, height=672),
                        source_kind="blob",
                        duration=210.769,
                        before=reading,
                        after=reading,
                    )
                ],
            )
        }
    )
    return observation


_MEDIA_STATE = (
    "<media_state>\nvideo 1 in frame main: at 00:19.4 of 03:30.8, paused.\n</media_state>"
)


@pytest.mark.parametrize("agent_class", BROWSER_AGENTS)
async def test_media_state_is_shown_only_when_switched_on(agent_class: type[Any]) -> None:
    """Off by default: the measured state is evidence, not prompt; on, it follows the capture state."""
    assert make_agent(agent_class).show_media_state is False
    off = await _prompt(agent_class, _with_media(_observation()))
    assert off == await _prompt(agent_class)
    on = await _prompt(agent_class, _with_media(_observation()), show_media_state=True)
    assert f"{_CAPTURE_STATE}\n\n{_MEDIA_STATE}" in on
    assert on.replace(f"\n\n{_MEDIA_STATE}", "") == off


@pytest.mark.parametrize("agent_class", BROWSER_AGENTS)
async def test_media_state_not_measured_says_so(agent_class: type[Any]) -> None:
    text = await _prompt(agent_class, show_media_state=True)
    assert "The media playback state was not measured this step." in text


async def test_ui_tars_has_no_media_switch_and_renders_no_media() -> None:
    assert not hasattr(make_agent(UITarsAgent), "show_media_state")
    assert "media_state" not in await _prompt(UITarsAgent, _with_media(_observation()))


# ── Negotiation demand ───────────────────────────────────────────────


def test_ui_tars_declares_no_browser_page_state_and_optional_capture_state() -> None:
    required, optional = make_agent(UITarsAgent).negotiation_demand("observation")
    assert "browser_meta" not in required | optional
    assert "capture_state" in optional


@pytest.mark.parametrize(
    "agent",
    [
        pytest.param(lambda: make_agent(SkyvernAgent), id="skyvern"),
        *(
            pytest.param(
                lambda mode=mode: make_agent(BrowserUseAgent, observation_mode=mode),
                id=f"browser_use-{mode}",
            )
            for mode in ("dom", "dom_vision", "vision")
        ),
    ],
)
def test_browser_agents_require_page_state_in_every_mode(agent: Any) -> None:
    required, optional = agent().negotiation_demand("observation")
    assert "browser_meta" in required
    assert "capture_state" in optional


_SWITCHABLE: list[Any] = [
    pytest.param(UITarsAgent, {}, id="ui_tars"),
    pytest.param(SkyvernAgent, {}, id="skyvern"),
    *(
        pytest.param(BrowserUseAgent, {"observation_mode": mode}, id=f"browser_use-{mode}")
        for mode in ("dom", "dom_vision", "vision")
    ),
]


@pytest.mark.parametrize(("agent_class", "config"), _SWITCHABLE)
def test_show_capture_state_off_excludes_only_capture_state(
    agent_class: type[Any], config: dict[str, Any]
) -> None:
    on = make_agent(agent_class, **config)
    off = make_agent(agent_class, show_capture_state=False, **config)
    on_required, on_optional = on.negotiation_demand("observation")
    off_required, off_optional = off.negotiation_demand("observation")
    assert off_required == on_required
    assert off_optional == on_optional - {"capture_state"}
    assert "capture_state" in on_optional
    # The framework's own bound check accepts the narrowed demand.
    assert demand_of(off, "observation") == (off_required, off_optional)
    # Actions are not the switch's business.
    assert off.negotiation_demand("action") == on.negotiation_demand("action")


def test_browser_use_names_the_switch_only_when_it_narrows() -> None:
    assert repr(make_agent(BrowserUseAgent)) == "browser_use(observation_mode='dom')"
    assert (
        repr(make_agent(BrowserUseAgent, observation_mode="vision", show_capture_state=False))
        == "browser_use(observation_mode='vision', show_capture_state=False)"
    )


class _Env:
    def __init__(self, provided: set[str]) -> None:
        self.provided_fragments = frozenset(provided)


def _register_probe_capture_state() -> type[CaptureStateFragment]:
    """A capability's concrete fragment, registered for this test only."""

    class ProbeCaptureState(CaptureStateFragment):
        __registry_key__ = "probe.capture_state"
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        @property
        def settle_state(self) -> SettleState | None:
            return None

        @property
        def foreground_change(self) -> ForegroundChange | None:
            return None

    return ProbeCaptureState


@pytest.mark.parametrize(("agent_class", "config"), _SWITCHABLE)
def test_the_fragment_is_negotiated_only_while_shown(
    agent_class: type[Any], config: dict[str, Any]
) -> None:
    """On, the environment's capture state is activated; off, it is not even asked for."""
    _register_probe_capture_state()
    env = _Env(
        {
            "browser.platform",
            "browser.screenshot",
            "browser.dom",
            "browser.browser_meta",
            "probe.capture_state",
        }
    )
    for shown in (True, False):
        agent = make_agent(agent_class, show_capture_state=shown, **config)
        negotiated = negotiate_observation(agent, ReactLoop(), env)
        assert ("probe.capture_state" in negotiated.active_fragments) is shown
        model = build_observation_model_from_result(negotiated)
        assert ("capture_state" in model.model_fields) is shown


def test_ui_tars_runs_in_an_environment_without_browser_page_state_or_capture_state() -> None:
    """Cross-platform: a screenshot and a platform are all an environment owes UI-TARS."""
    negotiated = negotiate_observation(
        make_agent(UITarsAgent),
        ReactLoop(),
        _Env({"browser.platform", "browser.screenshot", "browser.browser_meta"}),
    )
    assert "browser.browser_meta" not in negotiated.active_fragments


# ── Restored history ─────────────────────────────────────────────────


def _restored_history() -> RestoredHistory:
    source = Action(name="browser.click_coordinate", params={"coordinate_x": 2, "coordinate_y": 4})
    actual = Action(name="browser.click_coordinate", params={"coordinate_x": 3, "coordinate_y": 5})
    result = ActionResult(
        success=True, effect=ProbeEffect(effect_id="browser.click_coordinate", moved=7)
    )
    return RestoredHistory(
        source_run_id="source-run",
        source_step=3,
        source_reply="old reply",
        source_actions=[source],
        actions=[actual],
        results=[result],
        executions=[
            RestoredAction(
                recorded=source,
                executed=actual,
                method="identity",
                recorded_success=True,
                live_success=True,
                result=result,
            )
        ],
    )


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
@pytest.mark.parametrize("shown", [True, False])
async def test_restored_history_renders_the_effect_measured_while_restoring(
    agent_class: type[Any], shown: bool
) -> None:
    agent, space = _prepared(agent_class, show_action_effects=shown)
    observation = _observation()
    messages = await agent.build_restored_history(
        observation,
        history=_restored_history(),
        context=step_context(space, observation, step=3, phase="history"),
    )
    restored = messages[-1].text
    facts = json.loads(restored.split("\n", 1)[1])
    (executed,) = facts["actual_restored_execution"]
    assert ("effect" in executed["result"]) is shown
    if shown:
        assert executed["result"]["effect"] == "Probe moved 7 px."
    assert RAW not in _text(messages)


# ── Skyvern's window, from the Loop's history ─────────────────────────


def _skyvern_history() -> list[StepRecord]:
    """Three steps: one click; a batch that raised on its second action; a done."""
    return [
        StepRecord(
            step=0,
            actions=[Action(name="browser.click", params={"index": 1})],
            action_results=[
                ActionResult(success=True, effect=ProbeEffect(effect_id="browser.click", moved=1))
            ],
        ),
        StepRecord(
            step=1,
            actions=[
                Action(name="browser.type", params={"index": 2, "text": "hi"}),
                Action(name="browser.click", params={"index": 3}),
                Action(name="browser.scroll", params={"direction": "down"}),
            ],
            action_results=[ActionResult(success=False, error="field is read-only")],
            truncated_actions=1,
            error_phase="act",
            error="Element 3 is detached",
        ),
        StepRecord(
            step=2,
            actions=[Action(name="browser.wait", params={})],
            action_results=[
                ActionResult(success=True, effect=ProbeEffect(effect_id="browser.wait", moved=0))
            ],
        ),
    ]


async def _window(**config: Any) -> list[str]:
    observation = _observation()
    observation.history = HistoryObs(items=_skyvern_history())
    text = await _prompt(SkyvernAgent, observation, **config)
    return text.split("<action_history>\n")[1].split("\n</action_history>")[0].splitlines()


async def test_skyvern_window_lists_executed_actions_across_steps() -> None:
    """An action that raised carries the step's error; one the Loop never reached is absent."""
    assert await _window(action_history_window=10) == [
        "browser.click({'index': 1}) -> ok | effect: Probe moved 1 px.",
        "browser.type({'index': 2, 'text': 'hi'}) -> error: field is read-only",
        "browser.click({'index': 3}) -> error: Element 3 is detached",
        "browser.wait({}) -> ok | effect: Probe moved 0 px.",
    ]


async def test_skyvern_window_keeps_the_most_recent_actions() -> None:
    assert await _window(action_history_window=2) == [
        "browser.click({'index': 3}) -> error: Element 3 is detached",
        "browser.wait({}) -> ok | effect: Probe moved 0 px.",
    ]


async def test_skyvern_window_without_effects_shown() -> None:
    assert await _window(action_history_window=10, show_action_effects=False) == [
        "browser.click({'index': 1}) -> ok",
        "browser.type({'index': 2, 'text': 'hi'}) -> error: field is read-only",
        "browser.click({'index': 3}) -> error: Element 3 is detached",
        "browser.wait({}) -> ok",
    ]
