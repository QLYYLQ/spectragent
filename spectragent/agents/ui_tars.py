"""UITarsAgent — vision-only computer-use agent speaking the UI-TARS text protocol.

UI-TARS is a passive domain-knowledge provider: it knows the verb
vocabulary the UI-TARS family of models emits, and how to translate that
vocabulary into this framework's actions. It knows nothing about which
capability will execute them — its ``__wiring__`` names domain verbs
(``click_coordinate``, ``type_coordinate``), never a provider, so the
same agent negotiates against a browser, a desktop VM or an Android
emulator unchanged.

**One vocabulary, one source.** :data:`_VERB_TABLE` is the single place
that says what verbs exist. The parser reads it to translate model output
into Actions, and the system prompt is *generated* from it, intersected
with the actions this run actually negotiated. So the prompt cannot
advertise a verb ``guarded_act`` would reject, and cannot teach a
parameter the action does not read.

**Three contracts the prompt states and the parser holds the model to.**

1. *How many actions a reply may carry.* The Loop owns
   ``max_actions_per_step`` and is what truncates; ``AgentRuntime`` hands
   it to :meth:`UITarsAgent.prepare`, which renders the budget
   (:func:`_action_contract`) — one ``Action:`` line per action, run in
   order, at most N. The agent keeps no number of its own.
2. *Which grid a point is on.* ``coordinate_space`` says whether the model
   answers in pixels of the screenshot or on a 0-1000 grid over it. The
   capability's point actions take their own units over the area the
   screenshot depicts, which differ from the screenshot's pixels whenever
   the capture is scaled (a browser at device scale factor 2). The mapping
   is the framework's, not this agent's:
   :mod:`spectragent.schemas.observation.geometry` defines what a
   screenshot fragment reports and how a point converts, and this agent
   applies it once per point, with the screenshot the same step showed. A
   point with no measurable screenshot behind it is a :class:`ParseError`,
   never a DPR-1 guess. What counts as a point is equally narrow: the
   documented spellings and nothing else (:func:`_point_coordinates`), so a
   model that names its target rather than locating it (``point='btn2'``)
   is refused, never clicked at the digits scraped out of the name.
3. *What a reply looks like.* An action is a line that starts with
   ``Action:``; :func:`_action_calls` reads those and nothing else, and
   every way a reply can fail that reading — empty, no action line, a
   call cut off, text after a call, an unknown verb, an argument the verb
   does not take — is a :class:`ParseError` whose detail says which and
   how to fix it, because the Loop feeds that detail back to the model
   verbatim.
"""

from __future__ import annotations

import ast
import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel
from pydantic import Field as PydanticField

from spectragent.agents._registrar import AgentBase
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.errors import ParseError
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.observation.geometry import (
    CoordinateSpace,
    PointGrid,
    ScreenshotGeometry,
    point_grid,
)
from spectragent.schemas.observation.image import image_media_type
from spectragent.schemas.prompt import PromptSnapshot

# ── The verb vocabulary ──────────────────────────────────────


class VerbError(ValueError):
    """A verb was emitted with arguments its action cannot use."""


@dataclass(frozen=True)
class _Verb:
    """One UI-TARS verb, and everything that is true about it.

    Attributes:
        action: The capability-relative action id the verb means. It
            names no provider — ``build_guarded_act`` binds it to the one
            this run holds.
        signature: The call syntax taught to the model, e.g.
            ``click(point='(x, y)')``. It is *complete*: every argument
            the verb reads appears in it, so a model that follows it can
            reach every parameter :attr:`fills` names.
        fills: The action params :attr:`to_params` can produce. The ones
            not in :attr:`points` are rendered into the prompt with the
            descriptor's own type text, so the model sees what its
            arguments turn into.
            ``tests/test_agents/test_ui_tars_prompt_action_vocabulary.py``
            holds this to the descriptor.
        to_params: Translates the verb's kwargs into the action's params.
            Raises :class:`VerbError` when a required argument is missing
            or malformed — a loud parse failure beats an action that
            reports ``Missing param`` one round trip later.
        accepts: Every argument name the verb reads: the ones
            :attr:`signature` teaches, plus the UI-TARS spellings of the
            same thing (``start_box`` for ``point``). Anything else is
            refused rather than dropped — ``click(button='right')`` run as
            a left click is an action the model never asked for.
        points: The ``(x, y)`` param pairs that :attr:`to_params` leaves on
            the model's grid. :meth:`UITarsAgent._action_for` converts each
            pair into viewport units before the action exists, so nothing
            downstream ever sees the model's grid.
    """

    action: str
    signature: str
    fills: tuple[str, ...]
    to_params: Callable[[Mapping[str, Any]], dict[str, Any]]
    accepts: tuple[str, ...]
    points: tuple[tuple[str, str], ...] = ()


