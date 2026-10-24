"""The Skyvern family: an abstract root, ``skyvern``, and its two members.

``docs/architecture_design.md`` §4.1, "Agent 家族". Skyvern reads the page
through its own DOM cleaning pipeline (``dom_cleaner: skyvern`` or
``skyvern_hit_test``), so every member keeps the element tree and addresses an
element by its index. The members differ in what else reaches the model:

======================== =================================== ================================
member                   in model context                    actions
======================== =================================== ================================
``skyvern.dom``          the element tree                    index-addressed only
``skyvern.dom_vision``   the element tree and the screenshot index-addressed, and
                                                             ``click_coordinate`` when offered
======================== =================================== ================================

There is no vision-only member: without the element tree nothing is left that
is Skyvern's own, and computer-use agents (UI-TARS) cover that ground. Each
member writes its complete observation and action ``__wiring__``; this root
holds what they share, merged through the MRO: the Loop choice, the prompt,
the reply and its reading, and the action history.

The agent is glue (§4.1). What it owns is Skyvern's prompt and its reply
shape; the rest is the framework's, and the agent only quotes or calls it:

- **The reply is a closed schema** built per run from the negotiated
  descriptors (``skyvern/reply.py`` over ``core/actions/action_union.py``):
  only this run's actions, each with its own parameters, so a reply that gives
  an action a parameter it does not take (``text`` on a click) is refused at
  validation and goes back to the model.
- **The element tree describes itself**: the system prompt quotes the run's
  DOM serializer (``__fragment_formats__``), so it teaches ``html`` or
  ``text`` as the run writes it.
- **Points are the framework's** (``core/loops/coordinate_frames.py``). A
  member whose model writes points declares the grid (``coordinate_space``);
  the framework converts each point the reply writes and writes every point
  shown back to the model (the action history, its effect lines, the pointer)
  on the frame of the step it belongs to. A run with no point action has no
  frame, and every point stays in viewport units.
- **The action history** is the tail of the Loop's ``history`` fragment, the
  actions that actually ran, each with what it returned as the shared
  ``result_facts`` (``agents/rendering/action_effects.py``) writes it: the
  same facts restored history shows, so a live step and a restored one read
  alike. Returned content (``extract_content``'s text) is shown, cut to
  ``max_result_chars``.

The screenshot is captured in both members, because the trace keeps it and
evaluators score runs from it; whether the model sees it is the member's
choice (:meth:`SkyvernAgent._screenshot_detail`).

The template is Jinja2 rather than ``str.format`` because it branches on
optional criteria and on the member's surface. That is declared
(``__prompt_engine__``), not inferred from the file's contents.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError
from pydantic import Field as PydanticField

from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import PromptEngine
from spectragent.agents.rendering.action_effects import result_facts
from spectragent.agents.rendering.browser_state import render_browser_state
from spectragent.agents.rendering.capture_state import capture_state_demand, write_viewport_pixels
from spectragent.agents.rendering.platform import render_browser_platform
from spectragent.agents.rendering.prompt_context import (
    action_json,
    place_prompt_contributions,
    restored_history_message,
)
from spectragent.agents.rendering.skipped_actions import skip_reason
from spectragent.agents.skyvern.reply import ACTIONS_FIELD, REASONING_FIELD, reply_model
from spectragent.core.action_guard import executed_name
from spectragent.core.actions.action_union import action_of
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.coordinate_frames import fact_in_model_frame, point_writer
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import PointFrame
from spectragent.schemas.observation.image import image_media_type, read_model_image
from spectragent.schemas.prompt import PromptSnapshot, ReactPromptContext, RestoredHistory
from spectragent.schemas.step_record import StepRecord

#: How the model may be shown the screenshot: OpenAI's image ``detail``.
ImageDetail = Literal["auto", "low", "high"]


@dataclass
class SkyvernAgent(AgentBase):
    """The Skyvern family root: element-tree prompt, closed reply, action history."""

    __abstract__ = True
    __registry_key__ = "skyvern"
    __prompt_engine__: ClassVar[PromptEngine] = "jinja2"

    # The system prompt states the Loop's per-step action budget.
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset({"max_actions_per_step"})
    # The element tree's format is the DOM serializer's to describe: the
    # system prompt quotes the run's description verbatim, so changing the
    # serializer changes the prompt with it (``docs/architecture_design.md``
    # §4.4 "观测格式由提供方描述"). ``prepare`` receives it as
    # ``fragment_formats["dom"]``.
    __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"dom"})

    # Shared by both members; each member writes its own complete
    # ``observation`` and ``action`` entries.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``action_history_window`` counts *actions*, not steps — each one that
    # ran, and each one its step skipped after a failed one — and it is spent
    # in the per-step user message, not the system prompt, which
    # ``prepare()`` builds once.
    action_history_window: Annotated[
        int,
        PydanticField(
            description=(
                "Number of most recent actions replayed in each step's user message: "
                "each one that ran, with what it returned, and each one its step did not "
                "execute because an earlier action failed"
            )
        ),
    ] = 5
    max_result_chars: Annotated[
        int,
        PydanticField(
            ge=1,
            description=(
                "Longest returned content shown for one action (the text "
                "extract_content returned) in the action history and in restored "
                "history. Longer content is cut here and ends with a note saying "
                "how many characters it had."
            ),
        ),
    ] = 4000
    # Hand-written rather than computed from the directory listing: a
    # ``Literal[tuple(...)]`` renders correctly through conscribe but
    # costs a pyright error per field. ``tests/test_agents/test_prompt_tree.py``
    # asserts it equals what is on disk.
    prompt_template: Annotated[
        Literal["system_prompt"],
        PydanticField(description="Skyvern template name (without extension)"),
    ] = "system_prompt"
    navigation_payload: Annotated[
        str | None,
        PydanticField(
            description=(
                "User details the task needs (form values, credentials, "
                "preferences); rendered as <user_details> in the system prompt. "
                "None (the default): an empty section."
            )
        ),
    ] = None
    complete_criterion: Annotated[
        str | None,
        PydanticField(
            description=(
                "Condition that must hold before the agent may finish with success=true. "
                "None (the default): no condition."
            )
        ),
    ] = None
    terminate_criterion: Annotated[
        str | None,
        PydanticField(
            description=(
                "Condition on which the agent must give up (success=false). None (the "
                "default): no condition."
            )
        ),
    ] = None
    verification_code_check: Annotated[
        bool,
        PydanticField(
            description=(
                "Tell the agent to give up when a page demands a verification "
                "code it cannot obtain, instead of stalling"
            )
        ),
    ] = False
    show_action_effects: Annotated[
        bool,
        PydanticField(
            description=(
                "Show each executed action's measured effect (the one line the "
                "effect describes itself with, e.g. where a click landed or how "
                "far a scroll moved) in the action history, restored history "
                "included. Off: no effect reaches the model; the trace still "
                "records every effect, so both arms of an ablation measure the same."
            )
        ),
    ] = True
    show_capture_state: Annotated[
        bool,
        PydanticField(
            description=(
                "Ask for the optional capture_state fragment (how the environment "
                "stood when captured: settle, foreground change, pointer, failed "
                "visible resources) and show it with the page state every step. "
                "Off: it is excluded from this agent's observation demand, so it "
                "is neither captured nor shown."
            )
        ),
    ] = True
    show_media_state: Annotated[
        bool,
        PydanticField(
            description=(
                "Show the media playback state measured with the screenshot (each "
                "audio and video element's frame, order, position, length and whether "
                "it plays) after the page state every step. Off (the default): it is "
                "still recorded with every screenshot and used to check restoration, "
                "but never reaches the model. An explicit experiment variable: it "
                "changes the prompt."
            )
        ),
    ] = False

    # ── internal state populated by prepare() ──────────────────
    # ``task`` reaches every step as ``obs.task.user_request``; the action
    # history is the Loop's ``history`` fragment. Neither is held here.
    _action_space: ActionSpace = field(default_factory=ActionSpace, init=False, repr=False)
    _reply_model: type[BaseModel] | None = field(default=None, init=False, repr=False)
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation"

    @property
    def system_prompt(self) -> PromptSnapshot | None:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._action_space.descriptors)

    # ── The member's surface ───────────────────────────────────

    def _screenshot_detail(self) -> ImageDetail | None:
        """The detail the screenshot is shown to the model at, or ``None``: not shown.

        The one place a member says whether its model sees the screenshot; the
        system prompt and each step's message both read it. The root shows
        none (``skyvern.dom``); ``skyvern.dom_vision`` returns its configured
        detail. The screenshot is captured and recorded either way.
        """
        return None

    # ── Negotiation ────────────────────────────────────────────

    def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
        """The class demand, without ``capture_state`` when ``show_capture_state`` is off."""
        required, optional = super().negotiation_demand(registry)
        return capture_state_demand(
            self, registry, required, optional, shown=self.show_capture_state
        )

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_space: ActionSpace,
        execution_limits: ExecutionLimits,
        fragment_formats: Mapping[str, str],
    ) -> list[BaseMessage]:
        """Pure builder: this run's reply model and its system prompt seed.

        *action_space* is the run's effective action space, the one the guard
        executes; its descriptors become both the prompt's action list and the
        closed reply schema, so the two cannot disagree. ``max_actions_per_step``
        comes from the Loop, which is what truncates. *fragment_formats* holds
        the run's description of the element tree (``"dom"``), written by its
        DOM serializer; the system prompt quotes it.

        Raises:
            ValueError: No action budget, no element tree format, or no action.
            UnsupportedParamsSchema: An action's params are outside the closed
                union's subset — before the first model call.
        """
        max_actions_per_step = execution_limits.max_actions_per_step
        if max_actions_per_step is None:
            raise ValueError("prepare requires the Loop's max_actions_per_step execution limit")
        dom_format = fragment_formats.get("dom")
        if not dom_format:
            raise ValueError("prepare requires the dom fragment's format description")
        self._action_space = action_space
        self._reply_model = reply_model(action_space.descriptors)
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, max_actions_per_step, dom_format
        )
        # cache=True: the static prefix's breakpoint.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: Any, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """Pure builder: this step's user message.

        The task (``<user_request>``) and the step budget head every step, so
        they never drift out of a bounded memory. ``<action_history>`` changes
        every step, so it lives here rather than in the system prompt. Then the
        page state and the element tree; a member that shows the screenshot
        attaches it. The pointer is written on this step's frame
        (``context.frame``), each history step's points on its own.
        """
        detail = self._screenshot_detail()
        image = (
            None if detail is None else read_model_image(getattr(observation, "screenshot", None))
        )
        note = None if detail is None else _screenshot_note(image, context.frame)
        body = self._step_text(observation, context.frame, screenshot_note=note)
        if detail is not None and image is not None:
            message = _image_message(body, image, detail)
        else:
            message = UserMessage(content=body, cache=True)
        return place_prompt_contributions([message], context=context)

    async def build_restored_history(
        self,
        observation: Any,
        *,
        history: RestoredHistory,
        context: ReactPromptContext,
    ) -> list[BaseMessage]:
        """Render actual restored facts after this step's live observation.

        The window shows the steps before this one, as a policy prompt
        precedes its decision; this step's restored record joins the Loop's
        history afterwards. Returned content is bounded as in the live
        history, so the two read alike.
        """
        history.validate_execution()
        messages = await self.build_react_prompt(observation, context=context)
        restored = restored_history_message(
            history,
            show_action_effects=self.show_action_effects,
            context=context,
            action_space=self._action_space,
            max_content_chars=self.max_result_chars,
        )
        return [*messages, restored]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Validate a reply against this run's closed schema, as Actions.

        A provider-validated instance and free-form JSON text go through the
        same per-run model, so a run that falls back to text is held to the
        same verbs and parameter types. A name the model wrote is a name the
        run holds: the schema permits only negotiated, fully-qualified keys.

        Raises:
            ParseError: Anything the model can fix by writing another reply.
        """
        model = self._reply_model
        if model is None:
            raise ParseError(
                f"{self.agent_name}.parse_react_output ran before prepare(), so there "
                f"is no negotiated action set to validate against.",
                raw_output=str(llm_output)[:500],
            )
        parsed: Any = llm_output
        if not isinstance(llm_output, model):
            text = llm_output if isinstance(llm_output, str) else str(llm_output)
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ParseError(
                    f"LLM output is not valid JSON: {exc}", raw_output=text[:500]
                ) from exc
            try:
                parsed = model.model_validate(data)
            except ValidationError as exc:
                raise ParseError(
                    f"LLM JSON does not match this run's output schema: {exc}",
                    raw_output=text[:500],
                ) from exc
        return [action_of(item.action) for item in getattr(parsed, ACTIONS_FIELD)]

    def get_react_output_model(self) -> type[BaseModel]:
        """The closed per-run reply model, once ``prepare()`` has built it.

        Raises:
            RuntimeError: Called before ``prepare()``; the schema is built from
                the negotiated actions, which do not exist yet.
        """
        if self._reply_model is None:
            raise RuntimeError(
                f"{self.agent_name}.get_react_output_model() was called before "
                f"prepare(); the reply schema is built from the negotiated actions."
            )
        return self._reply_model

    # ── Training-export slot (optional ReAct slot, arch §4.1) ──

    @staticmethod
    def parse_react_thought(llm_output: Any) -> tuple[str, str] | None:
        """The thought of a recorded reply and the field it came from, or ``None``.

        Skyvern reasons per action: every non-empty ``reasoning`` of the
        reply's ``actions``, stripped, in order, joined by newlines. Pure:
        reads the reply only (the JSON text a run records, or the parsed
        model), so the training export calls it on the class.
        """
        if isinstance(llm_output, BaseModel):
            data: Any = llm_output.model_dump()
        elif isinstance(llm_output, Mapping):
            data = llm_output
        else:
            try:
                data = json.loads(llm_output if isinstance(llm_output, str) else str(llm_output))
            except json.JSONDecodeError:
                return None
        items = data.get(ACTIONS_FIELD) if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            return None
        reasons = [
            item[REASONING_FIELD].strip()
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get(REASONING_FIELD), str)
            and item[REASONING_FIELD].strip()
        ]
        return ("\n".join(reasons), REASONING_FIELD) if reasons else None

    # ── private helpers ────────────────────────────────────────

    def _build_system_prompt(
        self, task: str, max_actions_per_step: int, dom_format: str
    ) -> tuple[str, PromptSnapshot]:
        space = self._action_space
        return self.render(
            self.prompt_template,
            {
                "navigation_goal": task,
                "dom_format": dom_format,
                "shows_screenshot": self._screenshot_detail() is not None,
                # The grid the framework framed this run's point actions on;
                # ``None`` when the run has none, and so writes no point.
                "coordinate_space": space.coordinate_space,
                "action_space": _render_action_list(space.descriptors),
                # Named from the run's bindings, never written into the
                # template: the run's ``done`` and ``click`` are whichever
                # capability provides them. The relative names are the
                # fallback only a fixture without them reaches.
                "done_action": executed_name(space, "done") or "done",
                "example_action": executed_name(space, "click") or "click",
                "max_actions_per_step": max_actions_per_step,
                "max_result_chars": self.max_result_chars,
                "navigation_payload": self.navigation_payload or "",
                "complete_criterion": self.complete_criterion or "",
                "terminate_criterion": self.terminate_criterion or "",
                "verification_code_check": self.verification_code_check,
            },
        )

    def _step_text(
        self, observation: Any, frame: PointFrame | None, *, screenshot_note: str | None
    ) -> str:
        """The step's text: task, platform, step budget, action history, page, element tree."""
        parts = [
            f"<user_request>\n{observation.task.user_request}\n</user_request>",
            render_browser_platform(observation),
        ]
        step_info = getattr(observation, "step_info", None)
        if step_info is not None:
            parts.append(
                f"<step_info>step {step_info.step}/{step_info.max_steps} "
                f"(today={step_info.today})</step_info>"
            )
        history = self._render_action_history(getattr(observation, "history", None))
        parts.append(f"<action_history>\n{history}\n</action_history>")
        browser_state = self._render_browser_state(
            observation, frame, screenshot_note=screenshot_note
        )
        return "\n".join(parts) + "\n\n" + browser_state

    def _render_action_history(self, history: Any) -> str:
        """The last ``action_history_window`` actions, oldest first, one per line.

        Read from the Loop's ``history`` records, across steps, one JSON
        object per action that ran: ``{"action": ..., "result": ...}``, the
        result as ``result_facts`` writes it (returned content bounded by
        ``max_result_chars``, the effect line when effects are shown), or
        ``{"action": ..., "raised": ...}`` for the action that raised and so
        ended its step's batch. An action the Loop skipped because an
        earlier one of its step failed is
        ``{"action": ..., "not_executed": "action N failed"}``: it was the
        model's plan, and the model has to know it did not run. Actions the
        Loop never ran otherwise — cut by ``max_actions_per_step``, or after
        a ``done`` or a raise — are not history.

        The ``<= 0`` branch is not padding: ``lst[-0:]`` is ``lst[0:]``, so a
        window of 0 would otherwise render the entire run.
        """
        if self.action_history_window <= 0:
            return "(disabled)"
        records: list[StepRecord] = list(getattr(history, "items", None) or [])
        lines = [line for record in records for line in self._executed_lines(record)]
        window = lines[-self.action_history_window :]
        return "\n".join(window) if window else "(no actions yet)"

    def _executed_lines(self, record: StepRecord) -> list[str]:
        """One history line per action *record* ran or skipped, its points on the record's frame."""
        write_point = point_writer(record.frame)

        def written(action: Action) -> Any:
            return action_json(fact_in_model_frame(action, record.frame, self._action_space))

        entries: list[dict[str, Any]] = [
            {
                "action": written(action),
                "result": result_facts(
                    result,
                    show_effects=self.show_action_effects,
                    write_point=write_point,
                    max_content_chars=self.max_result_chars,
                ),
            }
            for action, result in zip(record.actions, record.action_results, strict=False)
        ]
        raised = len(record.action_results)
        if record.error_phase == "act" and raised < len(record.actions):
            entries.append({"action": written(record.actions[raised]), "raised": record.error})
        reason = skip_reason(record)
        entries.extend(
            {"action": written(action), "not_executed": reason} for action in record.skipped()
        )
        return [json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries]

    def _render_browser_state(
        self, observation: Any, frame: PointFrame | None, *, screenshot_note: str | None
    ) -> str:
        """The page state (URL, title, tabs, capture state), the screenshot note, the element tree.

        The page state is the shared browser writing, ``render_browser_state``,
        its pointer on this step's frame (viewport pixels when the run writes
        no point). *screenshot_note* is what a member that shows the
        screenshot says about it (:func:`_screenshot_note`); ``None`` for one
        that does not.
        """
        parts: list[str] = []
        page_state = render_browser_state(
            observation,
            show_media_state=self.show_media_state,
            write_point=point_writer(frame, viewport=write_viewport_pixels),
        )
        if page_state:
            parts.append(page_state)
        if screenshot_note is not None:
            parts.append(screenshot_note)
        dom = getattr(observation, "dom", None)
        element_tree = getattr(dom, "element_tree", None) if dom is not None else None
        if element_tree is not None:
            # ``element_tree`` is a ``DOMState``; the form the agent is meant
            # to read is its ``serialized_dom``.
            serialized = getattr(element_tree, "serialized_dom", None)
            dom_str = serialized if isinstance(serialized, str) else str(element_tree)
            parts.append(f"Element Tree:\n{dom_str}")
        return "\n\n".join(parts) if parts else "(empty browser state)"


