"""Offline fakes for branch runs: a deterministic site, an agent, a scripted teacher.

Everything a ``BranchLoop`` test needs without a browser or a model:

- :class:`BranchPage` / :class:`BranchFakeCap` — a capability whose page is a
  function of the clicks so far (so a restored prefix reproduces it exactly),
  whose element ids can be shifted (``id_offset``: a "relaunched" world where
  the same element has a fresh id), and which implements
  ``RestorationSupport`` by position (``retarget``) and URL (``verify``);
- :class:`BranchFakeAgent` — a ReAct agent with a text (or structured) reply
  syntax, a closed-set knob (``style``), a numeric knob its prompt never
  shows (``patience``) and agent state rebuilt only by replaying its parser
  (``parsed_log``, Skyvern's ``_action_history`` in miniature);
- :class:`ScriptedLLM` — replies from a script; a reply may be text, a dict
  for the structured model, or an exception to raise;
- :func:`record_source` / :func:`run_branch` — a source run through the real
  ``AgentRuntime`` + ``ReactLoop`` + ``TraceWriter``, and a branch of it.

The fake capability is built by :func:`make_cap`, never by an ``__init__`` of
its own: that would hide the inherited ``id`` knob from conscribe
(``tests/test_generated/test_no_dropped_annotated_fields.py``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.memory.sliding_window import SlidingWindowMemory
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.loops.errors import ParseError, RunAbort
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.restoration import (
    BranchProvenance,
    Checkpoint,
    RestorationEvidence,
    Retarget,
)
from tests._helpers.assembly import AGENT_ID as RUN_AGENT_ID
from tests._helpers.assembly import make_env, make_run

CAP_ID = "branchfake"
PAGE_KEY = f"{CAP_ID}.page"
CLICK = f"{CAP_ID}.click"
DONE = f"{CAP_ID}.done"
FAKE_AGENT_ID = "branchfake_agent"
TASK = "buy the blue kettle"


class BranchPage(FragmentBase):
    """The fake site's page: its URL, its clickable element ids, the cycle that saw it."""

    __registry_key__ = PAGE_KEY
    __source__: ClassVar[Literal["env", "loop"]] = "env"

    url: str = ""
    elements: list[int] = Field(default_factory=list)
    cycle: int = 0

    def summary(self) -> str:
        return self.url


class BranchFakeEnv(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "branchfake_env"


class BranchFakeCap(CapabilityBase):
    """A deterministic site: the URL counts the clicks, the elements are three ids.

    State (set by :func:`make_cap`): ``clicks`` (element positions clicked so
    far), ``cycle``, ``id_offset`` (element ids are ``offset + 0..2``),
    ``urls`` (cycle → forced URL, to make a restored step diverge),
    ``veto_act_at`` (a ``RunAbort`` raised by the click on this position),
    ``log`` (every action executed), ``setups``.
    """

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = CAP_ID
    __wiring__ = {
        "action": ("action", [CLICK, DONE]),
        "observation": ("observation", [PAGE_KEY]),
    }

    clicks: list[int]
    cycle: int
    id_offset: int
    urls: dict[int, str]
    veto_act_at: int | None
    log: list[str]
    setups: int
    #: The ``replay_archive`` a branch task's environment context handed
    #: ``setup`` (the source run directory) — accepted the way the browser
    #: accepts it; the fake's pages are deterministic without it.
    replay_archive: str | None

    async def setup(self, *, negotiation: Any = None, replay_archive: str | None = None) -> None:
        self.setups += 1
        self.replay_archive = replay_archive
        await super().setup(negotiation=negotiation)

    async def begin_observation(self, cycle: int) -> None:
        self.cycle = cycle

    def page(self) -> BranchPage:
        url = self.urls.get(self.cycle, f"https://site.test/{len(self.clicks)}")
        return BranchPage(
            url=url, elements=[self.id_offset + n for n in range(3)], cycle=self.cycle
        )

    async def resolve_fragment(self, fragment_key: str) -> Any:
        return self.page()

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(name=CLICK, description="Click an element", params={"index": "int"}),
            ActionDescriptor(name=DONE, description="Finish", params={"answer": "str"}),
        ]

    async def execute_action(self, action: Action) -> ActionResult:
        self.log.append(f"{action.name}({action.params})")
        if action.name == DONE:
            return ActionResult(
                success=True, is_done=True, extracted_content=str(action.params.get("answer", ""))
            )
        index = action.params.get("index")
        position = index - self.id_offset if isinstance(index, int) else None
        if position is None or not 0 <= position < 3:
            return ActionResult(success=False, error=f"no element {index}")
        if self.veto_act_at == position:
            raise RunAbort(f"element {position} is a trap", source="test_guard")
        self.clicks.append(position)
        return ActionResult(success=True, extracted_content=f"clicked {position}")

    # ── RestorationSupport ──

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        if action.name != CLICK:
            return Retarget(action=action.model_copy(deep=True), method="identity")
        before, now = recorded.page.elements, restored.page.elements
        index = action.params.get("index")
        if index not in before:
            return Retarget(action=None, method="geometry_signature", failure="not_found")
        moved = Action(
            name=action.name, params={**action.params, "index": now[before.index(index)]}
        )
        return Retarget(action=moved, method="geometry_signature", detail="by position")

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        return RestorationEvidence(
            capability_id=CAP_ID,
            url_match=recorded.page.url == restored.page.url,
            recorded_url=recorded.page.url,
            restored_url=restored.page.url,
        )


