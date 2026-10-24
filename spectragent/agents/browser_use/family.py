"""The BrowserUse family root: what ``browser_use.dom`` and ``browser_use.dom_vision`` share.

A passive domain-knowledge provider: it knows how to build a browser
prompt and how to read a browser reply. ``ReactLoop`` drives it, woven in
by ``AgentRuntime``.

**A member is a class, never a mode.** ``BrowserUseAgent`` is the abstract
family root (``docs/architecture_design.md`` §4.1 "Agent 家族"): it holds the
prompt building and reply parsing, and registers nothing. Each member writes
its own complete ``__wiring__``, so the trace's ``agent_id`` says what the
model saw and which actions it could take:

========================== ======================== ===========================
member                     in model context         click verbs
========================== ======================== ===========================
``browser_use.dom``        DOM text                 ``click`` (index)
``browser_use.dom_vision`` DOM text + screenshot    ``click`` and
                                                    ``click_coordinate``
========================== ======================== ===========================

**The DOM is required by every member.** Both declare ``dom`` required, so a
capability profile without a DOM (``vision_only``) fails negotiation by
name instead of quietly turning a DOM run into a vision one. A model that
reads only the screenshot is a computer-use agent (``ui_tars``).

The screenshot fragment is required by every member too. It is not only
model input: ``online_mind2web`` and ``lexbench_browser`` score runs from
the screenshots in the trace, so a ``browser_use.dom`` run still captures
them — the member decides what the *model* sees, not what the run records.

**What the model may emit is a closed schema.** ``prepare()`` compiles the
negotiated descriptors into a per-run union (``core/actions/action_union.py``)
whose ``anyOf`` branches are the verbs this run can execute and nothing
else. The prompt advertises the fully-qualified key, the schema permits it,
``env.act`` routes on it and the trace records it — one string the whole way.

**The DOM text describes itself.** The surface fragment does not say what an
element line looks like: it quotes the run's DOM serializer's own description
(``__fragment_formats__``), so the prompt teaches the format the run writes,
``text`` or ``html``.

**Points are the framework's.** A run whose model writes points declares its
grid (``coordinate_space``, on ``browser_use.dom_vision`` only); the framework
converts every point the reply writes and writes every point fed back on the
frame of the step it belongs to (``core/loops/coordinate_frames.py``). This
module calls those writers and never converts a point itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as PydanticField

from spectragent.agents._registrar import AgentBase
from spectragent.agents.browser_use.output_models import reasoning_fields
from spectragent.agents.prompts import load_prompt_fragment
from spectragent.agents.rendering.action_effects import effect_line
from spectragent.agents.rendering.browser_state import render_browser_state
from spectragent.agents.rendering.capture_state import (
    capture_state_demand,
    write_viewport_pixels,
)
from spectragent.agents.rendering.platform import render_browser_platform
from spectragent.agents.rendering.prompt_context import (
    place_prompt_contributions,
    restored_history_message,
)
from spectragent.agents.rendering.skipped_actions import not_executed_lines
from spectragent.core.actions.action_union import action_of, action_union
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.coordinate_frames import fact_in_model_frame, point_writer
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.prompts import hash_text, text_snapshot
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import PointFrame
from spectragent.schemas.prompt import (
    PromptRef,
    PromptSnapshot,
    ReactPromptContext,
    RestoredHistory,
)

#: ``PromptRef.path`` for a prompt that came from YAML rather than the
#: tree. It is not a path, and it deliberately cannot be mistaken for
#: one — the hash still pins the exact text the model was given.
_OVERRIDE_REF_PATH = "<override_system_message>"

#: Variants whose surface fragment is the compact one: the flash template
#: and the three written for a fine-tuned browser-use checkpoint, which say
#: as little as they can because the checkpoint learned the conventions.
#: Every template quotes a surface: the DOM format is the serializer's to
#: describe, so a fine-tuned run reads what this run's serializer writes.
_TERSE_VARIANTS: frozenset[str] = frozenset(
    {
        "system_prompt_flash",
        "system_prompt_browser_use",
        "system_prompt_browser_use_flash",
        "system_prompt_browser_use_no_thinking",
    }
)

#: The reasoning fields a reply's thought is read from, in precedence order
#: (``BrowserUseAgent.parse_react_thought``): the model's own reasoning block
#: first, then the step's goal, then the running memory — the only reasoning
#: field every template teaches (``output_models.REASONING_FIELDS``).
_THOUGHT_FIELDS: tuple[str, ...] = ("thinking", "next_goal", "memory")


@dataclass
class BrowserUseAgent(AgentBase):
    """Browser-use style agent: multi-action, flash mode, the page as DOM text.

    The abstract family root. Its members are ``browser_use.dom``
    (:class:`~spectragent.agents.browser_use.dom.BrowserUseDomAgent`) and
    ``browser_use.dom_vision``
    (:class:`~spectragent.agents.browser_use.dom_vision.BrowserUseDomVisionAgent`).
    """

    __abstract__ = True
    __registry_key__ = "browser_use"

    # ``observation`` and ``action`` are written out by each member, whole:
    # what a member sees and what it can do is the member's definition, and
    # an entry a member writes replaces the root's (the MRO merge, §4.1).
    # Keys are capability-relative on purpose: ``click`` is what this
    # agent knows how to do, ``browser.click`` is who executes it, and
    # negotiation is the only place that can answer the second question.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        "llm": "llm",  # auto-discovery — any registered LLM
    }
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset({"max_actions_per_step"})
    # The DOM text's format is its serializer's to describe: the surface
    # quotes the run's description verbatim, so changing the serializer
    # changes the prompt with it (``docs/architecture_design.md`` §4.4
    # "观测格式由提供方描述"). ``prepare`` receives it as
    # ``fragment_formats["dom"]``; every member requires ``dom``.
    __fragment_formats__: ClassVar[frozenset[str]] = frozenset({"dom"})

    #: The stem of this member's surface fragment under ``_surface/``: how
    #: the system prompt says this run sees the page.
    _SURFACE: ClassVar[str]

    # ── config (conscribe visible) ─────────────────────────────
    # ``max_steps`` / ``max_actions_per_step`` / ``timeout_seconds`` are
    # deliberately absent: the Loop owns and enforces them. AgentRuntime
    # hands the loop's limits to ``prepare()`` so the prompt advertises
    # exactly what will be executed.
    flash_mode: Annotated[bool, PydanticField(description="Skip thinking for speed")] = True
    use_thinking: Annotated[bool, PydanticField(description="Enable extended thinking")] = True
    is_browser_use_model: Annotated[
        bool, PydanticField(description="Using fine-tuned browser-use model")
    ] = False
    override_system_message: Annotated[
        str | None,
        PydanticField(
            description=(
                "Replace the entire system prompt with this text. None (the default): "
                "the agent's own prompt."
            )
        ),
    ] = None
    extend_system_message: Annotated[
        str | None,
        PydanticField(
            description="Append this text to the system prompt. None (the default): nothing."
        ),
    ] = None
    show_action_effects: Annotated[
        bool,
        PydanticField(
            description=(
                "Show each executed action's measured effect (the one line the "
                "effect describes itself with, e.g. where a click landed or how "
                "far a scroll moved) in the agent history, restored history "
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
                "visible resources) and show it in the browser state every step. "
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
                "changes the prompt. Needs the screenshot fragment; without one the "
                "block says the state was not measured."
            )
        ),
    ] = False
    # Counts *steps*, each with its reply, actions, results and effects;
    # Skyvern's ``action_history_window`` counts executed actions.
    step_history_window: Annotated[
        int,
        PydanticField(
            ge=1,
            description=(
                "Number of most recent steps, each with its reply, actions, results and "
                "effects, replayed in the agent history of every step's message."
            ),
        ),
    ] = 10

    # ── internal state populated by prepare() ──────────────────
    # ``task`` lives on every observation as ``obs.task.user_request``
    # (loop-source fragment), so the agent never holds its own copy.
    _action_space: ActionSpace = field(default_factory=ActionSpace, init=False, repr=False)
    _per_run_output_model: type[BaseModel] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``). conscribe's metaclass attaches ``WiredField``
    # descriptors at class creation; the Composer populates them via
    # setattr at wire time. Reading them before injection raises
    # ``AttributeError`` with a clear message.

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
        """Domain action descriptors. The Capability owns the actual
        executable action set; this method just exposes them for
        introspection."""
        return list(self._action_space.descriptors)

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
        """Pure builder: produce the seed messages for this run.

        AgentRuntime invokes this once after negotiation and writes the
        return value into memory; the agent does no memory IO itself.
        ``task`` flows through every step's observation as
        ``obs.task.user_request`` — not stored on ``self``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates. The agent must not carry its own copy: the
        prompt has to promise exactly what the loop will run.

        *action_space* is the run's effective action space, the one the
        guard executes (``AgentRuntime.action_space``): its descriptors
        arrive already narrowed by negotiation against this member's wiring,
        so there is nothing to filter here, and its bindings say what a name
        means in this run. What this does build is the per-run output model:
        a closed union over exactly these verbs, which is what makes "the
        prompt advertises what the run can execute" true of the *schema* as
        well as the prose.

        *fragment_formats* holds the run's description of the DOM text
        (``"dom"``), written by its DOM serializer; the surface quotes it in
        place of a format of its own.

        Raises:
            UnsupportedParamsSchema: an action's params are outside the
                closed-union subset — before the first LLM call, not at it.
        """
        max_actions_per_step = execution_limits.max_actions_per_step
        if max_actions_per_step is None:
            raise ValueError("prepare requires the Loop's max_actions_per_step execution limit")
        self._action_space = action_space
        variant = self.select_prompt_variant()
        self._per_run_output_model = self._build_per_run_output_model(variant)
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            variant,
            max_actions_per_step,
            fragment_formats,
        )
        # ``cache=True`` marks the static prefix. The per-step breakpoint on the
        # user message (``build_react_prompt``) already covers system + history,
        # so in the steady state this second one is redundant — it earns its
        # place when the tail entry is gone: a step that took longer than the
        # 5-minute TTL, or a memory strategy that evicts from the front and so
        # rewrites the prefix the tail breakpoint was written against. A read
        # can only land where some earlier request wrote a breakpoint, and
        # without this one the fallback is *no* read at all.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: Any, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """Pure builder: return the messages to append for this step.

        Emits an upstream-browser-use-style structured user message:
        ``<agent_history>...</agent_history><agent_state>...</agent_state>
        <browser_state>...</browser_state>``. ``cache=True`` lets
        Anthropic prefix-cache the (mostly stable) task/state header.
        Whether the screenshot goes with it is the member's
        (:meth:`_step_messages`).

        It records no state. The pointer is written on this step's frame
        (``context.frame``); each history step's actions and effects on the
        frame that step's record keeps.
        """
        messages = self._step_messages(observation, context.frame)
        return place_prompt_contributions(messages, context=context)

    async def build_restored_history(
        self,
        observation: Any,
        *,
        history: RestoredHistory,
        context: ReactPromptContext,
    ) -> list[BaseMessage]:
        """Render the live page and actual restoration without parsing the old reply.

        The restored actions ran after *observation* was captured, so their
        points — the executed actions', their effect lines' and the recorded
        source actions' — are written on its frame (``context.frame``) when
        this run teaches a grid; each action as its JSON.
        """
        history.validate_execution()
        messages = await self.build_react_prompt(observation, context=context)
        restored = restored_history_message(
            history,
            show_action_effects=self.show_action_effects,
            context=context,
            action_space=self._action_space,
        )
        return [*messages, restored]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Validate a reply against this run's closed schema, as Actions.

        Both paths — a provider-validated instance and free-form JSON text
        — go through the *same* per-run model, so a run that falls back to
        text is held to the same verb set and the same parameter types as
        one with constrained decoding.

        Raises:
            ParseError: anything the model can fix by writing a different
                reply. The Loop feeds the detail back verbatim, so each
                one names a single problem.
        """
        model = self._per_run_output_model
        if model is None:
            raise ParseError(
                f"{self.agent_id}.parse_react_output ran before prepare(), so there "
                "is no negotiated action set to validate against.",
                raw_output=str(llm_output)[:500],
            )

        parsed: Any = llm_output
        if not isinstance(llm_output, model):
            text = llm_output if isinstance(llm_output, str) else str(llm_output)
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ParseError(
                    f"LLM output is not valid JSON: {exc}",
                    raw_output=text[:500],
                ) from exc
            try:
                parsed = model.model_validate(data)
            except Exception as exc:
                raise ParseError(
                    f"LLM JSON does not match this run's output schema: "
                    f"{type(exc).__name__}: {exc}",
                    raw_output=text[:500],
                ) from exc

        # ``action_of`` drops a null only where it declines an optional param
        # (``clear: null`` must not override ``clear_by_default``) and keeps a
        # required one (``done``'s ``extracted_content: null`` is the answer).
        # Points stay as the model wrote them; the framework moves them into
        # the capability's units with this step's frame.
        return [action_of(item) for item in parsed.action]

    def get_react_output_model(self) -> type[BaseModel]:
        """The closed per-run model, once ``prepare()`` has built it.

        Never ``None``: an agent that has been prepared always has a verb
        set, and one that has not cannot answer. The Loop's slot accepts
        ``type[BaseModel] | None`` for agents like UI-TARS that speak a
        text protocol; narrowing it here is what lets every caller use the
        result without a null check.

        Raises:
            RuntimeError: called before ``prepare()``. Returning a looser
                fallback instead is how the open ``list[dict[str, Any]]``
                schema kept reaching providers unnoticed.
        """
        if self._per_run_output_model is None:
            raise RuntimeError(
                f"{self.agent_id}.get_react_output_model() was called before "
                "prepare(); the output schema is built from the negotiated "
                "action set, which does not exist yet."
            )
        return self._per_run_output_model

    # ── Training-export slot (optional ReAct slot, arch §4.1) ──

    @staticmethod
    def parse_react_thought(llm_output: Any) -> tuple[str, str] | None:
        """The thought of a recorded reply and the field it came from, or ``None``.

        The first non-empty of :data:`_THOUGHT_FIELDS` — ``thinking``, then
        ``next_goal``, then ``memory`` — stripped. Which ones a reply can
        carry is its template's (``output_models.REASONING_FIELDS``): a flash
        template teaches only ``memory``, so a flash teacher's thought is its
        memory line, and the returned field name says so. Pure: reads the
        reply only (the JSON text a run records, or the parsed model), so the
        training export calls it on the class.
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
        if not isinstance(data, Mapping):
            return None
        for name in _THOUGHT_FIELDS:
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip(), name
        return None

    # ── member hooks ───────────────────────────────────────────

    def _step_messages(self, observation: Any, frame: PointFrame | None) -> list[BaseMessage]:
        """This step's user message: the page as text.

        The screenshot is captured and recorded, never shown; a member
        that shows it overrides this.
        """
        return [UserMessage(content=self._build_step_text(observation, frame), cache=True)]

    def _render_surface(self, variant: str, fragment_formats: Mapping[str, str]) -> str:
        """This member's description of how the run sees and reaches the page.

        Loaded from ``prompts/agents/browser_use/_surface/`` — a
        subdirectory, so ``agent_prompt_variants`` (which globs the top
        level) cannot mistake a fragment for a template variant. The
        fragment holds one slot, ``{dom_format}``: the run's DOM
        serializer's own description of its text (*fragment_formats*
        ``["dom"]``), quoted verbatim, so the surface never describes a
        format the run does not write.
        """
        stem = f"{self._SURFACE}_terse" if variant in _TERSE_VARIANTS else self._SURFACE
        text, _ref = load_prompt_fragment(self.__registry_key__, "_surface", stem)
        return text.strip().format(dom_format=fragment_formats["dom"])

    # ── private helpers ────────────────────────────────────────

    def _build_per_run_output_model(self, variant: str) -> type[BaseModel]:
        """Compose this run's output model: taught reasoning + closed action.

        The reasoning fields are the ones *this variant's* ``<output>``
        block teaches and no others. Under OpenAI's strict dialect every
        property becomes required, so a flash run carrying the full field
        set would be forced to emit a ``thinking`` block its prompt never
        asked for.
        """
        item, _models = action_union(self._action_space.descriptors)
        allowed = ", ".join(d.name for d in self._action_space.descriptors)
        # Dynamic field definitions are valid at runtime but exceed Pydantic's
        # overloads. Keep this typing escape local to the model construction.
        create_output_model: Any = create_model
        return create_output_model(
            "BrowserUseOutputForRun",
            # Providers and gateways inject metadata of their own
            # (anthropic's ``thought_signature``); refusing it here would
            # fail a reply that is otherwise exactly right.
            __config__=ConfigDict(extra="allow"),
            **reasoning_fields(variant),
            action=(
                list[item],  # type: ignore[valid-type]
                PydanticField(
                    min_length=1,
                    description=(
                        f"Actions to run this step, in order. Each is an object "
                        f"naming one verb and giving it an object of parameters, "
                        f"e.g. {{'browser.navigate': {{'url': 'https://...'}}}}. "
                        f"Allowed verbs: {allowed}."
                    ),
                ),
            ),
        )

    def select_prompt_variant(self) -> str:
        """Which template on disk this configuration reads.

        Public because it is the only description of this agent's prompt
        surface: it has no ``prompt_template`` field, because its choice
        is a *function* of the model, not a free one. Asking YAML for both
        ``flash_mode: true`` and ``prompt_template: system_prompt_flash``
        would be two sources of truth that can disagree.
        ``tests/test_agents/test_prompt_tree.py`` enumerates this over the
        whole config matrix to prove every file in the directory is
        reachable and none is orphaned.

        The member is deliberately *not* an axis here: the members share one
        prompt tree and differ only by the surface fragment spliced into it,
        so a DOM / DOM-plus-screenshot ablation cannot be confounded by two
        separately worded templates.

        Neither is the model *version*, and that is a deletion, not an
        omission. Upstream browser-use keys a separate, deliberately
        9x-longer Anthropic template off Opus 4.5 / Haiku 4.5 for one
        reason: those two models need a >=4096-token prefix before a
        prompt-cache entry is written at all, so the prompt is padded to
        clear the bar. That premise does not survive the port. The
        threshold applies to the whole prefix ending at the breakpoint,
        and this loop's breakpoint sits on the newest user message —
        which carries a serialized DOM, and past step 1 the entire
        history as well. The bar is cleared by the observation, not by
        the system prompt, so padding bought nothing here.

        What it cost was reproducibility, which matters more: the same
        YAML run against two models silently rendered two different
        system prompts, with nothing in the config to show it. On an
        evaluation platform the prompt is an independent variable, so it
        may vary with what the agent *is* (its member, its verbs) but never
        with which model happens to be behind it.
        """
        if self.is_browser_use_model:
            if self.flash_mode:
                return "system_prompt_browser_use_flash"
            elif self.use_thinking:
                return "system_prompt_browser_use"
            else:
                return "system_prompt_browser_use_no_thinking"
        elif self.flash_mode:
            return "system_prompt_flash"
        elif self.use_thinking:
            return "system_prompt"
        else:
            return "system_prompt_no_thinking"

    def _build_system_prompt(
        self,
        variant: str,
        max_actions_per_step: int,
        fragment_formats: Mapping[str, str],
    ) -> tuple[str, PromptSnapshot]:
        """Return the system prompt and the snapshot that explains it."""
        snapshot: PromptSnapshot
        if self.override_system_message:
            text = self.override_system_message
            snapshot = text_snapshot(
                text,
                PromptRef(path=_OVERRIDE_REF_PATH, content_hash=hash_text(text)),
            )
        else:
            text, snapshot = self.render(
                variant,
                {
                    "max_actions": max_actions_per_step,
                    "action_list": _render_action_list(self._action_space.descriptors),
                    "surface": self._render_surface(variant, fragment_formats),
                },
            )

        if self.extend_system_message:
            # The extension is appended *after* substitution, so the
            # recorded template no longer reproduces what the model saw.
            # Record the text instead; the ref still names its source.
            text += f"\n{self.extend_system_message}"
            snapshot = text_snapshot(text, snapshot.ref)
        return text, snapshot

    def _build_step_text(
        self, observation: Any, frame: PointFrame | None, *, screenshot_note: str = ""
    ) -> str:
        """Compose the per-step XML user-message body.

        Layout (upstream browser-use prompts.py:400-416)::

            <agent_history>...</agent_history>
            <agent_state>
              <user_request>...</user_request>
              <follow_up_requests>...</follow_up_requests>  (only if any)
              <step_info>...</step_info>                    (only if known)
            </agent_state>
            <browser_state>URL/Title/Tabs/capture state/DOM</browser_state>

        *screenshot_note* ends the browser state: what a member that shows
        the screenshot says about it.

        Scratchpad blocks (file_system, todo, plan, sensitive_data,
        page_stats, recent_events, ...) are intentionally omitted —
        their data sources land with the future ScratchpadCapability.
        """
        history_xml = self._render_history(getattr(observation, "history", None))
        state_xml = self._render_agent_state(observation)
        browser_xml = self._render_browser_state(observation, frame) + screenshot_note
        return (
            f"<agent_history>\n{history_xml}\n</agent_history>\n"
            f"{state_xml}\n"
            f"{render_browser_platform(observation)}\n"
            f"<browser_state>\n{browser_xml}\n</browser_state>"
        )

    def _render_agent_state(self, observation: Any) -> str:
        task = observation.task
        parts = [f"<user_request>\n{task.user_request}\n</user_request>"]
        if task.follow_ups:
            joined = "\n".join(f"- {f}" for f in task.follow_ups)
            parts.append(f"<follow_up_requests>\n{joined}\n</follow_up_requests>")
        step_info = getattr(observation, "step_info", None)
        if step_info is not None:
            parts.append(
                f"<step_info>step {step_info.step}/{step_info.max_steps}, "
                f"today={step_info.today}</step_info>"
            )
        body = "\n".join(parts)
        return f"<agent_state>\n{body}\n</agent_state>"

    def _render_history(self, history: Any) -> str:
        """What this run already did: the last ``step_history_window`` steps.

        With ``show_action_effects`` on, a step whose results measured
        effects gets an ``<effects>`` element after ``<results>``: one line
        per such result, numbered by the action's position in the step and
        named by it, holding the effect's ``describe()`` line. A step with
        no effect to show renders exactly as before. An effect line is a
        measured page fact and may name an element by its role and
        accessible name (what a click hit, where focus went).

        A step the Loop stopped at a failed action gets a ``<not_executed>``
        element after ``<results>``, one sentence per action it skipped
        (``Action 2 (browser.send_keys) was not executed: action 1 failed.``,
        ``agents/rendering/skipped_actions.py``); a step that skipped nothing
        renders as before.

        In a run that teaches a grid, every point a step's actions and
        effects state is on the frame that step's record keeps
        (``StepRecord.frame``), put there by the framework; the
        ``<llm_output>`` is the model's own reply, already on it.
        """
        items = list(getattr(history, "items", []) or [])
        if not items:
            return "<step_1>\n(no prior steps)\n</step_1>"
        blocks: list[str] = []
        for rec in items[-self.step_history_window :]:
            actions_str = (
                ", ".join(
                    f"{a.name}({fact_in_model_frame(a, rec.frame, self._action_space).params})"
                    for a in rec.actions
                )
                or "(none)"
            )
            write_point = point_writer(rec.frame)
            results_str = (
                "; ".join(
                    f"ok={r.success} {r.error or r.extracted_content or ''}".rstrip()
                    for r in rec.action_results
                )
                or "(none)"
            )
            effects = [
                f"{position}. {action.name}: {line}"
                for position, (action, result) in enumerate(
                    zip(rec.actions, rec.action_results, strict=False), start=1
                )
                if (
                    line := effect_line(
                        result, shown=self.show_action_effects, write_point=write_point
                    )
                )
                is not None
            ]
            effects_xml = "<effects>\n" + "\n".join(effects) + "\n</effects>\n" if effects else ""
            # The step stopped at a failed action: name the ones after it,
            # whose params ``<actions>`` already shows.
            not_executed = " ".join(not_executed_lines(rec, write=lambda action: action.name))
            not_executed_xml = (
                f"<not_executed>{not_executed}</not_executed>\n" if not_executed else ""
            )
            tag = f"step_{rec.step + 1}"
            blocks.append(
                f"<{tag}>\n"
                f"<llm_output>{_truncate(rec.llm_output, 2000)}</llm_output>\n"
                f"<actions>{actions_str}</actions>\n"
                f"<results>{results_str}</results>\n"
                f"{not_executed_xml}"
                f"{effects_xml}"
                f"</{tag}>"
            )
        return "\n".join(blocks)

    def _render_browser_state(self, observation: Any, frame: PointFrame | None) -> str:
        """Page state (URL / Title / Tabs / capture state), then the DOM.

        The page state is the shared browser writing,
        ``render_browser_state``. The pointer is written on this step's
        *frame* when this run teaches a grid (a point with no place there
        is said to be so), and in viewport pixels when it writes no point
        (no frame).
        """
        parts: list[str] = []
        write_point = point_writer(frame, viewport=write_viewport_pixels)
        page_state = render_browser_state(
            observation, show_media_state=self.show_media_state, write_point=write_point
        )
        if page_state:
            parts.append(page_state)

        dom = getattr(observation, "dom", None)
        element_tree = getattr(dom, "element_tree", None) if dom is not None else None
        if element_tree is not None:
            serialized = getattr(element_tree, "serialized_dom", None)
            dom_str = serialized if isinstance(serialized, str) else str(element_tree)
            parts.append(f"DOM:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty browser state)"


def _render_action_list(descriptors: list[ActionDescriptor]) -> str:
    """Render action descriptors as a markdown bullet list for the prompt.

    Each line: ``- `<descriptor_name>(<params>)` — <description>``.

    The descriptor's own name is used verbatim, so the prompt advertises
    exactly the vocabulary the closed schema permits, ``env.act`` routes
    on, and the trace records. There is no alias table and no local-id
    fallback: a name the model writes is a name the run holds.

    Param text is verbatim too. A point param already says the grid the
    model writes it on: the framework framed the run's descriptors
    (``core/loops/coordinate_frames.py::framed_action_space``).

    Empty list yields an empty string — only in degenerate fixtures;
    ``prepare()`` refuses to build a schema with no verbs in it.
    """
    if not descriptors:
        return ""
    lines: list[str] = []
    for d in descriptors:
        if d.params:
            params_part = ", ".join(f"{k}: {v}" for k, v in d.params.items())
        else:
            params_part = "no params"
        lines.append(f"- `{d.name}({params_part})` — {d.description}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated)"