def _render_action_list(descriptors: list[ActionDescriptor]) -> str:
    """The negotiated actions as the prompt's vocabulary, one line each.

    ``- `browser.click(index: int -- element index)` — Click a DOM element.``

    The names are the descriptors' own, which is exactly what the closed
    reply schema permits and the guard executes. A point param's text already
    says the grid the model writes it on: the framework framed the run's
    descriptors (``core/loops/coordinate_frames.py::framed_action_space``).
    """
    lines: list[str] = []
    for d in descriptors:
        params_part = (
            ", ".join(f"{k}: {v}" for k, v in d.params.items()) if d.params else "no params"
        )
        lines.append(f"- `{d.name}({params_part})` — {d.description}")
    return "\n".join(lines)


def _screenshot_note(image: bytes | None, frame: PointFrame | None) -> str:
    """What a step that shows the screenshot says about it: its size, or that there is none."""
    if image is None:
        return "No screenshot could be shown this step."
    grid = frame.grid if frame is not None else None
    # Only the pixel grid needs a size: a 0-1000 grid is defined over
    # whatever size the image has, and stating pixels would invite the
    # model to answer in them.
    if grid is not None and grid.space == "screenshot_pixels":
        return f"Screenshot: {grid.width}x{grid.height} pixels, showing the whole visible viewport."
    return "Screenshot: attached, showing the whole visible viewport."


def _image_message(body: str, image: bytes, detail: ImageDetail) -> UserMessage:
    """The step's message with the screenshot attached after its text."""
    media_type = image_media_type(image) or "image/png"
    url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
    content: list[Any] = [
        ContentPartTextParam(text=body),
        ContentPartImageParam(image_url=ImageURL(url=url, detail=detail, media_type=media_type)),
    ]
    return UserMessage(content=content, cache=True)