def make_cap(*, id_offset: int = 100, urls: dict[int, str] | None = None) -> BranchFakeCap:
    cap = BranchFakeCap()
    cap.clicks = []
    cap.cycle = 0
    cap.id_offset = id_offset
    cap.urls = dict(urls or {})
    cap.veto_act_at = None
    cap.log = []
    cap.setups = 0
    cap.replay_archive = None
    return cap


class Reply(BaseModel):
    """The structured agent's reply model."""

    text: str


class BranchFakeAgent:
    """A ReAct agent for the fake site.

    Replies are ``click <id> [<id> …]`` or ``done <answer>``; anything else
    is a ``ParseError``. ``parsed_log`` is agent state that only replaying the
    parser rebuilds, and it is rendered into every prompt.
    """

    agent_id = FAKE_AGENT_ID
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["page", "task"], ["step_info", "history", "action_result"]),
        "action": ("action", ["click", "done"]),
    }

    def __init__(
        self, *, style: str = "plain", structured: bool = False, patience: int = 3
    ) -> None:
        self.style = style
        self.structured = structured
        #: A numeric knob nothing renders: only the assembly check can see a
        #: branch configured differently.
        self.patience = patience
        self.parsed_log: list[str] = []
        self.prompts = 0

    def prepare(
        self, task: str, action_descriptors: list[ActionDescriptor], max_actions_per_step: int = 1
    ) -> list[Any]:
        self.parsed_log = []
        names = ", ".join(sorted(d.name for d in action_descriptors))
        return [
            SystemMessage(
                content=(
                    f"[{self.style}] task: {task}; actions: {names}; at most "
                    f"{max_actions_per_step} per step"
                )
            )
        ]

    def build_react_prompt(self, obs: Any) -> list[Any]:
        self.prompts += 1
        info = obs.step_info
        history = [[a.name for a in item.actions] for item in obs.history.items]
        last = obs.action_result
        return [
            UserMessage(
                content=(
                    f"step {info.step}/{info.max_steps} {info.today} | {obs.page.url} "
                    f"{obs.page.elements} c{obs.page.cycle} | history {history} | last "
                    f"{last.last_action}:{last.success} | parsed {self.parsed_log}"
                )
            )
        ]

    def get_react_output_model(self) -> type[BaseModel] | None:
        return Reply if self.structured else None

    def parse_react_output(self, raw: Any) -> list[Action]:
        text = raw.text if isinstance(raw, Reply) else str(raw)
        words = text.split()
        if not words:
            raise ParseError("empty reply")
        verb, args = words[0], words[1:]
        if verb == "click" and args and all(arg.isdigit() for arg in args):
            actions = [Action(name="click", params={"index": int(arg)}) for arg in args]
        elif verb == "done":
            actions = [Action(name="done", params={"answer": " ".join(args)})]
        else:
            raise ParseError(f"cannot read {text!r}")
        self.parsed_log.append(text)
        return actions


