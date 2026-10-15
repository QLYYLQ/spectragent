"""A browser-shaped fake site for branching the real browser agents offline.

The real ``browser.dom`` / ``browser.screenshot`` / ``browser.browser_meta``
fragment classes and the real browser action descriptors, served by a
capability whose page is a function of how many clicks happened — so a
restored prefix reproduces the source's pages exactly. ``id_offset`` shifts
every element handle, which is what a relaunched browser does to the
``backendNodeId`` an index names; the capability implements
``RestorationSupport`` by position so a recorded index is re-found, and
verifies by URL. The screenshot does not depend on the offset (pinned layout).

The capability answers to ``capability_id == "browser"`` without registering
under that key (the real ``BrowserCapability`` owns it): ``__abstract__``.
"""

from __future__ import annotations

import dataclasses
import io
import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMRect,
    DOMState,
    ScrollInfo,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    BrowserPlatformObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.restoration import Checkpoint, RestorationEvidence, Retarget
from tests._helpers.assembly import AGENT_ID, make_env, make_run

VIEWPORT = (1280, 720)
IMAGE = (64, 36)
TASK = "put the blue kettle in the cart"
LABELS = ("Blue kettle", "Red kettle", "Checkout")


@cache
def _descriptors() -> tuple[ActionDescriptor, ...]:
    from spectragent.core.capabilities.browser.capability import BrowserCapability

    return tuple(action.descriptor() for action in BrowserCapability().actions)