#: Argument names a UI-TARS-family model uses for the same point. The
#: taught spelling comes first; ``start_box`` / ``end_box`` are UI-TARS 1.0
#: and 1.5's, which a model fine-tuned on that format writes whatever the
#: prompt says.
_POINT_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "point": ("point", "start_box"),
    "start_point": ("start_point", "start_box"),
    "end_point": ("end_point", "end_box"),
}

#: The tag pairs a UI-TARS-family model wraps a point or a box in. Stripped
#: only as a *matched* pair spanning the whole value, so a stray opener
#: (``'<point>10 20'``) or trailing prose (``'<point>10 20</point> button 3'``)
#: is not silently read as a coordinate.
_POINT_TAGS: tuple[tuple[str, str], ...] = (
    ("<|box_start|>", "<|box_end|>"),
    ("<|point_start|>", "<|point_end|>"),
    ("<point>", "</point>"),
    ("<bbox>", "</bbox>"),
    ("<box>", "</box>"),
)

#: The only things a point spelling may be made of once its tags are off: a
#: coordinate, a bracket, or a separator. ``_point_coordinates`` requires
#: these to tile the value with no gap, which is what stops a number glued to
#: a letter from being read as one: ``x1y2``, ``btn2``, ``#id2 > .cls3`` and
#: ``the 2nd button, row 3`` all leave a gap at their first letter. A
#: coordinate is therefore never matched by a fragment of a longer word —
#: the whole-value tiling is the word boundary.
_POINT_PIECE = re.compile(r"(?P<number>-?\d+(?:\.\d+)?)|[()\[\]{}]|[,\s]+")

#: One coordinate, whole. Used on the elements of a tuple/list literal, where
#: the pieces are already separated: ``point=['10', '20']``.
_COORDINATE = re.compile(r"-?\d+(?:\.\d+)?")


