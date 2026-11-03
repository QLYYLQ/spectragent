"""M3A's prompts, its history and its summary call, its contract with a run, and a run on Android.

The prompts are AndroidWorld's to the byte: ``data/androidworld_prompts.json``
holds what AndroidWorld's own ``m3a.py`` (commit ``55ddaf5e``) wrote for the
same goal, history and element list, written by running it in AndroidWorld's
interpreter. The summary is a helper call on the agent's own model, recorded
with its own purpose, never the step's decision (``docs/architecture_design.md``
§4.1). The run at the end goes the whole way: the real ``AgentRuntime``, the
real Android capability over the scripted phone (``tests/_helpers/android_device.py``),
a scripted model, and the trace recorder.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from spectragent.agents._registrar import AgentBase
from spectragent.agents.m3a import M3AAgent
from spectragent.agents.m3a.reply import NOT_IN_FORMAT
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.prompts import agent_prompt_variants, load_prompt, template_variables
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
    AndroidAccessibilityTree,
    AndroidAccessibilityWindowInfo,
)
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import (
    AndroidScreenshotObs,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.ui_tree.models import UITreeElement
from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.base import PromptModelOwner
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.call_context import CallAttribution, RunCallContext
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs, TaskObs
from spectragent.schemas.prompt import PromptContribution, ReactPromptContext
from spectragent.schemas.step_record import StepRecord
from tests._helpers.android_device import (
    ScriptedPhone,
    configured,
    fake_service_apk,
    phone_server,
    settings_forest,
)
from tests._helpers.android_runs import ScriptedModel
from tests._helpers.assembly import AGENT_ID, NoModel, make_env, make_run

_ANDROIDWORLD = json.loads(
    (Path(__file__).parent / "data" / "androidworld_prompts.json").read_text("utf-8")
)
#: The element list the recorded prompts were written for: elements 0 and 2 (1 unused).
_ELEMENTS: str = _ANDROIDWORLD["elements"]
_GOAL = "Turn wifi off."


def _png(
    colour: tuple[int, int, int] = (30, 30, 30), size: tuple[int, int] = (1080, 2400)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _tree(
    serialized: str = _ELEMENTS, boxes: Sequence[tuple[int, int, int, int]] = ()
) -> AndroidUITreeObs:
    """A cleaned tree of one window whose elements are numbered 0, 2, 4, ... at *boxes*."""
    boxes = boxes or ((0, 300, 1080, 450), (0, 450, 1080, 600))
    nodes = [
        AndroidAccessibilityNodeInfo.model_validate(
            {"bounds_in_screen": {"right": 1080, "bottom": 2400}}
        )
    ]
    nodes += [
        AndroidAccessibilityNodeInfo.model_validate(
            {
                "unique_id": n,
                "bounds_in_screen": {"left": x0, "top": y0, "right": x1, "bottom": y1},
            }
        )
        for n, (x0, y0, x1, y1) in enumerate(boxes, start=1)
    ]
    window = AndroidAccessibilityWindowInfo(id=7, tree=AndroidAccessibilityTree(nodes=tuple(nodes)))
    return AndroidUITreeObs(
        producer="accessibility_service",
        forest=AndroidAccessibilityForest(windows=(window,)),
        elements=tuple(
            UITreeElement(index=2 * (n - 1), window_id=7, unique_id=n)
            for n in range(1, len(boxes) + 1)
        ),
        serialized=serialized,
        node_count=len(nodes),
    )


@dataclass
class _Observation:
    """The fragments M3A reads, as a run hands them over."""

    ui_tree: AndroidUITreeObs = field(default_factory=_tree)
    screenshot: AndroidScreenshotObs = field(
        default_factory=lambda: AndroidScreenshotObs(
            image_bytes=_png(),
            image_width=1080,
            image_height=2400,
            viewport_width=1080,
            viewport_height=2400,
        )
    )
    task: TaskObs = field(default_factory=lambda: TaskObs(user_request=_GOAL))
    history: HistoryObs = field(default_factory=HistoryObs)


@dataclass
class _Summarizer:
    """A chat model answering each call with the next reply, keeping each call and its attribution."""

    replies: list[str]
    context: RunCallContext = field(default_factory=RunCallContext)
    calls: list[tuple[list[BaseMessage], CallAttribution]] = field(default_factory=list)
    inference_kind: str = "chat"
    model: str = "scripted"
    name: str = "scripted"

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion:
        self.calls.append((list(messages), self.context.current))
        return ChatInvokeCompletion(completion=self.replies.pop(0), usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def _agent(summarizer: _Summarizer | None = None) -> M3AAgent:
    """M3A, with *summarizer* bound as Runtime binds its summary model."""
    agent = M3AAgent()
    if summarizer is not None:
        agent.bind_prompt_models([summarizer], summarizer.context)  # type: ignore[list-item]
    return agent


def _text(message: BaseMessage) -> str:
    assert isinstance(message, UserMessage) and isinstance(message.content, list)
    first = message.content[0]
    assert isinstance(first, ContentPartTextParam)
    return first.text


def _images(message: BaseMessage) -> list[bytes]:
    assert isinstance(message, UserMessage) and isinstance(message.content, list)
    import base64

    return [
        base64.b64decode(part.image_url.url.split(",", 1)[1])
        for part in message.content
        if isinstance(part, ContentPartImageParam)
    ]


def _record(
    step: int, reply: str, *, results: Sequence[ActionResult] = (), error: str = ""
) -> StepRecord:
    actions = [Action(name="android.click", params={"index": 0})] if results else []
    return StepRecord(
        step=step,
        llm_output=reply,
        actions=actions,
        action_results=list(results),
        error_phase="parse" if error else "",
        error=error or None,
    )


def _context(step: int) -> ReactPromptContext:
    return ReactPromptContext(step=step, phase="policy", policy_step=step)


# ── One prompt per step, AndroidWorld's ─────────────────────────────


async def test_the_first_prompt_is_androidworlds_with_the_screenshot_and_its_marked_copy() -> None:
    observation = _Observation()

    (message,) = await _agent().build_react_prompt(observation, context=_context(0))

    assert _text(message) == _ANDROIDWORLD["cases"]["action_selection_first_step"]
    raw, marked = _images(message)
    assert [Image.open(io.BytesIO(image)).format for image in (raw, marked)] == ["JPEG", "JPEG"]
    assert raw != marked, "the second carries the marks"


async def test_a_screen_with_no_elements_says_not_available() -> None:
    observation = _Observation(ui_tree=_tree(serialized=""))

    (message,) = await _agent().build_react_prompt(observation, context=_context(0))

    assert _text(message) == _ANDROIDWORLD["cases"]["action_selection_no_elements"]


async def test_the_tasks_own_guidelines_are_written_as_androidworld_writes_them() -> None:
    """The guideline AndroidWorld's runner sets for its MiniWoB tasks, and a second one."""
    agent = M3AAgent(
        task_guidelines=(
            "This task is running in a mock app, you must stay in this app and DO NOT use the "
            "`navigate_home` action.",
            "Answer in one word.",
        )
    )
    observation = _Observation(task=TaskObs(user_request="Click the button."))

    (message,) = await agent.build_react_prompt(observation, context=_context(0))

    assert _text(message) == _ANDROIDWORLD["cases"]["action_selection_with_guidelines"]


