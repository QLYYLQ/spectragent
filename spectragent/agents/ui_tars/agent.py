"""UITarsAgent — the vision-only computer-use agent speaking the UI-TARS text protocol.

UI-TARS is glue (``docs/architecture_design.md`` §4.1): it declares the
observation and the action vocabulary it needs, builds its prompt, declares
the grid its model writes points on, and hands its model's replies to its
codec. Its ``__wiring__`` names domain verbs (``click_coordinate``,
``type_coordinate``), never a provider, so the same agent negotiates against
a browser, a desktop VM or an Android emulator unchanged.

- **The protocol is the codec's** (``agents/ui_tars/codec.py``): the verb
  table, reading a reply into actions, writing actions back as a reply, and
  reading a thought. The prompt's Available Actions are generated from the
  verbs the codec binds to this run's action space, so the prompt cannot
  advertise a verb the run cannot execute, nor teach a parameter its action
  does not read. The four ReAct slots (``parse_react_output``,
  ``parse_react_thought``, ``format_react_output``, ``format_action_fact``)
  are one-line calls into it.
- **Coordinates are the framework's** (``core/loops/coordinate_frames.py``).
  ``coordinate_space`` declares whether the model answers in pixels of the
  screenshot or on a 0-1000 grid over it, and the prompt teaches it. The
  framework converts every point the model writes into the capability's
  units, and writes every point it is shown (the previous batch, effect
  lines, the pointer) on the frame of the step it belongs to, read off that
  step's record (``StepRecord.frame``; this step's is ``ReactPromptContext.frame``).
- **The output contract.** The Loop owns ``max_actions_per_step`` and is what
  truncates; ``AgentRuntime`` hands it to :meth:`UITarsAgent.prepare`, which
  renders the budget (:func:`_action_contract`): one ``Action:`` line per
  action, run in order, at most N.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._registrar import AgentBase
from spectragent.agents.rendering.action_effects import result_facts
from spectragent.agents.rendering.capture_state import (
    CAPTURE_STATE_KEY,
    capture_state_demand,
    render_capture_state,
)
from spectragent.agents.rendering.platform import render_computer_use_platform
from spectragent.agents.rendering.prompt_context import (
    place_prompt_contributions,
    restored_history_message,
)
from spectragent.agents.rendering.skipped_actions import skip_reason
from spectragent.agents.ui_tars.codec import UITarsCodec, read_thought
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.coordinate_frames import fact_in_model_frame, point_writer
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace
from spectragent.schemas.observation.image import image_media_type, read_model_image
from spectragent.schemas.prompt import PromptSnapshot, ReactPromptContext, RestoredHistory

# ── The coordinate contract ───────────────────────────────────

#: What the system prompt says a point is, per coordinate space. Keyed by
#: every member of :data:`CoordinateSpace`, which is also what the
#: ``coordinate_space`` field's Literal lists
#: (``tests/test_agents/test_ui_tars/test_ui_tars_coordinates.py`` holds the three equal).
_COORDINATE_PROMPTS: Mapping[CoordinateSpace, str] = MappingProxyType(
    {
        "screenshot_pixels": (
            "A point is (x, y) in pixels of the screenshot you are shown: (0, 0) is "
            "its top-left corner, x grows to the right and y downwards. Every step "
            "states the screenshot's size in pixels."
        ),
        "normalized_1000": (
            "A point is (x, y) on a 0-1000 grid laid over the screenshot, whatever "
            "its size in pixels: (0, 0) is the top-left corner, (1000, 1000) the "
            "bottom-right corner, x grows to the right and y downwards."
        ),
    }
)

#: Said on every grid, because it is about the spelling rather than the
#: space. The codec reads a point as numbers and nothing else, so a model that
#: names its target instead of locating it (``point='btn2'``) spends a step on
#: a refusal. Saying so costs one sentence.
_POINT_SYNTAX = (
    "Write a point as the two numbers alone, e.g. point='(640, 360)'. Never put "
    "an element name, a selector or a description of the target where a number "
    "belongs — nothing there identifies an element, only where it is."
)


# ── The output contract ───────────────────────────────────────


def _action_contract(max_actions_per_step: int, *, can_finish: bool) -> tuple[str, str]:
    """``(action_format, action_budget)`` for the Loop's per-step limit.

    The semantics are ``ReactLoop``'s, stated as the model experiences
    them: every ``Action:`` line is one action; they run in the order
    written against the screenshot already shown, and an action that fails
    stops the batch (the ones after it do not run, and the next
    observation's feedback names them); the Loop drops everything past
    ``max_actions_per_step`` before it runs; nothing after a finishing
    action runs. With one action per reply there is nothing to stop, so its
    wording does not mention failure.
    """
    if max_actions_per_step < 1:
        raise ValueError(
            f"max_actions_per_step={max_actions_per_step}: a step that may run "
            f"no action cannot be advertised to the model"
        )
    if max_actions_per_step == 1:
        return (
            "Action: <one action call from Available Actions, written exactly as shown>",
            "Write exactly one Action line. That one action runs, and your next "
            "turn starts from a new screenshot of its result.",
        )
    n = max_actions_per_step
    budget = (
        f"Write one Action line per action, each on its own line. You may use a "
        f"maximum of {n} actions per reply. They run one after another in the "
        f"order written, all aimed at the screenshot you are looking at now, until "
        f"one fails: the actions after a failed one do not run, and the next "
        f"observation says which. You see the screen again only after the batch "
        f"stops. Anything past the first {n} is dropped before it runs, so do not "
        f"plan past it, and do not chain an action whose target only appears after "
        f"an earlier one has run."
    )
    if can_finish:
        budget += " Nothing written after finished() runs."
    return (
        "Action: <an action call from Available Actions, written exactly as shown>\n"
        "Action: <the next action call, only if another one is needed>",
        budget,
    )


def _render_action_space(codec: UITarsCodec) -> str:
    """The run's actions as UI-TARS call syntax, one block per action the codec binds a verb to::

        - type(content='text to type', point='(x, y)')
          Click a point in the page viewport to focus a text field, then type.
          executes: text: str

    The signature is the verb's (the codec reads it); the description and the
    parameter types are the descriptor's (what the action executes). The
    ``executes`` line names the params the verb's non-point arguments fill,
    as a consequence rather than as syntax: the descriptor's names belong to
    the executor (``text``), the model writes the verb's (``content``). Points
    are left out of it: the Coordinates section says which grid they are on.
    A run action with no verb is skipped: the model has no syntax for it.
    """
    bound_to = {bound.descriptor.name: bound for bound in codec.verbs.values()}
    blocks: list[str] = []
    for descriptor in codec.space.descriptors:
        bound = bound_to.get(descriptor.name)
        if bound is None:
            continue
        lines = [f"- {bound.verb.signature}", f"  {descriptor.description}"]
        filled = [
            (a.param, descriptor.params[a.param])
            for a in bound.verb.arguments
            if a.param is not None and a.param in descriptor.params
        ]
        if filled:
            lines.append(f"  executes: {', '.join(f'{k}: {v}' for k, v in filled)}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


@dataclass
class UITarsAgent(AgentBase):
    """UI-TARS style agent — vision-only, text protocol, coordinate actions.

    Every observation is taken as soon as the previous step's last action
    returns, so a widget that is still animating (a spinner, a challenge
    fading in) is captured mid-animation, and the next reply aims at what
    is no longer there. The knob for that is the capability's, not this
    agent's: ``post_delay_ms`` on the coordinate actions in the
    capability's ``actions:`` list — see "Vision agents" in
    ``configs/README.md``.
    """

    __registry_key__ = "ui_tars"

    # ── Cross-registry wiring ────────────────────────────────────
    # Domain verbs only, no ``capability`` entry: negotiation binds relative
    # keys (AGENTS.md §2.5). The required actions are what the model cannot
    # work without; the optional four are the rest of its native vocabulary,
    # dropped from the prompt and the codec when a capability does not offer
    # them. ``capture_state`` is the platform-neutral capture fact (optional);
    # browser page state (``browser_meta``) is for browser-specific agents and
    # deliberately not declared (``docs/architecture_design.md`` §4.4).
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset({"max_actions_per_step"})

    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        "observation": {
            "registry": "observation",
            "keys": ["screenshot", "platform", "history"],
            "optional_keys": ["step_info", CAPTURE_STATE_KEY],
            "declare_only": True,
        },
        "action": {
            "registry": "action",
            "keys": ["click_coordinate", "type_coordinate", "done"],
            "optional_keys": ["scroll", "send_keys", "drag", "wait"],
            "declare_only": True,
        },
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``max_steps`` / ``max_actions_per_step`` are the Loop's. The members of
    # ``prompt_template`` are the on-disk stems (``test_prompt_tree.py``) and
    # those of ``coordinate_space`` are :data:`CoordinateSpace`'s
    # (``test_ui_tars_coordinates.py``).
    prompt_template: Annotated[
        Literal["computer_use", "grounding", "mobile_use"],
        PydanticField(description="Which shipped prompt variant to render"),
    ] = "computer_use"
    coordinate_space: Annotated[
        Literal["screenshot_pixels", "normalized_1000"],
        PydanticField(
            description=(
                "The grid the model writes points on: 'screenshot_pixels' "
                "(pixels of the screenshot it is shown) or 'normalized_1000' "
                "(0-1000 over the screenshot on both axes, x first). The "
                "prompt teaches it; the framework converts every point the "
                "model writes into the units of the capability's point "
                "actions, and writes every point it is shown on this grid."
            ),
        ),
    ] = "screenshot_pixels"
    show_action_effects: Annotated[
        bool,
        PydanticField(
            description=(
                "Show each returned action's measured effect (the one line the "
                "effect describes itself with, e.g. where a click landed or how "
                "far a scroll moved) in the action feedback, restored history "
                "included. Off: no effect reaches the model; the trace still "
                "records every effect, so both arms of an ablation measure the same."
            ),
        ),
    ] = True
    show_capture_state: Annotated[
        bool,
        PydanticField(
            description=(
                "Ask for the optional capture_state fragment (how the environment "
                "stood when captured: settle, foreground change, pointer, failed "
                "visible resources) and show it every step. Off: it is excluded "
                "from this agent's observation demand, so it is neither captured "
                "nor shown."
            ),
        ),
    ] = True

    # ── internal state ─────────────────────────────────────────
    #: The protocol as this run speaks it, built by ``prepare()`` from the
    #: run's action space; before that it binds no verb.
    _codec: UITarsCodec = field(default_factory=UITarsCodec, init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)
    #: The platform the first observation of this execution declared.
    _input_platform_text: str | None = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``) via conscribe's ``WiredField`` descriptors.

    # ── BaseAgent properties ───────────────────────────────────

    @property
    def agent_name(self) -> str:
        return self.__registry_key__

    @property
    def domain(self) -> str:
        return "web_navigation_vision"

    @property
    def system_prompt(self) -> PromptSnapshot | None:
        return self._prompt_snapshot

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(self._codec.space.descriptors)

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
    ) -> list[BaseMessage]:
        """The seed messages for this run, and the codec for its action space.

        ``task`` is written once in the system prompt. The first observed
        platform is another system message, reset here for each execution or
        independently rendered training segment. *action_space* is the run's
        effective one (``AgentRuntime.action_space``): a verb is offered when
        the run's bindings resolve its action, so a short name two
        capabilities offer binds to neither and its verb is not taught.
        ``max_actions_per_step`` comes from the Loop, which is what truncates.
        """
        max_actions_per_step = execution_limits.max_actions_per_step
        if max_actions_per_step is None:
            raise ValueError("prepare requires the Loop's max_actions_per_step execution limit")
        self._codec = UITarsCodec(action_space, writes_thought=self.prompt_template != "grounding")
        self._input_platform_text = None
        action_format, action_budget = _action_contract(
            max_actions_per_step, can_finish="finished" in self._codec.verbs
        )
        text, self._prompt_snapshot = self.render(
            self.prompt_template,
            {
                "task": task,
                "action_space": _render_action_space(self._codec),
                "action_format": action_format,
                "action_budget": action_budget,
                "coordinate_space": (
                    f"{_COORDINATE_PROMPTS[self.coordinate_space]} {_POINT_SYNTAX}"
                ),
            },
        )
        # cache=True: static-prefix breakpoint — see BrowserUseAgent for why it
        # is kept alongside the per-step one.
        return [SystemMessage(content=text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: Any, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """This observation and the preceding batch's execution feedback.

        The first call after prepare also declares the measured platform as
        fixed system context, whatever the step number: an exported segment
        may begin at a nonzero source step. Builders return messages; only
        the Loop or offline renderer writes memory.

        The screenshot is always attached. With no screenshot bytes the step
        degrades to a text placeholder so the LLM call still succeeds, and
        since that step's frame has no grid the framework refuses any point
        in its reply. The pointer is written on this step's frame
        (``context.frame``), the previous batch on its own record's; a pixel
        frame states the screenshot's size.
        """
        platform_text = render_computer_use_platform(observation)
        if self._input_platform_text is not None and platform_text != self._input_platform_text:
            raise ValueError(
                "UI-TARS input platform changed within an execution; prepare a new run"
            )
        prefix: list[BaseMessage] = (
            [SystemMessage(content=platform_text, cache=True)]
            if self._input_platform_text is None
            else []
        )
        step_info = getattr(observation, "step_info", None)
        step_label = (
            f"Step: {step_info.step}/{step_info.max_steps}"
            if step_info is not None
            else f"Step: {context.step + 1}"
        )
        screenshot = getattr(observation, "screenshot", None)
        image_bytes = read_model_image(screenshot)
        text = step_label + "\n" + self._render_action_results(observation.history)
        capture_state = render_capture_state(observation, write_point=point_writer(context.frame))
        if capture_state:
            text += "\n" + capture_state

        if screenshot is None or image_bytes is None:
            messages = place_prompt_contributions(
                [*prefix, UserMessage(content=f"{text}\n(no screenshot available)")],
                context=context,
            )
            self._input_platform_text = platform_text
            return messages

        grid = context.frame.grid if context.frame is not None else None
        if grid is not None and grid.space == "screenshot_pixels":
            text += f"\nScreenshot: {grid.width}x{grid.height} pixels."
        # Labelled from the bytes, not assumed: the capability can be
        # configured to capture JPEG, and a JPEG behind a
        # ``data:image/png`` prefix is rejected outright by Anthropic.
        media_type = image_media_type(image_bytes) or "image/png"
        b64 = base64.b64encode(image_bytes).decode()
        content: list[Any] = [
            ContentPartTextParam(text=text),
            ContentPartImageParam(
                image_url=ImageURL(
                    url=f"data:{media_type};base64,{b64}",
                    detail="high",
                    media_type=media_type,
                ),
            ),
        ]
        messages = place_prompt_contributions(
            [*prefix, UserMessage(content=content)], context=context
        )
        self._input_platform_text = platform_text
        return messages

    def _render_action_results(self, history: Any) -> str:
        """The last batch, without treating proposed actions as completed.

        History is the loop-source contract shared with branch reconstruction
        and export. Earlier batches already have their own messages; the
        singular action_result fragment loses all but the last result.
        Sensitivity markers remain trace-only evidence. A returned result's
        effect is shown only as its ``describe()`` line, and only with
        ``show_action_effects``. Each action is its call as the codec writes it,
        every point on the frame the batch was chosen on (``StepRecord.frame``).
        Actions the Loop skipped because an earlier one failed are
        ``not_executed``, with the reason (``action N failed``); they are not
        among the actions without returned results, which may have run.
        """
        if not history.items:
            return "<action_results>\nNo previous action batch.\n</action_results>"
        record = history.items[-1]
        if len(record.action_results) > len(record.actions):
            raise ValueError("UI-TARS action feedback has more results than actions")
        returned = len(record.action_results)
        unreturned = len(record.actions) - record.skipped_actions
        write_point = point_writer(record.frame)
        codec = self._codec

        def fact(action: Action) -> str | dict[str, Any]:
            return codec.write_fact(fact_in_model_frame(action, record.frame, codec.space))

        facts: dict[str, Any] = {
            "step": record.step + 1,
            "returned_results": [
                {
                    "action": fact(action),
                    "result": result_facts(
                        result, show_effects=self.show_action_effects, write_point=write_point
                    ),
                }
                for action, result in zip(
                    record.actions[:returned], record.action_results, strict=True
                )
            ],
            "actions_without_returned_results": [
                fact(action) for action in record.actions[returned:unreturned]
            ],
            "discarded_action_count": record.truncated_actions,
            "error_phase": record.error_phase or None,
            "error": record.error,
        }
        if record.skipped_actions:
            # The batch stopped at a failed action; the ones after it never
            # ran. Only then is the key written, so a batch that skipped
            # nothing reads as it always has.
            facts["not_executed"] = {
                "actions": [fact(action) for action in record.skipped()],
                "because": skip_reason(record),
            }
        return (
            "<action_results>\n"
            + json.dumps(facts, ensure_ascii=False, sort_keys=True)
            + "\n</action_results>"
        )

    async def build_restored_history(
        self,
        observation: Any,
        *,
        history: RestoredHistory,
        context: ReactPromptContext,
    ) -> list[BaseMessage]:
        """This restored step's observation, then its execution written on that observation's frame.

        The restored actions ran after *observation* was captured, so their
        points (the executed actions', their effect lines' and the recorded
        source actions') are written on its frame (``context.frame``, which the
        restored step's record keeps for the next step's feedback).
        """
        history.validate_execution()
        messages = await self.build_react_prompt(observation, context=context)
        restored = restored_history_message(
            history,
            show_action_effects=self.show_action_effects,
            context=context,
            action_space=self._codec.space,
            write_action=self._codec.write_fact,
        )
        return [*messages, restored]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Every ``Action:`` line of the reply, its points as the model wrote them (``UITarsCodec.read_actions``)."""
        return self._codec.read_actions(llm_output)

    def get_react_output_model(self) -> type[BaseModel] | None:
        # UI-TARS speaks a native text protocol, not structured output.
        return None

    # ── Training-export slots (optional ReAct slots, arch §4.1) ──

    def format_react_output(self, actions: Sequence[Action], *, thought: str | None = None) -> str:
        """The reply for *actions*, their points already on the model's grid (``UITarsCodec.write``)."""
        return self._codec.write(actions, thought=thought)

    def format_action_fact(self, action: Action) -> str | dict[str, Any]:
        """*action* shown as a fact: its call, or its JSON (``UITarsCodec.write_fact``)."""
        return self._codec.write_fact(action)

    @staticmethod
    def parse_react_thought(llm_output: Any) -> tuple[str, str] | None:
        """The reply's ``Thought:`` and ``"Thought"``, or ``None``; needs no run (``codec.read_thought``)."""
        return read_thought(llm_output)
