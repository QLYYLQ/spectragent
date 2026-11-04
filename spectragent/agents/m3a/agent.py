# Portions of this module are derived from AndroidWorld's
# android_world/agents/m3a.py (M3A.step and the prompts it renders)
# (https://github.com/google-research/android_world, commit
# 55ddaf5edc6f6c25673aed343907d5b9dca66f84):
#
#   Copyright 2026 The android_world Authors.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# Modified by Spectragent (2026-11-03): rewritten as a Spectragent agent over the
# negotiated screenshot and UI tree. Kept: one message per step holding the
# action-selection prompt, the screenshot and its marked copy; the task's own
# guidelines; the history of one line per step; the summary call on the same model
# with the marked screens before and after. Changed: the summary is written when the
# next step's observation arrives, as a helper call the runtime weaves and
# attributes; a step whose action failed gets a history line with its error; the
# run's Loop and memory are checked to run M3A as AndroidWorld does.

"""M3AAgent: AndroidWorld's Multimodal Autonomous Agent for Android, which reads the UI tree.

Ported from AndroidWorld (``google-research/android_world`` at commit
``55ddaf5edc6f6c25673aed343907d5b9dca66f84``, Apache-2.0; ``THIRD_PARTY_NOTICES.md``):
``agents/m3a.py``, its prompts and its ``m3a_utils.py``. It is the agent that
validates ``android.ui_tree`` (the Android stage 2 of ``docs/execution_dag.md``;
``docs/architecture_design.md`` §4.1 "M3A").

**Each step, one prompt.** M3A keeps no conversation: every step is one user
message, its text and two images. The text is AndroidWorld's action-selection
prompt (``prompts/agents/m3a/action_selection.md``): what M3A can do, the
goal, a history of one line per earlier step, the screen's UI elements as the
configured serializer prints them (``android.ui_tree``'s ``serialized``; M3A's
line format with the ``m3a`` serializer), guidance, the task's own guidelines
when the run sets some (``task_guidelines``: AndroidWorld's runner sets one for
its MiniWoB tasks) and the reply format. The images are the screenshot as
captured and the same screenshot with every element's set-of-mark drawn on it
(``marks.py``), both JPEG as AndroidWorld sends them.

**A run must run M3A as AndroidWorld does**, and
:meth:`M3AAgent.check_run_settings` refuses one that would not, in
``AgentRuntime.prepare()`` (so ``spectragent config check`` and
``spectragent bench``'s pre-flight refuse it too). The memory keeps this
step's message alone:
``sliding_window`` (the only memory its wiring takes) with ``window_size: 1``.
The Loop never asks again within a step (``parse_retries_per_step: 0``) and
never ends the run on replies it cannot read (``max_consecutive_parse_errors:
null``), only on its step budget: AndroidWorld spends the step on such a
reply, its history says so, and the run goes on. A re-ask would also see only
its feedback, the window holding nothing else. A reply cut off at the
completion-token ceiling is one such reply to AndroidWorld, which reads its
content whatever the finish reason; a run reads it so when it sets the Loop's
``cut_off_reply: read_as_is``. That setting is the run's and is not checked
here: the Loop's default ends the run on a cut-off reply, naming the ceiling.

**The history is M3A's own, and its lines come from a second model call.**
After a step's action ran, AndroidWorld asks the same model to summarize the
step from the screenshot before (marked) and after (marked, labelled
``after``), the two element lists, the action and its reason
(``prompts/agents/m3a/summary.md``); the history line is
``Action selected: <action>. <summary>``. Here the next step's prompt builder
makes that call, when the after-screen is the observation it was handed: the
summary is a helper call (``PromptModelOwner``), on the agent's own model as
AndroidWorld's is, woven by Runtime and scoped with caller ``agent`` and
purpose ``step_summary`` in the run's call context, so the recorder keeps it
apart from the step's decision and it never becomes a thought or a decision
(§4.1). A step whose reply could not be read gets M3A's line for that and no
call; a step whose action failed gets ``Action selected: <action>. The action
failed: <error>`` and no call (AndroidWorld makes none when an action cannot
run). The summary call fails the step when it fails, like any helper call,
except when it is cut off at the completion-token ceiling: AndroidWorld's model
wrapper returns the content whatever the finish reason, so the line is written
with the text the summary had generated (empty when none), whatever the Loop's
``cut_off_reply``, which governs the step's decision only. The call record keeps
the cut. As AndroidWorld does, the screenshot before is sent without its ``before`` label
(it labels a copy it does not send), though the prompt says it has one.

**What M3A's replies run as** is ``reply.py``'s, which also lists where it
departs from AndroidWorld: an element ``index`` for ``click``, ``long_press``,
``type`` and ``scroll``; ``input_text`` is ``type`` (without an index,
``type_coordinate`` without a point, into the field with focus) then
``press_key`` ``enter``, so the Loop's ``max_actions_per_step`` must be at
least 2 (:meth:`M3AAgent.check_run_settings`); ``answer`` and ``status`` are
``loop.done``, the answer and the claim apart. M3A's prompt lists every
action it can take, so a run must offer them all
(:meth:`M3AAgent.check_action_space`).

**A branch reuses the source's summaries** (user ruling 2026-11-04, §4.1
"M3A"). On the ``branch`` Loop with ``history_source: source``, rebuilding the
source's history makes no model call: where M3A would call its summary model
(each replayed step, and the first policy step's line about the step before
the checkpoint), it takes the summary the source recorded
(``ReactPromptContext.source_helper_replies``, purpose ``step_summary``), so
its rebuilt prompts are the source's. A source step with no such summary is
refused (``IncompatibleCheckpointError``). From the step after the
checkpoint on, the summary is called live. ``history_source: restored`` is
refused: M3A has no ``build_restored_history``, and a restored step's line
would need a summary of a screen the source never summarized. It is not a
training-export student (no ``format_react_output``): the export renders a
student's prompts offline, where no summary model is called.
``parse_react_thought`` reads its reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Protocol

from PIL import Image
from pydantic import Field as PydanticField
from pydantic import JsonValue

from spectragent.agents._registrar import AgentBase
from spectragent.agents.m3a.marks import Bounds, jpeg, label, mark_elements, open_image
from spectragent.agents.m3a.reply import NOT_IN_FORMAT, actions_of, read_reason, read_reply
from spectragent.agents.prompts import load_prompt_fragment
from spectragent.core.action_guard import executed_descriptor
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.call_context import RunCallContext
from spectragent.core.llm.exceptions import ModelOutputTruncatedError
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.schemas.action import Action
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.fragments import HistoryObs, TaskObs
from spectragent.schemas.observation.geometry import ScreenshotGeometry, point_grid
from spectragent.schemas.observation.image import ModelImage, read_model_image
from spectragent.schemas.prompt import PromptSnapshot, ReactPromptContext
from spectragent.schemas.step_record import StepRecord

__all__ = ["M3AAgent", "M3AObservation"]

#: The actions M3A's replies run as, by relative name; its prompt teaches them all.
REQUIRED_ACTIONS = (
    "click",
    "long_press",
    "type",
    "type_coordinate",
    "scroll",
    "press_key",
    "open_app",
    "wait",
    "done",
)
#: How the summary call is attributed in the run's call context.
SUMMARY_PURPOSE = "step_summary"
#: M3A's history before any step.
NO_HISTORY = "You just started, no action has been performed yet."


# ── What M3A reads of an observation ─────────────────────────────


class _Element(Protocol):
    @property
    def bounds_in_screen(self) -> Bounds: ...


class _NumberedTree(Protocol):
    def element_nodes(self) -> Sequence[tuple[int, _Element]]: ...


class UITreeView(Protocol):
    """``android.ui_tree`` as M3A reads it: the serializer's text, and each element's bounds."""

    @property
    def serialized(self) -> str: ...

    def tree(self) -> _NumberedTree: ...


class ScreenshotView(ScreenshotGeometry, ModelImage, Protocol):
    """A screenshot that says its sizes and shows its image."""


class M3AObservation(Protocol):
    """The negotiated fragments M3A reads, by the names its wiring gives them."""

    @property
    def screenshot(self) -> ScreenshotView: ...
    @property
    def ui_tree(self) -> UITreeView: ...
    @property
    def task(self) -> TaskObs: ...
    @property
    def history(self) -> HistoryObs: ...


@dataclass(frozen=True)
class _Screen:
    """One step's screen as M3A shows it: both images (JPEG) and the element list."""

    step: int
    raw: bytes
    marked: bytes
    elements: str