def _argument(kwargs: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    """The value of whichever of *names* the call wrote, or ``None``.

    Two spellings of one argument in one call are refused: keeping either
    would silently discard the other, and they need not agree.
    """
    given = [name for name in names if name in kwargs]
    if len(given) > 1:
        raise VerbError(f"{' and '.join(given)} say the same thing; write only {names[0]}")
    return kwargs[given[0]] if given else None


def _strip_point_tags(text: str) -> str:
    """*text* with every matched :data:`_POINT_TAGS` pair around it removed."""
    stripped = True
    while stripped:
        stripped = False
        for opener, closer in _POINT_TAGS:
            if (
                len(text) >= len(opener) + len(closer)
                and text.startswith(opener)
                and text.endswith(closer)
            ):
                text = text[len(opener) : len(text) - len(closer)].strip()
                stripped = True
    return text


def _point_coordinates(value: Any) -> list[float] | None:
    """Every coordinate *value* spells, or ``None`` when it spells none.

    A point spelling is read, never scraped. The documented forms are the
    whole of what is accepted, and each one has to account for *every*
    character of the value: tags (:data:`_POINT_TAGS`) only as a matched
    pair around the rest, then nothing but numbers, brackets and separators
    tiling what is left. So ``'(10, 20)'``, ``'10,20'``, ``'[10, 20]'``,
    ``'<point>10 20</point>'``, ``'<|box_start|>(10,20)<|box_end|>'`` and the
    two-pair box ``'(10,20),(30,40)'`` all read, while anything carrying text
    — a selector (``'#id2 > .cls3'``), an element name (``'x1y2'``,
    ``'btn2'``), a description (``'the 2nd button, row 3'``) — reads as no
    coordinate at all rather than as the digits buried in it.

    A tuple or list literal is taken apart by element instead, each of which
    must itself be a number or a quoted one: ``point=(10, 20)``,
    ``point=['10', '20']``.

    The numbers come back exactly as written and in the order written: how
    many there are (two for a point, four for a box) and which grid they are
    on (``coordinate_space``) are the caller's business, not the spelling's.
    """
    if isinstance(value, str):
        text = _strip_point_tags(value.strip())
        if not text:
            return None
        numbers: list[float] = []
        at = 0
        for piece in _POINT_PIECE.finditer(text):
            if piece.start() != at:
                return None  # a gap: the value carries something that is not a coordinate
            at = piece.end()
            number = piece.group("number")
            if number is not None:
                numbers.append(float(number))
        return numbers if at == len(text) else None
    if isinstance(value, (tuple, list)):
        numbers = []
        for item in value:
            if isinstance(item, bool):
                return None
            if isinstance(item, (int, float)) or (
                isinstance(item, str) and _COORDINATE.fullmatch(item.strip())
            ):
                numbers.append(float(item))
            else:
                return None
        return numbers
    return None


def _parse_point(key: str, value: Any) -> tuple[float, float]:
    """``(x, y)`` from every spelling a UI-TARS-family model uses for a point.

    Two numbers are the point. Four are a box (``'(x1,y1,x2,y2)'``,
    ``'<bbox>x1 y1 x2 y2</bbox>'``, ``'(x1,y1),(x2,y2)'``), which UI-TARS 1.0
    emits for a click and which means its centre.

    Raises:
        VerbError: *value* spells no coordinates, or spells a count that is
            neither a point nor a box. Either way the detail says what was
            written and what to write instead, because the Loop hands it
            back to the model verbatim — an ambiguous point is refused, never
            guessed at.
    """
    numbers = _point_coordinates(value)
    if numbers is None:
        raise VerbError(
            f"{key}={value!r} is not a coordinate; expected {key}='(x, y)' — two "
            f"numbers and nothing else, e.g. {key}='(640, 360)'. Describe nothing "
            f"there: no element name, selector or text, only the numbers"
        )
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 4:
        return (numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2
    raise VerbError(
        f"{key}={value!r} has {len(numbers)} numbers; write two for a point "
        f"({key}='(x, y)') or four for a box ({key}='(x1, y1, x2, y2)')"
    )


def _point(kwargs: Mapping[str, Any], key: str = "point") -> tuple[float, float]:
    names = _POINT_ARGUMENTS[key]
    raw = _argument(kwargs, names)
    if raw is None:
        raise VerbError(f"a {key} is missing; write {key}='(x, y)'")
    return _parse_point(key, raw)


def _click_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    x, y = _point(kwargs)
    return {"coordinate_x": x, "coordinate_y": y}


def _type_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    x, y = _point(kwargs)
    # UI-TARS says ``content``; the action reads ``text``. Required: with
    # ``clear_by_default`` a missing text would clear the field and type
    # nothing, reported as a success.
    if "content" not in kwargs:
        raise VerbError("type needs content='text to type'")
    return {"coordinate_x": x, "coordinate_y": y, "text": str(kwargs["content"])}


def _drag_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    sx, sy = _point(kwargs, "start_point")
    ex, ey = _point(kwargs, "end_point")
    return {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}


_DIRECTIONS = ("up", "down", "left", "right")


def _scroll_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # A direction and nothing else. Not a point: ScrollAction scrolls
    # under the mouse pointer or a selector_map element, and a vision-only
    # run has no selector map, so a point would be accepted and not
    # honoured. Not an amount: how far one scroll goes is
    # ``ScrollAction.default_amount``, capability-side config — an agent
    # declares that it can scroll, not how many pixels.
    direction = str(kwargs.get("direction") or "").strip().lower()
    if direction not in _DIRECTIONS:
        raise VerbError("scroll needs direction='up'|'down'|'left'|'right'")
    return {"direction": direction}


def _hotkey_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    raw = _argument(kwargs, ("key", "keys"))
    # The executor splits a chord on ``+``. UI-TARS-1.5 spells a chord
    # with spaces (``'ctrl c'``) and some models as a list of key names;
    # either would otherwise be looked up as one key named ``ctrl c``.
    if isinstance(raw, (list, tuple)):
        keys = "+".join(part for name in raw for part in str(name).split())
    else:
        keys = str(raw or "").strip()
        if "+" not in keys and len(keys.split()) > 1:
            keys = "+".join(keys.split())
    if not keys:
        raise VerbError("hotkey needs key='ctrl+a'")
    return {"keys": keys}


def _wait_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # How long a wait waits is ``WaitAction.default_seconds`` — the same
    # capability-side-config rule as ``scroll``'s amount.
    return {}


def _finished_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # DoneAction reads ``extracted_content``, not ``content``.
    return {"success": True, "extracted_content": str(kwargs.get("content", ""))}


#: Every verb UI-TARS may emit. A verb absent from this table is a parse
#: error, not a silently manufactured action name. The mobile-only verbs
#: (``long_press``, ``open_app``, ``press_home``, ``press_back``) have no
#: browser action and no entry here; when an Android capability lands they
#: arrive as its actions.
_VERB_TABLE: dict[str, _Verb] = {
    "click": _Verb(
        action="click_coordinate",
        signature="click(point='(x, y)')",
        fills=("coordinate_x", "coordinate_y"),
        to_params=_click_params,
        accepts=_POINT_ARGUMENTS["point"],
        points=(("coordinate_x", "coordinate_y"),),
    ),
    "type": _Verb(
        action="type_coordinate",
        signature="type(content='text to type', point='(x, y)')",
        fills=("coordinate_x", "coordinate_y", "text"),
        to_params=_type_params,
        accepts=("content", *_POINT_ARGUMENTS["point"]),
        points=(("coordinate_x", "coordinate_y"),),
    ),
    "scroll": _Verb(
        action="scroll",
        signature="scroll(direction='up|down|left|right')",
        fills=("direction",),
        to_params=_scroll_params,
        accepts=("direction",),
    ),
    "hotkey": _Verb(
        action="send_keys",
        signature="hotkey(key='ctrl+a')",
        fills=("keys",),
        to_params=_hotkey_params,
        accepts=("key", "keys"),
    ),
    "drag": _Verb(
        action="drag",
        signature="drag(start_point='(x1, y1)', end_point='(x2, y2)')",
        fills=("start_x", "start_y", "end_x", "end_y"),
        to_params=_drag_params,
        accepts=(*_POINT_ARGUMENTS["start_point"], *_POINT_ARGUMENTS["end_point"]),
        points=(("start_x", "start_y"), ("end_x", "end_y")),
    ),
    "wait": _Verb(
        action="wait",
        signature="wait()",
        fills=(),
        to_params=_wait_params,
        accepts=(),
    ),
    "finished": _Verb(
        action="done",
        signature="finished(content='answer if applicable')",
        fills=("extracted_content", "success"),
        to_params=_finished_params,
        accepts=("content",),
    ),
}

#: Action id → the verb that means it. Used to render the prompt from the
#: *negotiated* descriptors rather than from a hand-written list.
_ACTION_TO_VERB: dict[str, str] = {v.action: name for name, v in _VERB_TABLE.items()}


def _local_id(name: str) -> str:
    return name.rsplit(".", 1)[-1]


# ── The coordinate contract ───────────────────────────────────

#: What the system prompt says a point is, per coordinate space. Keyed by
#: every member of :data:`CoordinateSpace`, which is also what the
#: ``coordinate_space`` field's Literal lists
#: (``tests/test_agents/test_ui_tars_coordinates.py`` holds the three equal).
_COORDINATE_PROMPTS: dict[CoordinateSpace, str] = {
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

#: Said on every grid, because it is about the spelling rather than the
#: space. The parser reads a point as numbers and nothing else
#: (:func:`_point_coordinates`), so a model that names its target instead of
#: locating it — ``point='btn2'``, ``point='the 2nd tile'`` — spends a step
#: on a refusal. Saying so costs one sentence.
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
    written against the screenshot already shown; the Loop drops
    everything past ``max_actions_per_step`` before it runs; nothing after
    a finishing action runs.
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
        f"order written, all aimed at the screenshot you are looking at now; you "
        f"see the screen again only after the last one has run. Anything past the "
        f"first {n} is dropped before it runs, so do not plan past it, and do not "
        f"chain an action whose target only appears after an earlier one has run."
    )
    if can_finish:
        budget += " Nothing written after finished() runs."
    return (
        "Action: <an action call from Available Actions, written exactly as shown>\n"
        "Action: <the next action call, only if another one is needed>",
        budget,
    )


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
    # Domain verbs only. No ``capability`` entry: naming one is the
    # welding F10 removed, and nothing at runtime read it — it only
    # narrowed the generated config to whichever capability happened to
    # be registered.
    #
    # Pure vision: a screenshot and the task, nothing DOM-shaped. The
    # required actions are what the model cannot work without; the
    # optional four are the rest of its native vocabulary, dropped from
    # both the prompt and the parser's reach when a capability does not
    # offer them.
    __wiring__: ClassVar[dict[str, Any]] = {
        "agent_loop": ("agent_loop", ["react", "branch"]),
        "observation": (
            "observation",
            ["screenshot", "task"],
            ["step_info"],
        ),
        "action": (
            "action",
            ["click_coordinate", "type_coordinate", "done"],
            ["scroll", "send_keys", "drag", "wait"],
        ),
        "llm": "llm",
    }

    # ── config (conscribe visible) ─────────────────────────────
    # ``max_steps`` / ``max_actions_per_step`` are deliberately absent:
    # the Loop owns and enforces them, and AgentRuntime hands the limit to
    # ``prepare()`` so the prompt advertises exactly what will run.
    #
    # Both Literals are written inline rather than through a module alias
    # or computed: the generated stub cannot import a module-level alias
    # (it would type the field ``Any``), and a ``Literal[tuple(...)]``
    # renders through conscribe but costs a pyright error per field.
    # ``prompt_template``'s members are the on-disk stems, so nothing has
    # to case-fold a config value into a filename on the way to the loader
    # (``tests/test_agents/test_prompt_tree.py`` asserts the two agree);
    # ``coordinate_space``'s are :data:`CoordinateSpace`'s
    # (``tests/test_agents/test_ui_tars_coordinates.py`` asserts that).
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
                "prompt teaches it; the agent converts every point into the "
                "units of the capability's point actions."
            ),
        ),
    ] = "screenshot_pixels"

    # ── internal state ─────────────────────────────────────────
    # ``task`` reaches every step via ``obs.task.user_request`` (loop
    # fragment); the agent does not hold its own copy.
    _action_descriptors: list[ActionDescriptor] = field(
        default_factory=list, init=False, repr=False
    )
    #: The verbs this run can execute: the table narrowed to the
    #: negotiated actions by ``prepare()``. The whole table until then,
    #: which only a hand-driven agent ever parses with.
    _verbs: dict[str, _Verb] = field(
        default_factory=lambda: dict(_VERB_TABLE), init=False, repr=False
    )
    #: The grid over the screenshot attached to the *current* step's
    #: prompt, set by ``build_react_prompt`` and read by
    #: ``parse_react_output`` — the Loop calls the two in that order within
    #: one step, so a point in a reply is always converted with the image
    #: that reply looked at. ``None`` when this step showed no screenshot
    #: of measurable size.
    _grid: PointGrid | None = field(default=None, init=False, repr=False)
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)

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
        return list(self._action_descriptors)

    # ── Lifecycle hook called by AgentRuntime ──────────────────

    def prepare(
        self,
        task: str,
        action_descriptors: list[ActionDescriptor],
        max_actions_per_step: int = 1,
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        ``task`` is baked into the system prompt, and also flows through
        every step's observation as ``obs.task.user_request``.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates; the prompt has to promise exactly that.
        """
        self._action_descriptors = list(action_descriptors)
        negotiated = {_local_id(d.name) for d in self._action_descriptors}
        self._verbs = {
            name: verb for name, verb in _VERB_TABLE.items() if verb.action in negotiated
        }
        self._grid = None
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, self._action_descriptors, max_actions_per_step
        )
        # cache=True: static-prefix breakpoint — see BrowserUseAgent for why it
        # is kept alongside the per-step one.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    def build_react_prompt(self, observation: Any) -> list[BaseMessage]:
        """Pure builder: return the screenshot user-message for this step.

        UI-TARS is purely vision-driven, so the screenshot is always
        attached. With no screenshot bytes the step degrades to a text
        placeholder so the LLM call still succeeds — and since that step
        has no grid, any point in its reply is refused.

        The one piece of state it records is that grid, because
        ``parse_react_output`` has to place a point with the image the
        reply was looking at.

        Raises:
            TypeError: the screenshot fragment is not a
                :class:`ScreenshotGeometry`. That is an assembly error —
                no point on it could ever be executed — so the run stops
                here instead of feeding the model a parse error per step
                that it has no way to fix.
        """
        task_text = observation.task.user_request
        screenshot = getattr(observation, "screenshot", None)
        if screenshot is not None and not isinstance(screenshot, ScreenshotGeometry):
            raise TypeError(
                f"{type(screenshot).__name__} reports no screenshot geometry, so no "
                f"point UI-TARS writes on it can be executed. A screenshot fragment "
                f"has to carry image_width, image_height, viewport_width and "
                f"viewport_height (ScreenshotGeometry, "
                f"spectragent.schemas.observation.geometry)."
            )
        image_bytes: bytes | None = None
        if screenshot is not None:
            # The image itself is not part of the geometry contract.
            image_bytes = getattr(screenshot, "image_bytes", None)
            image_path = getattr(screenshot, "image_path", None)
            if image_bytes is None and image_path:
                with open(image_path, "rb") as f:
                    image_bytes = f.read()

        if screenshot is None or image_bytes is None:
            self._grid = None
            return [UserMessage(content=f"Task: {task_text}\n(no screenshot available)")]

        self._grid = point_grid(self.coordinate_space, screenshot)
        text = f"Task: {task_text}"
        if self._grid is not None and self._grid.space == "screenshot_pixels":
            text += f"\nScreenshot: {self._grid.width}x{self._grid.height} pixels."
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
        return [UserMessage(content=content)]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Read every ``Action:`` line of a reply into an executable Action.

        All or nothing: one unreadable line fails the whole reply. Running
        the readable half of a multi-action plan — the tiles but not the
        line that was meant to come between them — does something the
        model never asked for.
        """
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        calls = _action_calls(text, example=self._example_call())
        try:
            return [self._action_for(call) for call in calls]
        except VerbError as exc:
            raise ParseError(str(exc), raw_output=text[:500]) from exc

    def get_react_output_model(self) -> type[BaseModel] | None:
        # UI-TARS speaks a native text protocol, not structured output.
        return None

    # ── private helpers ────────────────────────────────────────

    def _example_call(self) -> str:
        """A call the model could legally write this run, for error hints."""
        verb = next(iter(self._verbs.values()), None)
        return verb.signature if verb is not None else ""

    def _action_for(self, call: _Call) -> Action:
        """Translate one call through the negotiated verbs and the step's grid."""
        verb = self._verbs.get(call.verb)
        if verb is None:
            offered = ", ".join(self._verbs) or "none"
            raise VerbError(f"{call.label}: unknown verb {call.verb!r}; this run offers: {offered}")
        where = f"{call.label}, {_clip(call.source)}"
        if call.positional:
            raise VerbError(f"{where}: pass arguments by name, as in {verb.signature}")
        unexpected = [name for name in call.kwargs if name not in verb.accepts]
        if unexpected:
            raise VerbError(
                f"{where}: {call.verb}() does not take {', '.join(unexpected)}; "
                f"write it as {verb.signature}"
            )
        try:
            params = verb.to_params(call.kwargs)
            if verb.points:
                grid = self._grid
                if grid is None:
                    raise VerbError(
                        "a point cannot be placed, because this step's screenshot has no known size"
                    )
                for x_key, y_key in verb.points:
                    params[x_key], params[y_key] = grid.to_viewport(params[x_key], params[y_key])
        except VerbError as exc:
            raise VerbError(f"{where}: {exc}") from exc
        return Action(name=verb.action, params=params)

    def _build_system_prompt(
        self,
        task: str,
        action_space: list[ActionDescriptor],
        max_actions_per_step: int,
    ) -> tuple[str, PromptSnapshot]:
        action_format, action_budget = _action_contract(
            max_actions_per_step, can_finish="finished" in self._verbs
        )
        return self.render(
            self.prompt_template,
            {
                "task": task,
                "action_space": _render_action_space(action_space),
                "action_format": action_format,
                "action_budget": action_budget,
                "coordinate_space": (
                    f"{_COORDINATE_PROMPTS[self.coordinate_space]} {_POINT_SYNTAX}"
                ),
            },
        )


def _render_action_space(descriptors: list[ActionDescriptor]) -> str:
    """Render the negotiated actions as UI-TARS call syntax.

    One block per negotiated action::

        - type(content='text to type', point='(x, y)')
          Click a point in the page viewport to focus a text field, then type.
          executes: text: str

    The verb and its syntax come from :data:`_VERB_TABLE`, which is what
    the parser reads; the description and the parameter types come from
    the descriptor, which is what the action actually executes. Both
    halves are derived, so the prompt has no second vocabulary to drift
    from.

    The ``executes`` line lists the params the verb produces *as written*,
    named as a consequence rather than as syntax: the descriptor's names
    belong to the executor (``text``), the model writes the verb's
    (``content``). Point params are left out of it: the agent converts
    them, so the unit the executor declares is not the one the model
    writes, and the prompt's Coordinates section is what says what a
    point is.

    A negotiated action with no verb is skipped rather than advertised:
    the model has no syntax for it, so teaching its name would invite an
    unparseable call. The wiring and the table are kept in agreement by
    ``tests/test_agents/test_ui_tars_react_compatible.py``.
    """
    blocks: list[str] = []
    for d in descriptors:
        verb_name = _ACTION_TO_VERB.get(_local_id(d.name))
        if verb_name is None:
            continue
        verb = _VERB_TABLE[verb_name]
        lines = [f"- {verb.signature}", f"  {d.description}"]
        converted = {key for pair in verb.points for key in pair}
        filled = [(k, d.params[k]) for k in verb.fills if k in d.params and k not in converted]
        if filled:
            rendered = ", ".join(f"{k}: {v}" for k, v in filled)
            lines.append(f"  executes: {rendered}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


# ── Reading a reply ───────────────────────────────────────────


@dataclass(frozen=True)
class _Call:
    """One call read off an ``Action:`` line, before any verb is looked up."""

    verb: str
    kwargs: dict[str, Any]
    #: Positional arguments written; every verb takes keywords only.
    positional: int
    #: The call as the model wrote it, for error messages.
    source: str
    #: Which action line it came from: ``Action line 2``.
    label: str


#: End-of-sequence markers some model servers leave in the text.
_EOS_MARKERS: tuple[str, ...] = ("[EOS]", "<|im_end|>", "<|endoftext|>")

#: A markdown fence line, with or without a language tag.
_FENCE_LINE = re.compile(r"^[ \t]*```[\w+-]*[ \t]*$", re.MULTILINE)

#: The start of an action line: ``Action:``, optionally dressed as markdown
#: (``**Action:**``, ``- Action:``, ``> Action:``) or numbered as a list item
#: (``1. Action:``, ``2) Action:``). Anchored to a line start on purpose:
#: ``Action:`` inside a sentence is prose *quoting* an action — a real reply
#: wrote ``"Action: click(point='(647, 281)')" was executed`` in its Thought
#: and then refused the task.
#:
#: The price of the anchor, accepted: a reasoning line that itself begins
#: ``Action:`` (``Action: I clicked it earlier.``) is an action line that is
#: not a call, and fails the reply by the all-or-nothing rule.
_ACTION_LABEL = re.compile(
    r"^[ \t>*_-]*(?:\d{1,3}[.)][ \t*_]*)?action[ \t*_]*:[ \t*_`]*",
    re.IGNORECASE | re.MULTILINE,
)
_ACTION_WORD = re.compile(r"\baction\s*:", re.IGNORECASE)

#: A call's head: a (possibly dotted) name and its opening parenthesis.
_CALL_HEAD = re.compile(r"[A-Za-z_][\w.]*[ \t]*\(")

#: Skipped around a call: markdown code/emphasis marks, separators between
#: two calls on one line, and a sentence's closing punctuation.
_BETWEEN_CALLS = " \t`*;,."


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _action_calls(text: str, *, example: str) -> list[_Call]:
    """Every call on every ``Action:`` line of *text*, in order.

    Tolerated because models produce them and none is ambiguous: code
    fences, prose around the Thought/Action block, several Thought/Action
    blocks, markdown or list-number dressing on the label, the call on the
    line after a bare ``Action:``, a call spanning lines inside its
    parentheses, unescaped apostrophes in a quoted argument, a trailing
    ``.``/``;`` or ``# comment``, and several calls on one line. Everything
    else raises :class:`ParseError` naming the line and the problem.
    """
    raw = text
    for marker in _EOS_MARKERS:
        text = text.replace(marker, "")
    body = _FENCE_LINE.sub("", text)
    hint = f", e.g. Action: {example}" if example else ""

    if not body.strip():
        raise ParseError(
            f"The reply was empty. It must contain at least one line that "
            f"starts with 'Action:'{hint}.",
            raw_output=raw[:500],
        )

    calls: list[_Call] = []
    resume = 0
    line_no = 0
    for label in _ACTION_LABEL.finditer(body):
        if label.start() < resume:
            continue  # inside a multi-line call of the previous line
        line_no += 1
        found, resume = _calls_on_line(body, label.end(), f"Action line {line_no}", hint, raw)
        calls.extend(found)

    if not calls:
        misplaced = (
            " 'Action:' does appear, but not at the start of a line, so it was read as prose."
            if _ACTION_WORD.search(body)
            else ""
        )
        raise ParseError(
            f"No line starts with 'Action:'.{misplaced} Put each action on its own line{hint}.",
            raw_output=raw[:500],
        )
    return calls


def _calls_on_line(
    body: str, start: int, label: str, hint: str, raw: str
) -> tuple[list[_Call], int]:
    """The calls of one action line, and where scanning may resume."""

    def fail(detail: str) -> ParseError:
        return ParseError(detail, raw_output=raw[:500])

    i = _skip(body, start, " \t*`")
    if i >= len(body) or body[i] in "\r\n":
        # A bare ``Action:`` — the call may sit on the next non-blank line.
        j = _skip(body, i, " \t\r\n*`")
        if _CALL_HEAD.match(body, j) is None:
            raise fail(f"{label} has no call after 'Action:'{hint}.")
        i = j

    calls: list[_Call] = []
    while True:
        head = _CALL_HEAD.match(body, i)
        if head is None:
            rest = _clip(body[i : _line_end(body, i)], 80)
            if calls:
                raise fail(
                    f"{label} has text after the call: {rest!r}. Put nothing but "
                    f"the call on an Action line."
                )
            raise fail(f"{label} is not a call: {rest!r}{hint}.")
        close = _closing_paren(body, head.end() - 1)
        if close is None:
            raise fail(
                f"{label} is cut off before its closing parenthesis: {_clip(body[i:], 80)!r}."
            )
        end, strings = close
        calls.append(_read_call(body, i, end, strings, label, raw))
        i = _skip(body, end + 1, _BETWEEN_CALLS)
        if i >= len(body) or body[i] in "\r\n":
            return calls, i
        if body[i] == "#":
            return calls, _line_end(body, i)


def _skip(body: str, i: int, chars: str) -> int:
    while i < len(body) and body[i] in chars:
        i += 1
    return i


def _line_end(body: str, i: int) -> int:
    end = body.find("\n", i)
    return len(body) if end < 0 else end


def _closing_paren(body: str, open_at: int) -> tuple[int, list[tuple[int, int]]] | None:
    """Index of the ``)`` matching ``body[open_at]``, and the string spans inside.

    Quotes are tracked so a parenthesis inside ``point='(1, 2)'`` does not
    count, and so are brackets outside strings, which decide what may
    close a string (see :func:`_string_end`). A newline outside a string is
    allowed (a call written across lines), but a following ``Action:`` line
    ends the search: that call was cut off.
    """
    depth = 0
    brackets = 0
    strings: list[tuple[int, int]] = []
    i = open_at
    while i < len(body):
        ch = body[i]
        if ch in "'\"":
            end = _string_end(body, i, in_brackets=brackets > 0)
            if end is None:
                return None
            strings.append((i, end))
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i, strings
        elif ch in "[{":
            brackets += 1
        elif ch in "]}":
            brackets = max(0, brackets - 1)
        elif ch == "\n" and _ACTION_LABEL.match(body, i + 1):
            return None
        i += 1
    return None


def _string_end(body: str, start: int, *, in_brackets: bool = False) -> int | None:
    """Index of the quote that closes the string opened at ``body[start]``.

    A quote closes the string only when what follows it can follow a
    value — ``,``, ``)`` or the end, and ``]`` / ``}`` when the string is an
    element of a list or set (``point=['10', '20']``). Any other quote is an
    apostrophe the model did not escape (``content='it's done'``), the
    commonest way a text argument breaks Python syntax. Brackets count only
    when the scanner saw them outside a string, so ``content='see ['x'] here'``
    still reads as one string.
    """
    quote = body[start]
    if body.startswith(quote * 3, start):
        end = body.find(quote * 3, start + 3)
        return None if end < 0 else end + 2
    closers = ",)]}" if in_brackets else ",)"
    j = start + 1
    while j < len(body):
        ch = body[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            k = _skip(body, j + 1, " \t\r\n")
            if k >= len(body) or body[k] in closers:
                return j
        j += 1
    return None


def _read_call(
    body: str,
    start: int,
    end: int,
    strings: list[tuple[int, int]],
    label: str,
    raw: str,
) -> _Call:
    """Parse ``body[start:end + 1]`` as a Python call of literal arguments."""
    source = body[start : end + 1]
    # Re-escape each single-quoted literal so Python can read what the
    # scanner already delimited: stray inner quotes and raw newlines.
    pieces: list[str] = []
    cursor = start
    for s, e in strings:
        pieces.append(body[cursor:s])
        literal = body[s : e + 1]
        quote = literal[0]
        if not literal.startswith(quote * 3):
            inner = literal[1:-1]
            inner = re.sub(rf"(?<!\\){re.escape(quote)}", "\\" + quote, inner)
            inner = inner.replace("\r\n", "\\n").replace("\n", "\\n")
            literal = f"{quote}{inner}{quote}"
        pieces.append(literal)
        cursor = e + 1
    pieces.append(body[cursor : end + 1])
    normalized = "".join(pieces)

    def fail(detail: str) -> ParseError:
        return ParseError(detail, raw_output=raw[:500])

    try:
        node = ast.parse(normalized.strip(), mode="eval").body
    except SyntaxError:
        raise fail(f"{label} is not a well-formed call: {_clip(source)!r}.") from None
    if not isinstance(node, ast.Call):
        raise fail(f"{label} is not a call: {_clip(source)!r}.")
    if isinstance(node.func, ast.Name):
        verb = node.func.id
    elif isinstance(node.func, ast.Attribute):
        verb = node.func.attr
    else:
        raise fail(f"{label} does not name a verb: {_clip(source)!r}.")

    kwargs: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg is None:
            raise fail(f"{label}: {verb}() cannot take **arguments.")
        if kw.arg in kwargs:
            # ``ast`` accepts ``f(a=1, a=2)``; keeping the last would run a
            # call the model wrote two ways.
            raise fail(f"{label}: {verb}() is given {kw.arg} twice.")
        try:
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError):
            raise fail(
                f"{label}: argument {kw.arg} of {verb}() must be a quoted value, "
                f"e.g. {kw.arg}='...'."
            ) from None
    return _Call(
        verb=verb,
        kwargs=kwargs,
        positional=len(node.args),
        source=source,
        label=label,
    )
