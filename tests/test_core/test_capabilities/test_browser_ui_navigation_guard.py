"""The opt-in UI protocol is a before-act veto, including the real Runtime/WAL seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.aop.weaver import Weaver
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.actions import (
    SendKeysAction,
    TypeAction,
    TypeCoordinateAction,
)
from spectragent.core.capabilities.browser.hooks.ui_navigation import (
    UiNavigationGuard,
    navigation_violation,
)
from spectragent.core.composer import ConfigComposer
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops.errors import RunAbort
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_run
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser
from tests._helpers.fragment_samples import PNG_BYTES


class _Environment:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    async def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return ActionResult(success=True)


@pytest.mark.parametrize("name", ["browser.navigate", "browser.create_tab"])
async def test_before_act_veto_never_calls_the_environment(name: str) -> None:
    env = _Environment()
    wrapped = Weaver([UiNavigationGuard()]).wrap(env, role="env")
    with pytest.raises(RunAbort, match="direct URL navigation") as caught:
        await wrapped.act(action=Action(name=name, params={"url": "https://example.com/"}))
    assert caught.value.source == "ui_navigation_guard"
    assert not env.actions


async def test_relative_action_is_qualified_by_the_existing_guard_before_the_hook() -> None:
    env = _Environment()
    wrapped = Weaver([UiNavigationGuard()]).wrap(env, role="env")
    _, act = build_guarded_act(
        wrapped.act,
        [ActionDescriptor(name="browser.navigate", description="Navigate")],
        bindings={"navigate": "browser.navigate"},
    )
    action = Action(name="navigate", params={"url": "https://example.com/"})
    with pytest.raises(RunAbort):
        await act(action)
    assert action.name == "browser.navigate" and not env.actions


@pytest.mark.parametrize(
    "keys",
    [
        "Ctrl+L",
        "control+l",
        "CTRL + L",
        "Cmd+L",
        "Meta+l",
        "Command+L",
        "⌘+L",
        "Control_L+l",
        "ControlRight+l",
        "MetaLeft+l",
        "Keys.CONTROL+l",
        "Super+l",
        "Win+l",
        "ControlOrMeta+l",
        "CtrlOrCmd+l",
        "^+l",
        "Alt+D",
        "Option+d",
        "⌥+d",
        "Alt_L+d",
        "F6",
        "f6",
        "Shift+F6",
        "Ctrl+t",
        "cmd+t",
        "Meta+t",
        "Ctrl+n",
        "Cmd+N",
        "Meta+n",
        "Ctrl+Shift+n",
        "Command+Shift+n",
        "Ctrl+k",
        "Meta+e",
    ],
)
async def test_navigation_shortcut_spellings_are_vetoed_under_the_real_keys_contract(
    keys: str,
) -> None:
    params = SendKeysAction.Params(keys=keys).model_dump()
    env = _Environment()
    wrapped = Weaver([UiNavigationGuard()]).wrap(env, role="env")
    with pytest.raises(RunAbort, match="shortcuts are prohibited"):
        await wrapped.act(Action(name="browser.send_keys", params=params))
    assert not env.actions


@pytest.mark.parametrize("spelling", ["'ctrl l'", "['cmd', 'l']", "'Alt + D'", "'F6'"])
async def test_ui_tars_hotkey_normalization_reaches_the_same_veto(spelling: str) -> None:
    agent = UITarsAgent()
    agent.prepare(
        task="Use page controls",
        action_descriptors=[SendKeysAction().descriptor()],
        execution_limits=ExecutionLimits(max_actions_per_step=1),
    )
    actions = agent.parse_react_output(f"Action: hotkey(key={spelling})")
    assert len(actions) == 1
    env = _Environment()
    wrapped = Weaver([UiNavigationGuard()]).wrap(env, role="env")
    _, act = build_guarded_act(
        wrapped.act, [SendKeysAction().descriptor()], bindings={"send_keys": "browser.send_keys"}
    )
    with pytest.raises(RunAbort):
        await act(actions[0])
    assert not env.actions


@pytest.mark.parametrize(
    "keys",
    [
        "Ctrl+A",
        "control+c",
        "Cmd+V",
        "Meta+x",
        "Ctrl+z",
        "Cmd+f",
        "Enter",
        "Escape",
        "Tab",
        "ArrowDown",
        "a",
        "l",
        "t",
        "n",
    ],
)
async def test_page_editing_and_ordinary_keys_remain_allowed(keys: str) -> None:
    env = _Environment()
    wrapped = Weaver([UiNavigationGuard()]).wrap(env, role="env")
    result = await wrapped.act(Action(name="browser.send_keys", params={"keys": keys}))
    assert result.success and len(env.actions) == 1


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/search?q=test",
        "  http://example.com/a\n",
        "//example.com/page",
        "example.com",
        "www.example.com/search?q=weather",
        "example.com:8080/item/1",
        "localhost:3000/search",
        "127.0.0.1:8080/a",
        "[::1]:8080/a",
        "例子.中国/新闻",
        "chrome://newtab",
        "about:blank",
        "file:///tmp/page.html",
        "ftp://example.com/file",
        "javascript:location.href='https://example.com'",
        "data:text/html,hello",
    ],
)
@pytest.mark.parametrize("coordinate", [False, True])
def test_whole_url_or_host_input_is_refused_for_both_typing_contracts(
    text: str, coordinate: bool
) -> None:
    params = (
        TypeCoordinateAction.Params(coordinate_x=10, coordinate_y=20, text=text)
        if coordinate
        else TypeAction.Params(index=1, text=text)
    )
    name = "browser.type_coordinate" if coordinate else "browser.type"
    assert navigation_violation(Action(name=name, params=params.model_dump())) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Search for example.com",
        "This is a normal sentence. Please continue.",
        "https://example.com is the source",
        "alice@example.com",
        "version 1.2.3",
        "v1.2.3",
        "3.14",
        "e.g.",
        "a.b",
        "alpha/beta",
        "北京天气",
        "今天天气不错。明天再看。",
        "",
        "hello\nworld",
    ],
)
def test_normal_page_text_is_not_rejected_for_having_a_dot(text: str) -> None:
    assert (
        navigation_violation(Action(name="browser.type", params={"index": 1, "text": text})) is None
    )


@pytest.mark.parametrize(
    "action",
    [
        Action(name="browser.click", params={"index": 1}),
        Action(name="browser.click_coordinate", params={"coordinate_x": 5, "coordinate_y": 6}),
        Action(name="browser.go_back"),
        Action(name="browser.go_forward"),
        Action(name="browser.switch_tab", params={"target_id": "existing"}),
        Action(name="browser.scroll", params={"direction": "down"}),
        Action(name="browser.drag", params={"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4}),
        Action(name="browser.select_dropdown", params={"index": 1, "text": "example.com"}),
        Action(name="tool_sandbox.navigate", params={"url": "https://example.com/"}),
    ],
)
async def test_page_interactions_and_other_capabilities_are_unaffected(action: Action) -> None:
    env = _Environment()
    result = await Weaver([UiNavigationGuard()]).wrap(env, role="env").act(action)
    assert result.success and env.actions == [action]


def test_yaml_explicitly_selects_the_browser_hook_and_rejects_a_typo() -> None:
    composer = ConfigComposer(registrars=collect_registrars())
    run = composer.compose(yaml.safe_load("hook:\n  - hook_id: ui_navigation_guard\n"))
    assert isinstance(run.single("hook"), UiNavigationGuard)
    assert composer.compose({}).by_layer.get("hook", {}) == {}, "the guard is opt-in"
    with pytest.raises(ValidationError):
        composer.compose(yaml.safe_load("hook:\n  - hook_id: ui_navigation_guarrd\n"))


class _Reply:
    inference_kind = "chat"
    model = "fixed-local-reply"

    async def ainvoke(self, _messages: Any, **_kwargs: Any) -> ChatInvokeCompletion:
        return ChatInvokeCompletion(completion="Action: hotkey(key='ctrl+l')", usage=None)


async def test_runtime_setup_is_allowed_but_action_veto_is_persisted_before_env_act(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = FakeBrowser()
    browser.screenshot = PNG_BYTES
    cap = BrowserCapability(settle_max_wait_s=0)
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    env = BrowserEnvironment(capabilities={"browser#0": cap})
    executed: list[Action] = []
    original = env.act

    async def track(action: Action, **kwargs: Any) -> ActionResult:
        executed.append(action)
        return await original(action, **kwargs)

    monkeypatch.setattr(env, "act", track)
    writer = TraceWriter(trace_dir=str(tmp_path))
    run = make_run(
        agent=UITarsAgent(),
        loop=ReactLoop(max_steps=2),
        env=env,
        llm=_Reply(),
        memory=SimpleMemory(),
        hooks=[UiNavigationGuard(), writer],
    )
    start_url = "https://example.com/start"
    with pytest.raises(RunAbort, match="ui_navigation_guard"):
        await AgentRuntime.for_agent(run, AGENT_ID).execute(
            "Use the page",
            task_id="page-task",
            env_context={"start_url": start_url},
            raise_on_error=False,
        )
    assert not executed
    assert any(
        s.method == "Page.navigate" and s.params.get("url") == start_url
        for client in browser.clients
        for s in client.sent
    ), "setup must still open the given start page"
    manifests = list(tmp_path.glob("*/manifest.json"))
    assert len(manifests) == 1
    trace = RunTrace(manifests[0].parent)
    assert trace.result is not None and trace.result.agent_done is AgentDoneStatus.VETOED
    assert "ui_navigation_guard" in (trace.result.error or "")
    assert trace.manifest.status == "complete"
    assert len(trace.step_records) == 1
    step = trace.step_records[0]
    assert step.error_phase == "act" and "ui_navigation_guard" in (step.error or "")
    assert step.actions[0].name == "browser.send_keys"