@dataclass
class M3AAgent(AgentBase):
    """AndroidWorld's M3A: the screenshot, its marked copy and the UI elements; a summary per step."""

    __registry_key__ = "m3a"

    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset(
        {"max_actions_per_step", "parse_retries_per_step", "max_consecutive_parse_errors"}
    )

    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        "observation": {
            "registry": "observation",
            "keys": ["screenshot", "ui_tree", "task", "history"],
            "declare_only": True,
        },
        "action": {
            "registry": "action",
            "keys": list(REQUIRED_ACTIONS),
            "declare_only": True,
        },
        "llm": "llm",
        # One message per step: the window keeps it alone (``check_run_settings``).
        "memory": ("memory", ["sliding_window"]),
    }

    #: Where a new ``config template`` starts M3A: as AndroidWorld runs it
    #: (module docstring). :meth:`check_run_settings` refuses a run whose memory
    #: or Loop limits do not meet these: one message in the memory, at least two
    #: actions a step for ``input_text``, no re-ask within a step, no end on
    #: unreadable replies. ``cut_off_reply: read_as_is`` is AndroidWorld's behaviour, a
    #: reply cut off at the token ceiling read as it is; the template starts
    #: from it and nothing enforces it. Starting values written into the YAML,
    #: not defaults: a run takes only what its configuration says.
    __template_block__: ClassVar[Mapping[str, JsonValue]] = {
        "agent_loop": {
            "loop_id": "react",
            "max_actions_per_step": 2,
            "parse_retries_per_step": 0,
            "max_consecutive_parse_errors": None,
            "cut_off_reply": "read_as_is",
        },
        "memory": {"memory_id": "sliding_window", "window_size": 1},
    }

    # ── config ─────────────────────────────────────────────────
    task_guidelines: Annotated[
        tuple[str, ...],
        PydanticField(
            description=(
                "The task's own guidelines, written into each step's prompt after M3A's "
                "guidance as AndroidWorld writes them: under 'For The Current Task:', one "
                "'- ' line each. AndroidWorld's runner sets one for its MiniWoB tasks; "
                "empty (the default) writes nothing."
            )
        ),
    ] = ()

    # ── internal state ─────────────────────────────────────────
    #: The history lines so far, one per step, without their ``Step N-``.
    _history: list[str] = field(default_factory=list, init=False, repr=False)
    #: The screen the last step's action was chosen on: the next step's "before".
    _before: _Screen | None = field(default=None, init=False, repr=False)
    #: The agent's own model, woven by Runtime, for the summary call.
    _summarizer: BaseChatModel | None = field(default=None, init=False, repr=False)
    _call_context: RunCallContext | None = field(default=None, init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def system_prompt(self) -> PromptSnapshot | None:
        """The last action-selection prompt rendered: M3A has no system prompt of its own."""
        return self._prompt_snapshot

    # ── The summary model (``PromptModelOwner``) ───────────────

    @property
    def helper_llms(self) -> list[BaseChatModel]:
        """The summary's model: the agent's own, as AndroidWorld's M3A asks one model both."""
        return [self.llm]

    def bind_prompt_models(
        self, models: Sequence[BaseChatModel], call_context: RunCallContext
    ) -> None:
        """Keep the woven summary model and this run's call context (Runtime, in ``prepare()``)."""
        if len(models) != 1:
            raise ValueError(f"M3A summarizes with one model, and was handed {len(models)}")
        self._summarizer, self._call_context = models[0], call_context

    # ── Lifecycle hooks called by AgentRuntime ─────────────────

    def check_action_space(self, action_space: ActionSpace) -> None:
        """Refuse a run that cannot run every action M3A's prompt teaches (``ActionSpaceCheck``).

        M3A's prompt is AndroidWorld's, and lists every action, so an action
        space narrowed below them would have it teach what the run cannot do.
        """
        missing = [
            name for name in REQUIRED_ACTIONS if executed_descriptor(action_space, name) is None
        ]
        if missing:
            raise ValueError(
                f"M3A's prompt teaches actions that run as {', '.join(REQUIRED_ACTIONS)}; this "
                f"run's action space has no {', '.join(missing)}"
            )

    def check_run_settings(self, execution_limits: ExecutionLimits) -> None:
        """Refuse a run that would not run M3A as AndroidWorld does (``RunSettingsCheck``).

        ``AgentRuntime.prepare()`` asks, before the run starts (module
        docstring).

        Raises:
            ValueError: The Loop runs fewer than 2 actions per reply (M3A's
                ``input_text`` runs as typing and then Enter), asks again within
                a step, or ends the run on replies it cannot read; or the
                memory keeps more than the step's one message.
        """
        limit = execution_limits.max_actions_per_step
        if limit is None or limit < 2:
            raise ValueError(
                f"M3A's input_text runs as two actions, typing then Enter, so it needs the Loop's "
                f"max_actions_per_step at 2 or more; it is {limit}"
            )
        retries = execution_limits.parse_retries_per_step
        if retries:
            raise ValueError(
                "AndroidWorld never asks M3A again within a step: a reply it cannot read costs "
                f"the step. Set the Loop's parse_retries_per_step to 0; it is {retries}"
            )
        streak = execution_limits.max_consecutive_parse_errors
        if streak is not None:
            raise ValueError(
                "AndroidWorld never ends a run on replies M3A cannot read, only on its step "
                f"budget. Set the Loop's max_consecutive_parse_errors to null; it is {streak}"
            )
        window = getattr(self.memory, "window_size", None)
        if window != 1:
            raise ValueError(
                "M3A keeps no conversation, each step is one message: its memory must be "
                f"sliding_window with window_size 1; it is {window}"
            )

    def prepare(self, task: str, action_space: ActionSpace) -> list[BaseMessage]:
        """Start a run: no seed messages (each step is one message), and an empty history.

        The goal is read off each step's ``task`` fragment, so *task* is not
        kept; *action_space* was checked in ``AgentRuntime.prepare()``
        (:meth:`check_action_space`).
        """
        self._history, self._before, self._prompt_snapshot = [], None, None
        return []

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: M3AObservation, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """This step's message, after the history line of the step before it.

        The previous step's line needs this observation, its "after", so it is
        written here; when that step's action ran, writing it is the summary
        call (module docstring).
        """
        screen, marked = _screen(observation, context.step)
        goal = observation.task.user_request
        if observation.history.items:
            line = await self._history_line(
                observation.history.items[-1], screen, marked, goal, context
            )
            self._history.append(line)
        self._before = screen
        history = "\n".join(f"Step {n}- {line}" for n, line in enumerate(self._history, 1))
        text, self._prompt_snapshot = self.render(
            "action_selection",
            {
                "prefix": _prefix(self.__registry_key__),
                "goal": goal,
                "history": history or NO_HISTORY,
                "ui_elements": screen.elements or "Not available",
                "task_guidelines": _guidelines(self.task_guidelines),
            },
        )
        content: list[ContentPartTextParam | ContentPartImageParam] = [
            ContentPartTextParam(text=text),
            _image(screen.raw),
            _image(screen.marked),
        ]
        system: list[BaseMessage] = []
        for contribution in context.contributions:
            said = f"External prompt contribution:\n{contribution.text}"
            if contribution.placement == "system":
                system.append(SystemMessage(content=said))
            else:  # inside the step's one message, which is all the memory keeps
                content.append(ContentPartTextParam(text=said))
        return [*system, UserMessage(content=content)]

    def parse_react_output(self, llm_output: object) -> list[Action]:
        """The actions the reply's action runs as (``reply.py``)."""
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        return actions_of(read_reply(text))

    def get_react_output_model(self) -> None:
        """None: M3A replies in its own text format."""
        return None

    # ── Training-export slot (optional ReAct slot, arch §4.1) ──

    @staticmethod
    def parse_react_thought(llm_output: object) -> tuple[str, str] | None:
        """The reply's ``Reason:`` and ``"Reason"``, or ``None``; needs no run."""
        reason = read_reason(llm_output if isinstance(llm_output, str) else str(llm_output))
        return None if reason is None else (reason, "Reason")

    # ── The history ────────────────────────────────────────────

    async def _history_line(
        self,
        record: StepRecord,
        after: _Screen,
        after_marked: Image.Image,
        goal: str,
        context: ReactPromptContext,
    ) -> str:
        """M3A's history line for the step *record* recorded, whose result *after* shows.

        Its summary is the source's recorded one where *context* hands the
        source's helper replies over (a branch rebuilding its source's
        history), else the summary call's.
        """
        if record.error_phase == "parse":
            return record.error or NOT_IN_FORMAT
        before = self._before
        if before is None or before.step != record.step:
            raise ValueError(
                f"M3A summarizes step {record.step} from the screen its action was chosen on, "
                f"which it keeps from building that step's prompt; it last built step "
                f"{before.step if before is not None else 'none'}'s"
            )
        reply = read_reply(record.llm_output)
        failed = next((result for result in record.action_results if not result.success), None)
        if failed is not None:
            why = f": {failed.error}" if failed.error else "."
            return f"Action selected: {reply.action}. The action failed{why}"
        if context.source_helper_replies is not None:
            summary = _recorded_summary(context, record.step)
        else:
            summary = await self._summarize(
                goal, reply.action, reply.reason, before, after, after_marked
            )
        return f"Action selected: {reply.action}. {summary}"

    async def _summarize(
        self,
        goal: str,
        action: str,
        reason: str,
        before: _Screen,
        after: _Screen,
        after_marked: Image.Image,
    ) -> str:
        """The summary call: the step's action, its reason and its screens before and after.

        A summary cut off at the completion-token ceiling is the text it had
        generated (empty when none), as AndroidWorld's model wrapper returns the
        content whatever the finish reason, whatever the Loop's ``cut_off_reply``:
        this call is M3A's own, not the step's decision. Its call record keeps the cut.
        """
        if self._summarizer is None or self._call_context is None:
            raise RuntimeError(
                "M3A's step summary needs the model and call context Runtime binds "
                "(bind_prompt_models), and has none"
            )
        text, _snapshot = self.render(
            "summary",
            {
                "prefix": _prefix(self.__registry_key__),
                "goal": goal,
                "before_elements": before.elements,
                "after_elements": after.elements,
                "action": action,
                "reason": reason,
            },
        )
        message = UserMessage(
            content=[
                ContentPartTextParam(text=text),
                _image(before.marked),
                _image(jpeg(label(after_marked, "after"))),
            ]
        )
        with self._call_context.scope(caller="agent", purpose=SUMMARY_PURPOSE, step=after.step):
            try:
                completion = await self._summarizer.ainvoke([message])
            except ModelOutputTruncatedError as exc:
                return exc.partial_output or ""
        return completion.completion


def _recorded_summary(context: ReactPromptContext, summarized: int) -> str:
    """The summary of step *summarized* the source wrote while it built step ``context.step``.

    Raises:
        IncompatibleCheckpointError: The source recorded no such summary, or
            more than one: a branch makes no model call to write it again.
    """
    replies = (context.source_helper_replies or {}).get(SUMMARY_PURPOSE, ())
    if len(replies) != 1:
        raise IncompatibleCheckpointError(
            context.source_run_id or "the source run",
            [
                f"step {context.step} recorded {len(replies)} {SUMMARY_PURPOSE} replies; M3A's "
                f"history line of step {summarized} is the one summary the source wrote, and "
                "rebuilding the source's history makes no model call to write it again"
            ],
        )
    return replies[0]


def _screen(observation: M3AObservation, step: int) -> tuple[_Screen, Image.Image]:
    """*observation*'s screen as M3A shows it, and its marked image (for the label after).

    Raises:
        ValueError: The screenshot has no image, or no size to put the marks by.
    """
    shot = observation.screenshot
    data = read_model_image(shot)
    grid = point_grid("screenshot_pixels", shot)
    if data is None or grid is None:
        raise ValueError(
            f"M3A marks the UI elements on the step's screenshot, and step {step}'s has "
            f"{'no image' if data is None else 'no known size'}"
        )
    tree = observation.ui_tree
    image = open_image(data)
    marked = mark_elements(
        image,
        ((index, node.bounds_in_screen) for index, node in tree.tree().element_nodes()),
        grid,
    )
    screen = _Screen(step=step, raw=jpeg(image), marked=jpeg(marked), elements=tree.serialized)
    return screen, marked


def _guidelines(guidelines: Sequence[str]) -> str:
    """The task's own guidelines as AndroidWorld writes them into the prompt; none, nothing."""
    if not guidelines:
        return ""
    return "For The Current Task:\n" + "".join(f"- {line}\n" for line in guidelines)


def _prefix(agent_id: str) -> str:
    """What M3A's two prompts open with: who it is and every action it can take."""
    text, _ref = load_prompt_fragment(agent_id, "_parts", "prefix")
    return text


def _image(data: bytes) -> ContentPartImageParam:
    return ContentPartImageParam.from_bytes(data, media_type="image/jpeg", detail="auto")