async def test_the_history_holds_the_summary_of_a_step_and_m3as_line_for_an_unread_reply() -> None:
    """Three steps: an action that ran (summarized), a reply M3A could not read (its own line,
    no call), then the prompt AndroidWorld writes for that history."""
    summarizer = _Summarizer(["Opened Settings."])
    agent = _agent(summarizer)
    opened = "Reason: Settings has the Wi-Fi toggle.\nAction: {'action_type': 'open_app', 'app_name': 'Settings'}"
    history: list[StepRecord] = []

    await agent.build_react_prompt(_Observation(), context=_context(0))
    history.append(_record(0, opened, results=[ActionResult(success=True)]))
    await agent.build_react_prompt(
        _Observation(history=HistoryObs(items=list(history))), context=_context(1)
    )
    history.append(_record(1, "I am not sure.", error=NOT_IN_FORMAT))
    (message,) = await agent.build_react_prompt(
        _Observation(history=HistoryObs(items=list(history))), context=_context(2)
    )

    assert _text(message) == _ANDROIDWORLD["cases"]["action_selection_with_history"]
    assert len(summarizer.calls) == 1, "no call for the step whose reply could not be read"


async def test_the_summary_is_a_helper_call_with_androidworlds_prompt_and_the_marked_screens() -> (
    None
):
    summarizer = _Summarizer(["Wi-Fi page opened."])
    agent = _agent(summarizer)
    clicked = "Reason: The Wi-Fi row opens its page.\nAction: {'action_type': 'click', 'index': 0}"

    (first,) = await agent.build_react_prompt(_Observation(), context=_context(0))
    after = _Observation(
        ui_tree=_tree(serialized=""),
        history=HistoryObs(items=[_record(0, clicked, results=[ActionResult(success=True)])]),
    )
    (second,) = await agent.build_react_prompt(after, context=_context(1))

    ((call, attribution),) = summarizer.calls
    assert attribution == CallAttribution(caller="agent", purpose="step_summary", step=1)
    (asked,) = call
    assert _text(asked) == _ANDROIDWORLD["cases"]["summary"]
    before, labelled = _images(asked)
    assert before == _images(first)[1], "the marked screenshot the action was chosen on, unlabelled"
    assert labelled != _images(second)[1], "the after-screen carries its label"
    assert (
        'Step 1- Action selected: {"action_type": "click", "index": 0}. Wi-Fi page opened.'
        in _text(second)
    )