@cache
def _png(clicks: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", IMAGE)
    pixels = image.load()
    assert pixels is not None
    for y in range(IMAGE[1]):
        for x in range(IMAGE[0]):
            pixels[x, y] = ((x * 7 + clicks * 40) % 256, (y * 11) % 256, (x * y + clicks) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def page_dom(clicks: int, offset: int) -> DOMObs:
    """Three buttons with handles ``offset + 1 .. 3`` and a cart line."""
    html = DOMElementNode(tag_name="html", backend_node_id=offset)
    body = DOMElementNode(tag_name="body", backend_node_id=offset + 10, parent=html)
    html.children.append(body)
    selector_map: dict[int, DOMElementNode] = {}
    lines = []
    for position, label in enumerate(LABELS):
        handle = offset + position + 1
        button = DOMElementNode(
            tag_name="button",
            backend_node_id=handle,
            attributes={"id": f"b{position}"},
            text_content=label,
            bounds=DOMRect(x=20.0, y=100.0 + 40 * position, width=120.0, height=30.0),
            element_index=handle,
            is_interactive=True,
            parent=body,
        )
        body.children.append(button)
        selector_map[handle] = button
        lines.append(f"[{handle}]<button id=b{position}>{label}</button>")
    lines.append(f"<p>cart: {clicks}</p>")
    state = DOMState(
        dom_tree=html,
        selector_map=selector_map,
        scroll_info=ScrollInfo(viewport_width=VIEWPORT[0], viewport_height=VIEWPORT[1]),
        serialized_dom="\n".join(lines),
    )
    return DOMObs(element_tree=state, selector_map=dict(selector_map))


class FakeBrowserCap(CapabilityBase):
    """The site. State is set by :func:`make_browser`."""

    __abstract__ = True

    clicks: int
    cycle: int
    id_offset: int
    log: list[str]

    @property
    def capability_id(self) -> str:
        return "browser"

    @property
    def declared_fragments(self) -> frozenset[str]:
        return frozenset(
            {"browser.dom", "browser.screenshot", "browser.browser_meta", "browser.platform"}
        )

    @property
    def provided_actions(self) -> frozenset[str]:
        return frozenset(d.name for d in _descriptors())

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return list(_descriptors())

    async def begin_observation(self, cycle: int) -> None:
        self.cycle = cycle

    async def resolve_fragment(self, fragment_key: str) -> Any:
        if fragment_key == "browser.platform":
            return BrowserPlatformObs(operating_system="macos")
        if fragment_key == "browser.dom":
            return page_dom(self.clicks, self.id_offset)
        if fragment_key == "browser.screenshot":
            return ScreenshotObs(
                image_bytes=_png(self.clicks),
                viewport_width=VIEWPORT[0],
                viewport_height=VIEWPORT[1],
                image_width=IMAGE[0],
                image_height=IMAGE[1],
            )
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(
                url=f"https://shop.test/cart/{self.clicks}", page_title=f"Shop ({self.clicks})"
            )
        raise KeyError(fragment_key)

    async def execute_action(self, action: Action) -> ActionResult:
        self.log.append(f"{action.name}({action.params})")
        name = action.name
        if name == "browser.done":
            return ActionResult(
                success=True,
                is_done=True,
                extracted_content=str(action.params.get("extracted_content", "")),
            )
        if name == "browser.click":
            index = action.params.get("index")
            if not isinstance(index, int) or not 1 <= index - self.id_offset <= 3:
                return ActionResult(success=False, error=f"no element {index}")
        self.clicks += 1
        return ActionResult(success=True)

    # ── RestorationSupport ──

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        if action.name != "browser.click" or "index" not in action.params:
            return Retarget(action=action.model_copy(deep=True), method="identity")
        before = sorted(recorded.dom.selector_map)
        now = sorted(restored.dom.selector_map)
        index = action.params["index"]
        if index not in before:
            return Retarget(action=None, method="geometry_signature", failure="not_found")
        moved = {**action.params, "index": now[before.index(index)]}
        return Retarget(action=Action(name=action.name, params=moved), method="geometry_signature")

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        # Only what this run negotiated: a vision-only agent has no meta.
        before = getattr(recorded, "browser_meta", None)
        after = getattr(restored, "browser_meta", None)
        if before is None or after is None:
            return RestorationEvidence(capability_id="browser")
        recorded_url, restored_url = before.url, after.url
        return RestorationEvidence(
            capability_id="browser",
            url_match=recorded_url == restored_url,
            recorded_url=recorded_url,
            restored_url=restored_url,
        )


def make_browser(*, id_offset: int = 100) -> FakeBrowserCap:
    cap = FakeBrowserCap()
    cap.clicks = 0
    cap.cycle = 0
    cap.id_offset = id_offset
    cap.log = []
    return cap


class FakeBrowserEnv(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "branch_fake_browser_env"


class ScriptedTeacher:
    """Replies from a script; a dict is validated into the call's output model."""

    model = "scripted-teacher"
    name = "scripted-teacher"
    model_type = "test"
    provider = "test"

    async def aclose(self, *, timeout: float = 5.0) -> tuple:
        return ()

    def __init__(self, replies: Sequence[Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[list[Any]] = []

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        if not self.replies:
            raise AssertionError("the scripted teacher has no reply left")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if output_format is not None:
            return ChatInvokeCompletion(completion=output_format.model_validate(reply), usage=None)
        return ChatInvokeCompletion(completion=reply, usage=None)


AgentKind = Literal["browser_use", "skyvern", "ui_tars"]


def make_agent(kind: AgentKind) -> Any:
    if kind == "browser_use":
        return BrowserUseAgent(observation_mode="dom_vision")
    if kind == "skyvern":
        return SkyvernAgent()
    return UITarsAgent()


def make_memory(kind: str) -> Any:
    if kind == "sliding_window":
        return SlidingWindowMemory(window_size=4)
    return SimpleMemory(max_messages=1000)


class _MemoryEntry(BaseModel):
    memory_id: Literal["simple", "sliding_window"]


def _config(agent: Any, memory: str) -> Any:
    """The resolved config a Composer would record: the agent's closed-set knobs."""
    fields: dict[str, Any] = {"agent_id": (Literal[agent.agent_id], agent.agent_id)}  # type: ignore[valid-type]
    if isinstance(agent, BrowserUseAgent):
        fields["observation_mode"] = (
            Literal["dom", "dom_vision", "vision"],
            agent.observation_mode,
        )
    from pydantic import create_model

    entry = create_model("AgentEntry", memory=(_MemoryEntry, ...), **fields)
    top = create_model("Config", agent=(list[entry], ...))  # type: ignore[valid-type]
    return top(agent=[entry(memory=_MemoryEntry(memory_id=memory))])  # type: ignore[call-arg]


@dataclasses.dataclass
class Outcome:
    result: AgentResult | None
    run_dir: Path | None
    teacher: ScriptedTeacher
    agent: Any
    cap: FakeBrowserCap
    prompts_built: list[Any]


async def drive(
    trace_root: Path,
    *,
    kind: AgentKind,
    memory: str,
    loop: Any,
    replies: Sequence[Any],
    run_id: str,
    id_offset: int = 100,
    checkpoint: Checkpoint | None = None,
) -> Outcome:
    """One run of a real browser agent against the fake site, traced."""
    agent = make_agent(kind)
    built: list[Any] = []
    original = agent.build_react_prompt

    async def spy(observation: Any, *, context) -> Any:
        messages = await original(observation, context=context)
        text = "\n".join(str(message.content) for message in messages)
        history = re.search(r"<action_history>\n(.*?)\n</action_history>", text, flags=re.S)
        snapshot = history.group(1).splitlines() if history else []
        built.append((messages, snapshot))
        return messages

    agent.build_react_prompt = spy
    cap = make_browser(id_offset=id_offset)
    teacher = ScriptedTeacher(replies)
    writer = TraceWriter(trace_dir=str(trace_root))
    run = make_run(
        agent=agent,
        loop=loop,
        env=make_env(FakeBrowserEnv, {"browser": cap}),
        llm=teacher,
        memory=make_memory(memory),
        hooks=[writer],
    )
    run = dataclasses.replace(run, top_level_cfg=_config(agent, memory))
    outcome = Outcome(None, None, teacher, agent, cap, built)
    try:
        outcome.result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            TASK,
            task_id=run_id,
            run_id=run_id,
            timeout=60,
            loop_inputs={"checkpoint": checkpoint} if checkpoint is not None else None,
        )
    finally:
        outcome.run_dir = writer.run_dir
    return outcome


def source_loop() -> ReactLoop:
    return ReactLoop(
        max_steps=10, max_actions_per_step=1, parse_retries_per_step=1, today_override="2026-10-11"
    )


def branch_loop() -> BranchLoop:
    return BranchLoop(max_actions_per_step=1, parse_retries_per_step=1)
