"""BrowserUseAgent — one browser agent, three observation modes.

A passive domain-knowledge provider: it knows how to build a browser
prompt and how to read a browser reply. ``ReactLoop`` drives it, woven in
by ``AgentRuntime``.

**The mode is configured, never inferred.** ``observation_mode`` says
whether this run reads the page as DOM text, as DOM text plus a
screenshot, or as a screenshot alone. It picks the surface fragment the
prompt is built from *and* narrows what the agent takes into negotiation,
so the three follow from one enum value:

============ ================= =============================== ===========
mode         in model context  click verbs                     DOM reached
============ ================= =============================== ===========
dom          DOM text          ``click`` (index)               yes
dom_vision   DOM text + image  ``click`` and ``click_coordinate``, yes
                               independently
vision       image             ``click_coordinate`` only       never
============ ================= =============================== ===========

Deriving the mode from the environment instead — "there is no ``dom``
fragment, so this must be a vision run" — would make a mistyped capability
profile silently downgrade a DOM experiment into a vision one. Here a DOM
mode keeps ``dom`` *required*, so that mistake is an
``IncompatibleFragmentError`` naming the mode that asked for it.

The screenshot fragment is required in **every** mode. It is not only
model input: ``online_mind2web`` and ``lexbench_browser`` score runs from
the screenshots in the trace, so a DOM-mode run still captures them — the
mode decides what the *model* sees, not what the run records.

**What the model may emit is a closed schema.** ``prepare()`` compiles the
negotiated descriptors into a per-run union (``agents/_action_union.py``)
whose ``anyOf`` branches are the verbs this run can execute and nothing
else. There is no vocabulary map and no local-id fallback: the prompt
advertises the fully-qualified key, the schema permits it, ``env.act``
routes on it and the trace records it — one string the whole way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.agents._action_union import _create_model, action_union
from spectragent.agents._browser_platform import render_browser_platform
from spectragent.agents._output_models import reasoning_fields
from spectragent.agents._registrar import AgentBase
from spectragent.agents.prompts import load_prompt_fragment
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.errors import ParseError
from spectragent.core.negotiation import bounded_demand
from spectragent.prompts import hash_text, text_snapshot
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.observation.geometry import (
    CoordinateSpace,
    PointGrid,
    ScreenshotGeometry,
    point_grid,
)
from spectragent.schemas.observation.image import image_media_type
from spectragent.schemas.prompt import PromptRef, PromptSnapshot

#: ``PromptRef.path`` for a prompt that came from YAML rather than the
#: tree. It is not a path, and it deliberately cannot be mistaken for
#: one — the hash still pins the exact text the model was given.
_OVERRIDE_REF_PATH = "<override_system_message>"


@dataclass(frozen=True)
class _ModeDemand:
    """How one mode narrows the class ``__wiring__``, per registry.

    ``promote`` moves a declared optional key into required; ``exclude``
    drops one. Both may name **only** the class's optional keys: the
    optional set is precisely the space the modes disagree about, and a
    required key is one no mode may touch.
    """

    promote: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()

    def apply(
        self, required: set[str], optional: set[str], *, where: str
    ) -> tuple[set[str], set[str]]:
        """This mode's ``(required, optional)``, from the class's own.

        The check is here rather than left to ``bounded_demand`` because
        the two failures are not symmetrical. A stray ``promote`` grows
        the required set and leaves the declared space, so it would be
        caught downstream anyway; a stray ``exclude`` subtracts a key
        nothing has — a misspelling, or a required key a mode may not drop
        — and produces sets that are perfectly legal and quietly wrong.
        The mode table is the single definition of the three surfaces, so
        a typo in it has to be an error, not a shrug.
        """
        stray = sorted((self.promote | self.exclude) - optional)
        if stray:
            raise ValueError(
                f"{where}: {stray} is not an optional key of "
                f"BrowserUseAgent.__wiring__ (optional: {sorted(optional)}). A "
                f"mode narrows only inside the optional set — it can neither "
                f"invent a key nor release one the class declared required."
            )
        return required | self.promote, (optional - self.promote) - self.exclude


#: Mode → registry → how it narrows. A module-level immutable table, not
#: state: it is read, never written, and every run builds its own agent.
_MODE_DEMAND: Mapping[str, Mapping[str, _ModeDemand]] = MappingProxyType(
    {
        "dom": MappingProxyType(
            {
                "observation": _ModeDemand(promote=frozenset({"dom"})),
                "action": _ModeDemand(
                    promote=frozenset({"click", "type"}),
                    # The coordinate verbs read a point off an image this
                    # mode never shows the model. Previously they were
                    # negotiated, executable and simply never taught —
                    # the guard would have run one the model could not
                    # have known to ask for.
                    exclude=frozenset({"click_coordinate", "type_coordinate", "drag"}),
                ),
            }
        ),
        "dom_vision": MappingProxyType(
            {
                "observation": _ModeDemand(promote=frozenset({"dom"})),
                "action": _ModeDemand(
                    promote=frozenset({"click", "type"}),
                    # Two *click* verbs, kept distinct on purpose; typing
                    # and dragging stay index-addressed here, where an
                    # index exists.
                    exclude=frozenset({"type_coordinate", "drag"}),
                ),
            }
        ),
        "vision": MappingProxyType(
            {
                "observation": _ModeDemand(exclude=frozenset({"dom"})),
                "action": _ModeDemand(
                    # ``send_keys`` presses keys; it cannot type text into
                    # a field nobody focused, so ``type_coordinate`` is
                    # required rather than optional.
                    promote=frozenset({"click_coordinate", "type_coordinate"}),
                    # ``extract_content`` returns innerText or outerHTML —
                    # DOM-derived text under another name. Letting it in
                    # would make "vision-only" mean "vision plus a text
                    # channel", and every ablation drawn against it wrong.
                    exclude=frozenset({"click", "type", "extract_content"}),
                ),
            }
        ),
    }
)

#: Which modes attach the screenshot to the model's message.
_MODES_WITH_IMAGE: frozenset[str] = frozenset({"dom_vision", "vision"})

#: Which modes put the DOM tree in the model's message.
_MODES_WITH_DOM: frozenset[str] = frozenset({"dom", "dom_vision"})

#: Local id → the ``(x_param, y_param)`` pairs carrying a point the model
#: reads off the *screenshot* while the capability executes it in its own
#: viewport units. The param names are the descriptor's own; only the unit
#: differs between what the model is taught and what the executor
#: declares, and :meth:`BrowserUseAgent._points_to_viewport` is the one
#: place that converts.
_POINT_PARAMS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "click_coordinate": (("coordinate_x", "coordinate_y"),),
        "type_coordinate": (("coordinate_x", "coordinate_y"),),
        "drag": (("start_x", "start_y"), ("end_x", "end_y")),
    }
)

#: What a coordinate param means, per grid. ``{axis}`` is ``x`` or ``y``
#: and ``{edge}`` the edge it is measured from, so one sentence covers
#: ``coordinate_x``, ``start_y`` and the rest.
_POINT_DESCRIPTIONS: Mapping[CoordinateSpace, str] = MappingProxyType(
    {
        "screenshot_pixels": (
            "int -- {axis} in pixels of the screenshot you are shown, from its {edge} edge"
        ),
        "normalized_1000": (
            "int -- {axis} on a 0-1000 grid laid over the screenshot whatever its "
            "pixel size, from its {edge} edge"
        ),
    }
)

#: The paragraph the prompt adds when this run has a coordinate action.
#: Said in code rather than in the surface fragment because it depends on
#: ``coordinate_space``, which is config — the same split UI-TARS makes.
_COORDINATE_GUIDE: Mapping[CoordinateSpace, str] = MappingProxyType(
    {
        "screenshot_pixels": (
            "<coordinates>\n"
            "A point is (x, y) in pixels of the screenshot you are shown: (0, 0) "
            "is its top-left corner, x grows to the right and y downwards. Every "
            "step states the screenshot's size in pixels. Write the numbers only "
            "— nothing there names an element, it only says where one is.\n"
            "</coordinates>"
        ),
        "normalized_1000": (
            "<coordinates>\n"
            "A point is (x, y) on a 0-1000 grid laid over the screenshot, whatever "
            "its size in pixels: (0, 0) is the top-left corner, (1000, 1000) the "
            "bottom-right corner, x grows to the right and y downwards. Write the "
            "numbers only — nothing there names an element, it only says where one "
            "is.\n"
            "</coordinates>"
        ),
    }
)

#: Template stems written for a fine-tuned browser-use checkpoint. They
#: carry no surface description at all (the checkpoint learned the DOM
#: format), so they take no surface fragment — and are refused outside a
#: DOM mode, where that assumption no longer holds.
_FINE_TUNED_VARIANTS: frozenset[str] = frozenset(
    {
        "system_prompt_browser_use",
        "system_prompt_browser_use_flash",
        "system_prompt_browser_use_no_thinking",
    }
)

#: Variants whose surface fragment is the compact one.
_TERSE_VARIANTS: frozenset[str] = frozenset(
    {
        "system_prompt_flash",
    }
)


def _local_id(name: str) -> str:
    return name.rsplit(".", 1)[-1]


#: The reasoning fields a reply's thought is read from, in precedence order
#: (``BrowserUseAgent.parse_react_thought``): the model's own reasoning block
#: first, then the step's goal, then the running memory — the only reasoning
#: field every template teaches (``_output_models.REASONING_FIELDS``).
_THOUGHT_FIELDS: tuple[str, ...] = ("thinking", "next_goal", "memory")


@dataclass
class BrowserUseAgent(AgentBase):
    """Browser-use style agent with multi-action, flash mode and three surfaces."""

    __registry_key__ = "browser_use"

    # ── Cross-registry wiring ────────────────────────────────────
    # The declared space, across *all* modes. Required here means "no
    # mode can do without it"; optional means "some mode promotes it and
    # some mode excludes it", and ``_MODE_DEMAND`` is where that is said.
    # conscribe compiles this into the generated config's ``Literal``, so
    # it is also the boundary ``negotiation_demand`` may not cross.
    #
    # Keys are capability-relative on purpose: ``click`` is what this
    # agent knows how to do, ``browser.click`` is who executes it, and
    # negotiation is the only place that can answer the second question.
    # There is no ``capability`` entry for the same reason — naming one
    # constrained the generated config to a provider without any runtime
    # effect.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        # ``screenshot`` is required in every mode because the evaluators
        # read it out of the trace, not because the model always sees it.
        "observation": {
            "registry": "observation",
            "keys": ["screenshot", "platform", "task"],
            "optional_keys": ["dom", "browser_meta", "step_info", "history"],
            "declare_only": True,
        },
        "action": {
            "registry": "action",
            "keys": ["navigate", "done"],
            "optional_keys": [
                # Index-addressed and point-addressed verbs are never
                # merged: ``click`` takes a selector-map index, which only
                # exists when ``browser.dom`` was negotiated;
                # ``click_coordinate`` takes a point on the screenshot,
                # which reaches what an index cannot (a cross-origin iframe
                # widget). One overloaded verb would have half-inapplicable
                # params in either mode.
                "click",
                "type",
                "click_coordinate",
                "type_coordinate",
                "drag",
                "scroll",
                "send_keys",
                "extract_content",
                "go_back",
                "go_forward",
                "switch_tab",
                "wait",
            ],
            "declare_only": True,
        },
        "llm": "llm",  # auto-discovery — any registered LLM
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``max_steps`` / ``max_actions_per_step`` / ``timeout_seconds`` are
    # deliberately absent: the Loop owns and enforces them. AgentRuntime
    # hands the loop's limits to ``prepare()`` so the prompt advertises
    # exactly what will be executed.
    #
    observation_mode: Annotated[
        Literal["dom", "dom_vision", "vision"],
        PydanticField(
            description=(
                "What reaches the model: 'dom' (the element tree as text), "
                "'dom_vision' (that plus a screenshot), or 'vision' (a "
                "screenshot alone — no DOM is extracted, and the only click "
                "verb is a coordinate one). Screenshots are captured and "
                "recorded in every mode."
            ),
        ),
    ] = "dom"
    coordinate_space: Annotated[
        Literal["screenshot_pixels", "normalized_1000"],
        PydanticField(
            description=(
                "The grid the model writes points on: 'screenshot_pixels' "
                "(pixels of the screenshot it is shown) or 'normalized_1000' "
                "(0-1000 over the screenshot on both axes, x first). The "
                "prompt teaches it; the agent converts every point into the "
                "units of the capability's point actions. Unused when this "
                "run negotiated no coordinate action."
            ),
        ),
    ] = "screenshot_pixels"
    flash_mode: Annotated[bool, PydanticField(description="Skip thinking for speed")] = True
    vision_detail: Annotated[
        Literal["auto", "low", "high"], PydanticField(description="Screenshot detail level")
    ] = "low"
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

    # ── internal state populated by prepare() ──────────────────
    # ``task`` lives on every observation as ``obs.task.user_request``
    # (loop-source fragment), so the agent never holds its own copy.
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    _per_run_output_model: type[BaseModel] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)
    _max_history_steps: int = field(default=10, init=False, repr=False)
    #: The grid over the screenshot attached to the *current* step's
    #: prompt, set by ``build_react_prompt`` and read by
    #: ``parse_react_output`` — the loop calls the two in that order within
    #: one step, so a point in the reply is always converted with the image
    #: that reply looked at. ``None`` when this step showed the model no
    #: measurable screenshot.
    _grid: PointGrid | None = field(default=None, init=False, repr=False)

    # ``llm`` and ``memory`` are wired from ``__wiring__`` (inherited from
    # ``AgentBase``). conscribe's metaclass attaches ``WiredField``
    # descriptors at class creation; the Composer populates them via
    # setattr at wire time. Reading them before injection raises
    # ``AttributeError`` with a clear message.

    def __post_init__(self) -> None:
        # A cross-field check, so it fails while the Composer is building
        # the object graph — earlier than negotiation, and long before the
        # first LLM call. The three fine-tuned templates describe no
        # surface at all because the checkpoint learned the DOM format;
        # pointing one at a vision run would ship a prompt that never
        # mentions how this run sees the page.
        if self.observation_mode == "vision" and self.is_browser_use_model:
            raise ValueError(
                "browser_use: is_browser_use_model=true selects a prompt written "
                "for a checkpoint fine-tuned on the DOM element tree, which "
                "observation_mode='vision' never produces. Use 'dom' or "
                "'dom_vision', or turn is_browser_use_model off."
            )

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
        return list(self._action_descriptors)

    # ── Negotiation ────────────────────────────────────────────

    def negotiation_demand(self, registry: str) -> tuple[set[str], set[str]]:
        """This instance's demand, narrowed by ``observation_mode``.

        The mode promotes some of the class's optional keys and excludes
        others; ``bounded_demand`` then checks the result never left the
        declared space. Negotiation itself is untouched — it receives one
        ``(required, optional)`` pair and cannot tell that a mode chose it.

        Both checks run even though ``demand_of`` would run the second
        one anyway. They answer different questions: ``_ModeDemand.apply``
        blames the mode table, ``bounded_demand`` blames the instance.
        """
        required, optional = super().negotiation_demand(registry)
        demand = _MODE_DEMAND[self.observation_mode].get(registry)
        if demand is None:
            return required, optional
        narrowed = demand.apply(
            required,
            optional,
            where=f"_MODE_DEMAND[{self.observation_mode!r}][{registry!r}]",
        )
        return bounded_demand(self, registry, *narrowed)

    def __repr__(self) -> str:
        # This exact shape is what ``demand_label`` looks for: a repr that
        # begins with the registry key is used whole in a negotiation
        # diagnosis, so ``IncompatibleFragmentError`` says
        # ``dom ← browser_use(observation_mode='dom')`` instead of leaving
        # "required fragment 'dom' is missing" as a puzzle. A dataclass's
        # generated repr would dump the LLM and the memory into the
        # message; this says the one thing that chose the demand.
        return f"browser_use(observation_mode={self.observation_mode!r})"

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
        max_actions_per_step: int = 1,
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        AgentRuntime invokes this once after negotiation and writes the
        return value into memory; the agent does no memory IO itself.
        ``task`` flows through every step's observation as
        ``obs.task.user_request`` — not stored on ``self``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates. The agent must not carry its own copy: the
        prompt has to promise exactly what the loop will run.

        The descriptors arrive already narrowed by negotiation, which the
        mode steered — so there is nothing to filter here. What this does
        build is the per-run output model: a closed union over exactly
        these verbs, which is what makes "the prompt advertises what the
        run can execute" true of the *schema* as well as the prose.

        Raises:
            UnsupportedParamsSchema: an action's params are outside the
                closed-union subset — before the first LLM call, not at it.
        """
        self._action_descriptors = list(action_descriptors)
        self._grid = None
        variant = self.select_prompt_variant()
        relabel = self._coordinate_relabel()
        self._per_run_output_model = self._build_per_run_output_model(variant, relabel)
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task,
            variant,
            relabel,
            max_actions_per_step,
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

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Pure builder: return the messages to append for this step.

        Emits an upstream-browser-use-style structured user message:
        ``<agent_history>...</agent_history><agent_state>...</agent_state>
        <browser_state>...</browser_state>``. ``cache=True`` lets
        Anthropic prefix-cache the (mostly stable) task/state header.

        The mode decides what goes in: a DOM mode renders the element
        tree, a vision-capable mode attaches the image. ``vision`` renders
        no ``dom`` fragment *even if the observation carries one* — a
        fragment can reach the agent that negotiation did not activate (a
        loop re-observing, a hand-built observation in a test), and
        "vision only" has to mean it.

        That gate is on the fragment, not on the whole message. What the
        step's own history says about the actions it ran is rendered in
        every mode (see :meth:`_render_history`); keeping DOM-derived text
        out of *that* is negotiation's job, and in ``vision`` it does it by
        never activating a verb that returns any.

        The one piece of state it records is the grid over the screenshot
        it attaches, because ``parse_react_output`` has to convert a point
        in the reply with the image that reply was looking at.

        Raises:
            TypeError: the screenshot fragment reports no geometry while
                this run has a coordinate action. That is an assembly
                error — no point on it could ever be executed — so the run
                stops here rather than feeding the model a parse error per
                step that it cannot fix.
        """
        screenshot = getattr(observation, "screenshot", None)
        shows_screenshot = (
            self.observation_mode in _MODES_WITH_IMAGE and _image_bytes(screenshot) is not None
        )
        self._grid = None
        if shows_screenshot and self._has_point_actions():
            if not isinstance(screenshot, ScreenshotGeometry):
                raise TypeError(
                    f"{type(screenshot).__name__} reports no screenshot geometry, "
                    f"so no point this run writes on it can be executed. A "
                    f"screenshot fragment has to carry image_width, image_height, "
                    f"viewport_width and viewport_height (ScreenshotGeometry, "
                    f"spectragent.schemas.observation.geometry)."
                )
            self._grid = point_grid(self.coordinate_space, screenshot)

        body = self._build_step_text(observation)
        if shows_screenshot:
            return [self._build_vision_message(observation, body)]
        return [UserMessage(content=body, cache=True)]

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
                "browser_use.parse_react_output ran before prepare(), so there "
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

        return [self._action_for(item) for item in parsed.action]

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
                "browser_use.get_react_output_model() was called before "
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
        carry is its template's (``_output_models.REASONING_FIELDS``): a flash
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

    # ── private helpers ────────────────────────────────────────

    def _has_point_actions(self) -> bool:
        return any(_local_id(d.name) in _POINT_PARAMS for d in self._action_descriptors)

    def _coordinate_relabel(self) -> dict[str, dict[str, str]]:
        """Descriptor name → param → the unit *the model* writes it in.

        The capability declares its point params in the units it executes
        (viewport pixels); the model answers on the grid this agent asked
        for. Only the description differs — the names are the descriptor's
        own, and :meth:`_points_to_viewport` is the single conversion.
        """
        template = _POINT_DESCRIPTIONS[self.coordinate_space]
        relabel: dict[str, dict[str, str]] = {}
        for descriptor in self._action_descriptors:
            pairs = _POINT_PARAMS.get(_local_id(descriptor.name))
            if not pairs:
                continue
            labels: dict[str, str] = {}
            for x_key, y_key in pairs:
                labels[x_key] = template.format(axis="x", edge="left")
                labels[y_key] = template.format(axis="y", edge="top")
            relabel[descriptor.name] = labels
        return relabel

    def _build_per_run_output_model(
        self,
        variant: str,
        relabel: Mapping[str, Mapping[str, str]],
    ) -> type[BaseModel]:
        """Compose this run's output model: taught reasoning + closed action.

        The reasoning fields are the ones *this variant's* ``<output>``
        block teaches and no others. Under OpenAI's strict dialect every
        property becomes required, so a flash run carrying the full field
        set would be forced to emit a ``thinking`` block its prompt never
        asked for.
        """
        item, _models = action_union(self._action_descriptors, relabel=relabel)
        allowed = ", ".join(d.name for d in self._action_descriptors)
        return _create_model(
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

    def _action_for(self, item: BaseModel) -> Action:
        """One validated union member as an Action, on the executor's grid.

        ``exclude_none`` matters: a model that writes ``clear: null`` means
        "I have no opinion", and forwarding the ``None`` would override the
        capability's own ``clear_by_default`` with a value nobody chose.
        """
        dumped = item.model_dump(exclude_none=True)
        name, params = next(iter(dumped.items()))
        pairs = _POINT_PARAMS.get(_local_id(name))
        if not pairs:
            return Action(name=name, params=params)
        return Action(name=name, params=self._points_to_viewport(name, params, pairs))

    def _points_to_viewport(
        self,
        name: str,
        params: dict[str, Any],
        pairs: tuple[tuple[str, str], ...],
    ) -> dict[str, Any]:
        """Convert every point in *params* onto the executor's grid.

        Raises:
            ParseError: the reply names a point but this step showed no
                measurable screenshot. That goes back to the model rather
                than clicking a guessed location.
        """
        grid = self._grid
        if grid is None:
            raise ParseError(
                f"{name} takes a point on the screenshot, but this step showed "
                f"no screenshot with a known size, so the point cannot be "
                f"placed.",
                raw_output=str(params)[:500],
            )
        converted = dict(params)
        for x_key, y_key in pairs:
            x, y = converted.get(x_key), converted.get(y_key)
            if x is None or y is None:
                raise ParseError(
                    f"{name} needs both {x_key!r} and {y_key!r}; it was given "
                    f"{x_key}={x!r}, {y_key}={y!r}.",
                    raw_output=str(params)[:500],
                )
            converted[x_key], converted[y_key] = grid.to_viewport(float(x), float(y))
        return converted

    def select_prompt_variant(self) -> str:
        """Which template on disk this configuration reads.

        Public because it is the only description of this agent's prompt
        surface: it has no ``prompt_template`` field, because its choice
        is a *function* of the model and the mode, not a free one. Asking
        YAML for both ``flash_mode: true`` and
        ``prompt_template: system_prompt_flash`` would be two sources of
        truth that can disagree. ``tests/test_agents/test_prompt_tree.py``
        enumerates this over the whole config matrix to prove every file
        in the directory is reachable and none is orphaned.

        ``observation_mode`` is deliberately *not* an axis here: the modes
        share one prompt tree and differ only by the surface fragment
        spliced into it, so a DOM/vision ablation cannot be confounded by
        two separately worded templates.

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
        may vary with what the agent *is* (its mode, its verbs) but never
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

    def _render_surface(self) -> str:
        """The mode's description of how this run sees and reaches the page.

        Loaded from ``prompts/agents/browser_use/_surface/`` — a
        subdirectory, so ``agent_prompt_variants`` (which globs the top
        level) cannot mistake a fragment for a template variant. The
        coordinate paragraph is appended from code because it depends on
        ``coordinate_space``, and only when this run actually negotiated
        an action that takes a point.
        """
        stem = self.observation_mode
        if self.select_prompt_variant() in _TERSE_VARIANTS:
            stem = f"{stem}_terse"
        text, _ref = load_prompt_fragment(self.__registry_key__, "_surface", stem)
        text = text.strip()
        if self._has_point_actions():
            text = f"{text}\n{_COORDINATE_GUIDE[self.coordinate_space]}"
        return text

    def _build_system_prompt(
        self,
        task: str,  # noqa: ARG002 — reserved; some prompt templates may consume it later
        variant: str,
        relabel: Mapping[str, Mapping[str, str]],
        max_actions_per_step: int = 1,
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
            params: dict[str, Any] = {
                "max_actions": max_actions_per_step,
                "action_list": _render_action_list(self._action_descriptors, relabel),
            }
            # The fine-tuned templates describe no surface, so handing
            # them one would be a param nothing renders — which
            # ``test_prompt_tree`` reads as "the agent computed something
            # the model never sees", and rightly.
            if variant not in _FINE_TUNED_VARIANTS:
                params["surface"] = self._render_surface()
            text, snapshot = self.render(variant, params)

        if self.extend_system_message:
            # The extension is appended *after* substitution, so the
            # recorded template no longer reproduces what the model saw.
            # Record the text instead; the ref still names its source.
            text += f"\n{self.extend_system_message}"
            snapshot = text_snapshot(text, snapshot.ref)
        return text, snapshot

    def _build_vision_message(self, observation: Any, body: str) -> UserMessage:
        """Build a multi-modal user message with body + screenshot.

        Caller already checked that the screenshot has bytes, and already
        constructed the structured XML body.
        """
        import base64

        from spectragent.core.llm.messages import (
            ContentPartImageParam,
            ContentPartTextParam,
            ImageURL,
        )

        image_bytes = _image_bytes(observation.screenshot)
        assert image_bytes is not None  # noqa: S101 — caller's precondition
        media_type = image_media_type(image_bytes) or "image/png"
        b64 = base64.b64encode(image_bytes).decode()
        content: list[Any] = [
            ContentPartTextParam(text=body),
            ContentPartImageParam(
                image_url=ImageURL(
                    url=f"data:{media_type};base64,{b64}",
                    detail=self.vision_detail,
                    media_type=media_type,
                ),
            ),
        ]
        return UserMessage(content=content, cache=True)

    def _build_step_text(self, observation: Any) -> str:
        """Compose the per-step XML user-message body.

        Layout (upstream browser-use prompts.py:400-416)::

            <agent_history>...</agent_history>
            <agent_state>
              <user_request>...</user_request>
              <follow_up_requests>...</follow_up_requests>  (only if any)
              <step_info>...</step_info>                    (only if known)
            </agent_state>
            <browser_state>URL/Title/Tabs/DOM</browser_state>

        Scratchpad blocks (file_system, todo, plan, sensitive_data,
        page_stats, recent_events, ...) are intentionally omitted —
        their data sources land with the future ScratchpadCapability.
        """
        history_xml = self._render_history(getattr(observation, "history", None))
        state_xml = self._render_agent_state(observation)
        browser_xml = self._render_browser_state(observation)
        grid = self._grid
        # Only the pixel grid needs a size: a 0-1000 grid is defined over
        # whatever size the image happens to have, so stating the pixels
        # would invite the model to answer in them.
        if grid is not None and grid.space == "screenshot_pixels":
            browser_xml += (
                f"\n\nScreenshot: {grid.width}x{grid.height} pixels, showing the "
                f"whole visible viewport."
            )
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
        """What this run already did, in every mode.

        Deliberately not gated on the mode. These are the run's own past
        actions and their results — the model needs them to avoid
        repeating a step, and in ``vision`` they are the actions that mode
        negotiated, whose results carry no element tree. The invariant
        "``vision`` sees no DOM" is held where it is decidable: the ``dom``
        fragment is never rendered (:meth:`_render_browser_state`), and
        ``extract_content`` — DOM-derived text under another name — is
        excluded from the mode's action demand, so no result can contain
        any. Filtering the text here instead would be guessing at a
        provenance the string does not carry.
        """
        items = list(getattr(history, "items", []) or [])
        if not items:
            return "<step_1>\n(no prior steps)\n</step_1>"
        if len(items) > self._max_history_steps:
            items = items[-self._max_history_steps :]
        blocks: list[str] = []
        for rec in items:
            actions_str = ", ".join(f"{a.name}({a.params})" for a in rec.actions) or "(none)"
            results_str = (
                "; ".join(
                    f"ok={r.success} {r.error or r.extracted_content or ''}".rstrip()
                    for r in rec.action_results
                )
                or "(none)"
            )
            tag = f"step_{rec.step + 1}"
            blocks.append(
                f"<{tag}>\n"
                f"<llm_output>{_truncate(rec.llm_output, 2000)}</llm_output>\n"
                f"<actions>{actions_str}</actions>\n"
                f"<results>{results_str}</results>\n"
                f"</{tag}>"
            )
        return "\n".join(blocks)

    def _render_browser_state(self, observation: Any) -> str:
        """URL / Title / Tabs / DOM block.

        The DOM half is gated on the *mode*, not on whether the
        observation happens to carry a ``dom`` fragment. In ``vision`` the
        tree never reaches the model even if something put one on the
        observation — which is what makes a vision ablation a vision
        ablation.
        """
        parts: list[str] = []
        meta = getattr(observation, "browser_meta", None)
        if meta is not None:
            url = getattr(meta, "url", "") or ""
            title = getattr(meta, "page_title", "") or ""
            if url:
                parts.append(f"URL: {url}")
            if title:
                parts.append(f"Title: {title}")
            tabs = getattr(meta, "tabs", None) or []
            if tabs:
                tab_info = ", ".join(
                    f"[{t.get('tab_id', '?')}] {t.get('title', '')} ({t.get('url', '')})"
                    for t in tabs
                )
                parts.append(f"Tabs: {tab_info}")

        if self.observation_mode in _MODES_WITH_DOM:
            dom = getattr(observation, "dom", None)
            element_tree = getattr(dom, "element_tree", None) if dom is not None else None
            if element_tree is not None:
                serialized = getattr(element_tree, "serialized_dom", None)
                dom_str = serialized if isinstance(serialized, str) else str(element_tree)
                parts.append(f"DOM:\n{dom_str}")

        return "\n\n".join(parts) if parts else "(empty browser state)"


def _image_bytes(screenshot: Any) -> bytes | None:
    """The encoded bytes of a screenshot fragment, reading a path if needed.

    The encoding is whatever the capability captured — PNG by default,
    JPEG when it was configured for one. The caller labels the data URL
    from the bytes (:func:`image_media_type`) rather than assuming: a
    JPEG under a ``data:image/png`` prefix is sniffed past by OpenAI and
    rejected outright by Anthropic.
    """
    if screenshot is None:
        return None
    image_bytes = getattr(screenshot, "image_bytes", None)
    if image_bytes is not None:
        return image_bytes
    image_path = getattr(screenshot, "image_path", None)
    if not image_path:
        return None
    with open(image_path, "rb") as f:
        return f.read()


def _render_action_list(
    descriptors: list[ActionDescriptor],
    relabel: Mapping[str, Mapping[str, str]],
) -> str:
    """Render action descriptors as a markdown bullet list for the prompt.

    Each line: ``- `<descriptor_name>(<params>)` — <description>``.

    The descriptor's own name is used verbatim, so the prompt advertises
    exactly the vocabulary the closed schema permits, ``env.act`` routes
    on, and the trace records. There is no alias table and no local-id
    fallback: a name the model writes is a name the run holds.

    Param text is verbatim too, except where *relabel* names the grid the
    model answers on rather than the units the executor declares. The
    *names* never change.

    Empty list yields an empty string — only in degenerate fixtures;
    ``prepare()`` refuses to build a schema with no verbs in it.
    """
    if not descriptors:
        return ""
    lines: list[str] = []
    for d in descriptors:
        if d.params:
            labels = relabel.get(d.name, {})
            params_part = ", ".join(f"{k}: {labels.get(k, v)}" for k, v in d.params.items())
        else:
            params_part = "no params"
        lines.append(f"- `{d.name}({params_part})` — {d.description}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated)"