async def test_a_step_whose_action_failed_is_written_with_its_error_and_not_summarized() -> None:
    summarizer = _Summarizer([])
    agent = _agent(summarizer)
    clicked = "Reason: r\nAction: {'action_type': 'click', 'index': 7}"
    failed = ActionResult(
        success=False, error="The screen's UI tree has no element 7; its elements are 0, 2."
    )

    await agent.build_react_prompt(_Observation(), context=_context(0))
    (message,) = await agent.build_react_prompt(
        _Observation(history=HistoryObs(items=[_record(0, clicked, results=[failed])])),
        context=_context(1),
    )

    assert summarizer.calls == []
    assert (
        'Step 1- Action selected: {"action_type": "click", "index": 7}. The action failed: '
        "The screen's UI tree has no element 7; its elements are 0, 2."
    ) in _text(message)


async def test_a_summary_without_the_model_runtime_binds_is_refused() -> None:
    agent = M3AAgent()
    clicked = "Reason: r\nAction: {'action_type': 'click', 'index': 0}"
    await agent.build_react_prompt(_Observation(), context=_context(0))

    with pytest.raises(RuntimeError, match="needs the model and call context Runtime binds"):
        await agent.build_react_prompt(
            _Observation(
                history=HistoryObs(
                    items=[_record(0, clicked, results=[ActionResult(success=True)])]
                )
            ),
            context=_context(1),
        )


async def test_a_contribution_goes_where_the_memory_keeps_it() -> None:
    """A turn contribution joins the step's one message, a system one is a system message."""
    context = _context(0)
    context.contributions = [
        PromptContribution(text="Mind the pop-up.", placement="turn"),
        PromptContribution(text="Be brief.", placement="system"),
    ]

    system, message = await _agent().build_react_prompt(_Observation(), context=context)

    assert isinstance(system, SystemMessage) and "Be brief." in str(system.content)
    assert isinstance(message, UserMessage) and isinstance(message.content, list)
    last = message.content[-1]
    assert isinstance(last, ContentPartTextParam) and "Mind the pop-up." in last.text


# ── What M3A asks of a run ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("loop", "window", "refusal"),
    [
        ({"max_actions_per_step": 1}, 1, "max_actions_per_step at 2 or more; it is 1"),
        ({"parse_retries_per_step": 1}, 1, "parse_retries_per_step to 0; it is 1"),
        ({"max_consecutive_parse_errors": 3}, 1, "max_consecutive_parse_errors to null; it is 3"),
        ({}, 2, "sliding_window with window_size 1; it is 2"),
    ],
    ids=["one-action-per-reply", "a-re-ask", "a-limit-on-unread-replies", "a-wider-window"],
)
def test_a_run_that_would_not_run_m3a_as_androidworld_does_is_refused(
    loop: dict[str, int], window: int, refusal: str
) -> None:
    """input_text runs as two actions; an unread reply costs its step, never more, never less;
    the memory keeps the step's one message."""
    runtime = AgentRuntime.for_agent(_assembly(loop=loop, window=window), AGENT_ID)
    runtime.prepare(task_id="assembly")

    with pytest.raises(ValueError, match=refusal):
        runtime.seed_messages("Turn wifi off.")