class ScriptedLLM:
    """A teacher that replies from a script and remembers every prompt.

    A reply is text; a dict (validated into the call's ``output_format``); or an
    exception, raised. Text for a structured call is wrapped as ``{"text": …}``.
    """

    model = "scripted-teacher"

    #: What every reply is billed: a source with real token counts, so a
    #: restored copy of its steps can be seen not to carry them.
    USAGE = ChatInvokeUsage(
        prompt_tokens=10,
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=2,
        total_tokens=12,
    )

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
            payload = reply if isinstance(reply, dict) else {"text": reply}
            return ChatInvokeCompletion(
                completion=output_format.model_validate(payload), usage=self.USAGE
            )
        return ChatInvokeCompletion(completion=reply, usage=self.USAGE)


class _MemoryEntry(BaseModel):
    memory_id: Literal["simple", "sliding_window"]
    max_messages: int | None = None
    window_size: int | None = None


class _LoopEntry(BaseModel):
    loop_id: str
    max_steps: int
    max_actions_per_step: int


class _AgentEntry(BaseModel):
    agent_id: Literal["branchfake_agent", "other_agent"] = FAKE_AGENT_ID
    style: Literal["plain", "fancy"] = "plain"
    structured: bool = False
    patience: int = 3
    agent_loop: _LoopEntry
    memory: _MemoryEntry


class FakeConfig(BaseModel):
    """The resolved configuration a Composer would have recorded."""

    agent: list[_AgentEntry]


def memory_of(kind: str, *, window: int = 4) -> Any:
    if kind == "sliding_window":
        return SlidingWindowMemory(window_size=window)
    return SimpleMemory(max_messages=1000)


@dataclasses.dataclass
class BranchRunOutcome:
    result: AgentResult | None
    run_dir: Path | None
    llm: ScriptedLLM
    cap: BranchFakeCap
    agent: BranchFakeAgent


def build_run(
    trace_root: Path,
    *,
    loop: Any,
    replies: Sequence[Any],
    agent: BranchFakeAgent,
    cap: BranchFakeCap,
    memory: str = "simple",
    window: int = 4,
    config_agent_id: str = FAKE_AGENT_ID,
    env_cls: type[EnvironmentBase] = BranchFakeEnv,
) -> tuple[Any, TraceWriter, ScriptedLLM]:
    """One composed stack on the fake site — what a Composer would build — and its writer and teacher.

    ``top_level_cfg`` is the resolved configuration a Composer records, so a
    branch of a run built here can be checked against it (Q10).
    """
    writer = TraceWriter(trace_dir=str(trace_root))
    llm = ScriptedLLM(replies)
    memory_strategy = memory_of(memory, window=window)
    run = make_run(
        agent=agent,
        loop=loop,
        env=make_env(env_cls, {CAP_ID: cap}),
        llm=llm,
        memory=memory_strategy,
        hooks=[writer],
    )
    config = FakeConfig(
        agent=[
            _AgentEntry(
                agent_id=config_agent_id,  # type: ignore[arg-type]
                style=agent.style,  # type: ignore[arg-type]
                structured=agent.structured,
                patience=agent.patience,
                agent_loop=_LoopEntry(
                    loop_id=loop.loop_id,
                    max_steps=loop.max_steps,
                    max_actions_per_step=loop.max_actions_per_step,
                ),
                memory=_MemoryEntry(
                    memory_id=memory,  # type: ignore[arg-type]
                    max_messages=getattr(memory_strategy, "max_messages", None),
                    window_size=getattr(memory_strategy, "window_size", None),
                ),
            )
        ]
    )
    return dataclasses.replace(run, top_level_cfg=config), writer, llm