def test_m3a_takes_no_memory_but_sliding_window() -> None:
    def config(memory: dict[str, object]) -> dict[str, object]:
        android = {
            "capability_id": "android",
            "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
            "service_apk": "spectragent-device.apk",
            "tap_injector": {"injector_id": "shell_injection"},
            "typing_backend": {"typing_id": "set_text"},
            "ui_tree_cleaner": {"cleaner_id": "androidworld"},
            "ui_tree_serializer": {"serializer_id": "m3a"},
        }
        agent = {
            "agent_id": "m3a",
            "agent_loop": {"loop_id": "react"},
            "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
            "memory": memory,
        }
        return {"capability": [android], "agent": [agent]}

    window = config({"memory_id": "sliding_window", "window_size": 1})
    ConfigComposer.for_config(window).validate(window)
    simple = config({"memory_id": "simple"})
    with pytest.raises(ValidationError, match="sliding_window"):
        ConfigComposer.for_config(simple).validate(simple)


def test_a_space_without_an_action_m3as_prompt_teaches_is_refused() -> None:
    runtime = AgentRuntime.for_agent(_assembly(), AGENT_ID)
    runtime.prepare(task_id="assembly")
    narrowed = runtime.action_space.model_copy(
        update={
            "descriptors": [
                d for d in runtime.action_space.descriptors if d.name != "android.open_app"
            ],
            "allowed_actions": runtime.action_space.allowed_actions - {"android.open_app"},
        }
    )
    with pytest.raises(ValueError, match="this run's action space has no open_app"):
        M3AAgent().check_action_space(narrowed)


#: The Loop settings M3A runs on: AndroidWorld's (``M3AAgent.prepare``).
_ANDROIDWORLD_LOOP: dict[str, Any] = {
    "max_actions_per_step": 2,
    "max_consecutive_parse_errors": None,
}


def _assembly(
    tmp_path: Path | None = None, *, loop: dict[str, int] | None = None, window: int = 1
) -> Any:
    """M3A on the Android capability, on a ReAct Loop with AndroidWorld's settings but *loop*."""
    directory = tmp_path or Path(__import__("tempfile").mkdtemp())
    capability = AndroidCapability(service_apk=str(fake_service_apk(directory)))
    env = make_env(AndroidEnvironment, {"android#0": capability})
    return make_run(
        agent=M3AAgent(),
        loop=ReactLoop(**{**_ANDROIDWORLD_LOOP, **(loop or {})}),
        env=env,
        llm=NoModel(),
        memory=SlidingWindowMemory(window_size=window),
    )


def test_m3a_assembles_against_the_android_capability_and_owns_its_summary_model(
    tmp_path: Path,
) -> None:
    run = _assembly(tmp_path)
    runtime = AgentRuntime.for_agent(run, AGENT_ID)

    runtime.prepare(task_id="assembly")

    actions, observation = runtime.negotiated_actions, runtime.negotiated_observation
    assert actions is not None and observation is not None
    assert actions.action_bindings == {
        "click": "android.click",
        "long_press": "android.long_press",
        "type": "android.type",
        "type_coordinate": "android.type_coordinate",
        "scroll": "android.scroll",
        "press_key": "android.press_key",
        "open_app": "android.open_app",
        "wait": "android.wait",
        "done": "loop.done",
    }
    assert {name: observation.fragment_bindings[name] for name in ("screenshot", "ui_tree")} == {
        "screenshot": "android.screenshot",
        "ui_tree": "android.ui_tree",
    }
    descriptors = {d.name: d for d in runtime.action_space.descriptors}
    assert set(descriptors["android.long_press"].params) == {"index"}, "no grid, so no point"
    assert "index" in descriptors["android.scroll"].params
    agent = run.by_layer["agent"][AGENT_ID]
    assert isinstance(agent, M3AAgent) and agent.helper_llms == [agent.llm]
    assert isinstance(agent, PromptModelOwner), "the runtime binds its summary model"
    assert runtime.seed_messages("Turn wifi off.") == [], "every step is one message of its own"


# ── A run on Android ──────────────────────────────────────────────────


async def test_a_run_types_into_an_element_presses_enter_summarizes_and_claims_success(
    tmp_path: Path,
) -> None:
    """input_text runs as a tap at the element's centre, the text and Enter; the next step's
    prompt carries the summary, recorded as a helper call; status complete ends the run."""
    phone = ScriptedPhone(screen=_png(), text_field=True, field_bounds=(0, 450, 1080, 600))
    phone.forest = settings_forest(
        ("Network & internet", (0, 300, 1080, 450)), ("Search settings", (0, 450, 1080, 600))
    )
    model = ScriptedModel(
        "Reason: Search for it.\nAction: {'action_type': 'input_text', 'text': 'wifi', 'index': 1}",
        "Typed wifi and searched.",
        "Reason: Done.\nAction: {'action_type': 'status', 'goal_status': 'complete'}",
    )
    async with phone_server(phone) as adb:
        capability = AndroidCapability(
            service_apk=str(fake_service_apk(tmp_path)), settle_max_wait_s=0
        )
        configured(capability, adb)
        env = make_env(AndroidEnvironment, {"android#0": capability})
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        run = make_run(
            agent=M3AAgent(),
            loop=ReactLoop(**_ANDROIDWORLD_LOOP),
            env=env,
            llm=model,
            memory=SlidingWindowMemory(window_size=1),
            hooks=[writer],
        )
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute(_GOAL, timeout=30)

    assert result.agent_done == "done", result.error
    assert result.agent_reported_success is True and result.answer == ""
    assert phone.inputs[1:] == [
        "helper stroke (540,525)@0 (540,525)@50",
        "service set_text 'wifi' at the cursor",
        "helper key KEYCODE_ENTER",
    ]
    decision, summary, last = model.calls
    assert [len(call) for call in (decision, last)] == [1, 1], "each step is one message"
    assert "Step 1- Action selected:" in _text(last[0]) and "Typed wifi and searched." in _text(
        last[0]
    )
    assert "Now I want you to summerize the latest step." in _text(summary[0])
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert [(call.step, call.caller, call.purpose) for call in trace.call_metrics(1)] == [
        (1, "agent", "step_summary"),
        (1, "agent", "agent_decision"),
    ]


# ── Its prompt tree (``test_prompt_tree.py``'s invariants, per step) ──


async def _rendered_in_two_steps() -> list[tuple[str, dict[str, Any]]]:
    """Every template M3A renders in a step and in the next, which summarizes it, with its params."""
    calls: list[tuple[str, dict[str, Any]]] = []
    original = AgentBase.render

    def spy(self: AgentBase, name: str, params: dict[str, Any]) -> Any:
        calls.append((name, dict(params)))
        return original(self, name, params)

    agent = _agent(_Summarizer(["Opened Settings."]))
    opened = "Reason: r\nAction: {'action_type': 'open_app', 'app_name': 'Settings'}"
    try:
        AgentBase.render = spy  # type: ignore[method-assign]
        await agent.build_react_prompt(_Observation(), context=_context(0))
        await agent.build_react_prompt(
            _Observation(
                history=HistoryObs(items=[_record(0, opened, results=[ActionResult(success=True)])])
            ),
            context=_context(1),
        )
    finally:
        AgentBase.render = original  # type: ignore[method-assign]
    return calls


async def test_every_template_variable_is_supplied_and_every_supplied_param_rendered() -> None:
    rendered = await _rendered_in_two_steps()

    assert [name for name, _ in rendered] == ["action_selection", "summary", "action_selection"]
    for name, params in rendered:
        template, ref = load_prompt("m3a", name)
        assert template_variables(M3AAgent.__prompt_engine__, template) == set(params), name
        assert ref.path == f"agents/m3a/{name}.md" and len(ref.content_hash) == 64


async def test_every_template_on_disk_is_reached() -> None:
    reached = {name for name, _ in await _rendered_in_two_steps()}
    assert reached == set(agent_prompt_variants("m3a"))