async def _drive(
    trace_root: Path,
    *,
    loop: Any,
    replies: Sequence[Any],
    agent: BranchFakeAgent,
    cap: BranchFakeCap,
    memory: str,
    window: int,
    run_id: str,
    task_id: str,
    loop_inputs: dict[str, Any] | None,
    branch: BranchProvenance | None,
    config_agent_id: str = FAKE_AGENT_ID,
    raise_on_error: bool = True,
    env_cls: type[EnvironmentBase] = BranchFakeEnv,
    env_context: dict[str, Any] | None = None,
    action_filter: list[str] | None = None,
) -> BranchRunOutcome:
    run, writer, llm = build_run(
        trace_root,
        loop=loop,
        replies=replies,
        agent=agent,
        cap=cap,
        memory=memory,
        window=window,
        config_agent_id=config_agent_id,
        env_cls=env_cls,
    )
    outcome = BranchRunOutcome(result=None, run_dir=None, llm=llm, cap=cap, agent=agent)
    try:
        outcome.result = await AgentRuntime.for_agent(run, RUN_AGENT_ID).execute(
            TASK,
            task_id=task_id,
            benchmark="branchfake",
            subset="all",
            run_id=run_id,
            timeout=30,
            loop_inputs=loop_inputs,
            branch=branch,
            raise_on_error=raise_on_error,
            env_context=env_context,
            action_filter=action_filter,
        )
    finally:
        outcome.run_dir = writer.run_dir
    return outcome


async def record_source(
    tmp_path: Path,
    replies: Sequence[Any],
    *,
    loop: ReactLoop | None = None,
    style: str = "plain",
    structured: bool = False,
    memory: str = "simple",
    window: int = 4,
    run_id: str = "src",
    branch: BranchProvenance | None = None,
    env_context: dict[str, Any] | None = None,
    action_filter: list[str] | None = None,
) -> Path:
    """Run the fake agent through the real runtime and trace writer; return the run dir.

    ``env_context`` is the task's environment setup context (recorded as the
    source's ``task_setup``); ``None`` records none. ``action_filter`` is
    ``AgentRuntime.execute``'s ablation allowlist.
    """
    outcome = await _drive(
        tmp_path / "sources",
        loop=loop or ReactLoop(max_steps=10, parse_retries_per_step=1, today_override="2001-02-03"),
        replies=replies,
        agent=BranchFakeAgent(style=style, structured=structured),
        cap=make_cap(),
        memory=memory,
        window=window,
        run_id=run_id,
        task_id="src-task",
        loop_inputs=None,
        branch=branch,
        env_context=env_context,
        action_filter=action_filter,
    )
    assert outcome.run_dir is not None
    return outcome.run_dir


async def run_branch(
    tmp_path: Path,
    source: Path,
    step: int,
    replies: Sequence[Any],
    *,
    loop: Any = None,
    agent: BranchFakeAgent | None = None,
    cap: BranchFakeCap | None = None,
    memory: str = "simple",
    window: int = 4,
    run_id: str | None = None,
    branch: BranchProvenance | None = None,
    config_agent_id: str = FAKE_AGENT_ID,
    raise_on_error: bool = True,
    env_cls: type[EnvironmentBase] = BranchFakeEnv,
    action_filter: list[str] | None = None,
) -> BranchRunOutcome:
    """Branch *source* at *step* with a scripted teacher."""
    return await _drive(
        tmp_path / "branches",
        loop=loop if loop is not None else BranchLoop(),
        replies=replies,
        agent=agent if agent is not None else BranchFakeAgent(),
        cap=cap if cap is not None else make_cap(),
        memory=memory,
        window=window,
        run_id=run_id or f"branch-s{step}",
        task_id=f"src/s{step}/b0",
        loop_inputs={"checkpoint": Checkpoint(trace_dir=str(source.resolve()), step=step)},
        branch=branch,
        config_agent_id=config_agent_id,
        raise_on_error=raise_on_error,
        env_cls=env_cls,
        action_filter=action_filter,
    )
