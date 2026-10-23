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
parameter the action does not read. Which run action a verb means is the
run's bindings' answer (``core/action_guard.py::executed_name``), and which
of its params are a point is that action's descriptor's
(``ActionDescriptor.point_params``): the table states neither a provider nor
a point.

**Three contracts the prompt states and the parser holds the model to.**

1. *How many actions a reply may carry.* The Loop owns
   ``max_actions_per_step`` and is what truncates; ``AgentRuntime`` hands
   it to :meth:`UITarsAgent.prepare`, which renders the budget
   (:func:`_action_contract`) — one ``Action:`` line per action, run in
   order, at most N. The agent keeps no number of its own.
2. *Which grid a point is on.* ``coordinate_space`` declares whether the
   model answers in pixels of the screenshot or on a 0-1000 grid over it,
   and the prompt teaches it. That declaration is this agent's whole part
   in coordinates: the framework converts every point between the
   declared grid and the capability's units, with the screenshot of the
   step the point belongs to (``core/loops/coordinate_frames.py``,
   ``docs/architecture_design.md`` §4.1 "坐标系归框架"). The parser reads
   the numbers a reply writes, on the model's grid, and nothing more. What
   counts as a point is narrow: the documented spellings and nothing else
   (:func:`_point_coordinates`), so a model that names its target rather
   than locating it (``point='btn2'``) is refused, never clicked at the
   digits scraped out of the name.
3. *What a reply looks like.* An action is a line that starts with
   ``Action:``; :func:`_action_calls` reads those and nothing else, and
   every way a reply can fail that reading — empty, no action line, a
   call cut off, text after a call, an unknown verb, an argument the verb
   does not take — is a :class:`ParseError` whose detail says which and
   how to fix it, because the Loop feeds that detail back to the model
   verbatim.

**Every point the model reads is on the grid it writes.** The facts fed
back — the actions a batch executed, their effects, the pointer — are in
the capability's viewport units, and stay so in the trace. The framework
writes each of their points on the frame of the step it belongs to, read
off that step's record (``StepRecord.frame``; this step's,
``ReactPromptContext.frame``, for the pointer): an action shown as a fact
arrives with its points already on the grid, and this agent writes it as
the call it would have written for it (the same per-verb writing as
:meth:`UITarsAgent.format_react_output`); effect lines and the pointer go
through the framework's ``point_writer``. A point with no place on its
frame — the step had no screenshot of known size, or the point lies outside
it — is said to be so, never written in pixels.
:meth:`UITarsAgent.format_action_fact` is that writing for an action, public
so the training export writes its ``keep_as_text`` turns with it.
"""

from __future__ import annotations

import ast
import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
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
from spectragent.core.action_guard import executed_descriptor, executed_name
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops.coordinate_frames import fact_in_model_frame, point_writer
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace
from spectragent.schemas.observation.image import image_media_type, read_model_image
from spectragent.schemas.prompt import PromptSnapshot, ReactPromptContext, RestoredHistory

# ── The verb vocabulary ──────────────────────────────────────


class _VerbError(ValueError):
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
            that are not a point (the bound descriptor's ``point_params``)
            are rendered into the prompt with the descriptor's own type
            text, so the model sees what its arguments turn into.
            ``tests/test_agents/test_ui_tars/test_ui_tars_prompt_action_vocabulary.py``
            holds this to the descriptor.
        to_params: Translates the verb's kwargs into the action's params.
            Raises :class:`_VerbError` when a required argument is missing
            or malformed — a loud parse failure beats an action that
            reports ``Missing param`` one round trip later.
        accepts: Every argument name the verb reads: the ones
            :attr:`signature` teaches, plus the UI-TARS spellings of the
            same thing (``start_box`` for ``point``). Anything else is
            refused rather than dropped — ``click(button='right')`` run as
            a left click is an action the model never asked for.
        write: The inverse of :attr:`to_params`: the call text for the
            action's params, its points already on the model's grid (the
            framework put them there). Raises :class:`_Unwritable` for a
            value the call syntax cannot carry — a point that is not two
            numbers among them; a param outside :attr:`fills` never reaches
            it.
    """

    action: str
    signature: str
    fills: tuple[str, ...]
    to_params: Callable[[Mapping[str, Any]], dict[str, Any]]
    accepts: tuple[str, ...]
    write: Callable[[Mapping[str, Any]], str]


#: Argument names a UI-TARS-family model uses for the same point. The
#: taught spelling comes first; ``start_box`` / ``end_box`` are UI-TARS 1.0
#: and 1.5's, which a model fine-tuned on that format writes whatever the
#: prompt says.
_POINT_ARGUMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "point": ("point", "start_box"),
        "start_point": ("start_point", "start_box"),
        "end_point": ("end_point", "end_box"),
    }
)

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
        raise _VerbError(f"{' and '.join(given)} say the same thing; write only {names[0]}")
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
        _VerbError: *value* spells no coordinates, or spells a count that is
            neither a point nor a box. Either way the detail says what was
            written and what to write instead, because the Loop hands it
            back to the model verbatim — an ambiguous point is refused, never
            guessed at.
    """
    numbers = _point_coordinates(value)
    if numbers is None:
        raise _VerbError(
            f"{key}={value!r} is not a coordinate; expected {key}='(x, y)' — two "
            f"numbers and nothing else, e.g. {key}='(640, 360)'. Describe nothing "
            f"there: no element name, selector or text, only the numbers"
        )
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 4:
        return (numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2
    raise _VerbError(
        f"{key}={value!r} has {len(numbers)} numbers; write two for a point "
        f"({key}='(x, y)') or four for a box ({key}='(x1, y1, x2, y2)')"
    )


def _point(kwargs: Mapping[str, Any], key: str = "point") -> tuple[float, float]:
    names = _POINT_ARGUMENTS[key]
    raw = _argument(kwargs, names)
    if raw is None:
        raise _VerbError(f"a {key} is missing; write {key}='(x, y)'")
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
        raise _VerbError("type needs content='text to type'")
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
        raise _VerbError("scroll needs direction='up'|'down'|'left'|'right'")
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
        raise _VerbError("hotkey needs key='ctrl+a'")
    return {"keys": keys}


def _wait_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # How long a wait waits is ``WaitAction.default_seconds`` — the same
    # capability-side-config rule as ``scroll``'s amount.
    return {}


def _finished_params(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    # DoneAction reads ``extracted_content``, not ``content``. No ``success``:
    # ``finished`` has no argument that says whether the task was accomplished
    # (a model ends with ``finished(content='无法完成…')`` too), so the run's
    # ``agent_reported_success`` is ``None``, never an invented ``True``.
    return {"extracted_content": str(kwargs.get("content", ""))}


# ── Writing a call: the inverse of the readers above ─────────


class _Unwritable(ValueError):
    """A param value the verb's call syntax cannot carry (see ``_Verb.write``)."""

    def __init__(self, params: tuple[str, ...], detail: str) -> None:
        self.params = params
        self.detail = detail
        super().__init__(detail)


def _quoted(text: str) -> str:
    """*text* as a single-quoted literal that :func:`_read_call` reads back as *text*.

    Backslashes and quotes are escaped, and so is every character that is
    not printable — newlines and carriage returns above all, which would
    otherwise end the action line (or start a new ``Action:`` one).
    """
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif not ch.isprintable():
            out.append(repr(ch)[1:-1])
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def _required(params: Mapping[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None:
        raise _Unwritable((key,), f"{key} has no value to write")
    return value


def _point_text(params: Mapping[str, Any], x_key: str, y_key: str) -> str:
    """A point as the call writes it, ``'(x, y)'`` — two numbers, or nothing the syntax can carry.

    A point with no place on the model's grid arrives as the sentence that
    says so (``NOT_PLACEABLE``, ``OUTSIDE_THE_SCREENSHOT``); a call cannot
    spell that, so the action keeps its JSON (:meth:`UITarsAgent.format_action_fact`).
    """
    x, y = _required(params, x_key), _required(params, y_key)
    if not _is_number(x) or not _is_number(y):
        raise _Unwritable((x_key, y_key), f"({x!r}, {y!r}) is not a point")
    return _quoted(f"({x}, {y})")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _write_click(params: Mapping[str, Any]) -> str:
    return f"click(point={_point_text(params, 'coordinate_x', 'coordinate_y')})"


def _write_type(params: Mapping[str, Any]) -> str:
    text = _quoted(str(_required(params, "text")))
    return f"type(content={text}, point={_point_text(params, 'coordinate_x', 'coordinate_y')})"


def _write_scroll(params: Mapping[str, Any]) -> str:
    direction = _required(params, "direction")
    if direction not in _DIRECTIONS:
        raise _Unwritable(("direction",), f"direction={direction!r} is not one of {_DIRECTIONS}")
    return f"scroll(direction={_quoted(direction)})"


def _write_hotkey(params: Mapping[str, Any]) -> str:
    keys = str(_required(params, "keys"))
    # ``_hotkey_params`` reads whitespace as a chord separator and strips the
    # ends, so a key name with whitespace in it would come back as another
    # chord (or another key): refused, not rewritten.
    if not keys or any(ch.isspace() for ch in keys):
        raise _Unwritable(
            ("keys",),
            f"keys={keys!r} is empty or holds whitespace, which hotkey() reads as a chord",
        )
    return f"hotkey(key={_quoted(keys)})"


def _write_drag(params: Mapping[str, Any]) -> str:
    start = _point_text(params, "start_x", "start_y")
    end = _point_text(params, "end_x", "end_y")
    return f"drag(start_point={start}, end_point={end})"


def _write_wait(params: Mapping[str, Any]) -> str:
    return "wait()"


def _write_finished(params: Mapping[str, Any]) -> str:
    # A claim about the task (``success`` set to true or false) never reaches
    # here: ``finished`` reads back with none (``_finished_params``), so
    # ``success`` is outside the verb's ``fills`` and ``_call_text`` refuses it
    # as unrepresentable, naming the param.
    content = params.get("extracted_content")
    # No content and empty content read back alike (``''``), so both are
    # written as ``finished()`` — one text per reading, which is what keeps
    # the written form a fixed point.
    if not content:
        return "finished()"
    return f"finished(content={_quoted(str(content))})"


#: Every verb UI-TARS may emit. A verb absent from this table is a parse
#: error, not a silently manufactured action name. The mobile-only verbs
#: (``long_press``, ``open_app``, ``press_home``, ``press_back``) have no
#: browser action and no entry here; when an Android capability lands they
#: arrive as its actions.
_VERB_TABLE: Mapping[str, _Verb] = MappingProxyType(
    {
        "click": _Verb(
            action="click_coordinate",
            signature="click(point='(x, y)')",
            fills=("coordinate_x", "coordinate_y"),
            to_params=_click_params,
            accepts=_POINT_ARGUMENTS["point"],
            write=_write_click,
        ),
        "type": _Verb(
            action="type_coordinate",
            signature="type(content='text to type', point='(x, y)')",
            fills=("coordinate_x", "coordinate_y", "text"),
            to_params=_type_params,
            accepts=("content", *_POINT_ARGUMENTS["point"]),
            write=_write_type,
        ),
        "scroll": _Verb(
            action="scroll",
            signature="scroll(direction='up|down|left|right')",
            fills=("direction",),
            to_params=_scroll_params,
            accepts=("direction",),
            write=_write_scroll,
        ),
        "hotkey": _Verb(
            action="send_keys",
            signature="hotkey(key='ctrl+a')",
            fills=("keys",),
            to_params=_hotkey_params,
            accepts=("key", "keys"),
            write=_write_hotkey,
        ),
        "drag": _Verb(
            action="drag",
            signature="drag(start_point='(x1, y1)', end_point='(x2, y2)')",
            fills=("start_x", "start_y", "end_x", "end_y"),
            to_params=_drag_params,
            accepts=(*_POINT_ARGUMENTS["start_point"], *_POINT_ARGUMENTS["end_point"]),
            write=_write_drag,
        ),
        "wait": _Verb(
            action="wait",
            signature="wait()",
            fills=(),
            to_params=_wait_params,
            accepts=(),
            write=_write_wait,
        ),
        "finished": _Verb(
            action="done",
            signature="finished(content='answer if applicable')",
            fills=("extracted_content",),
            to_params=_finished_params,
            accepts=("content",),
            write=_write_finished,
        ),
    }
)


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
    # Domain verbs only. No ``capability`` entry: naming one would weld the
    # agent to one capability (negotiation binds relative keys instead,
    # AGENTS.md §2.5), and nothing at runtime read it — it only
    # narrowed the generated config to whichever capability happened to
    # be registered.
    #
    # Page input is visual; loop history supplies execution feedback. The
    # required actions are what the model cannot work without; the
    # optional four are the rest of its native vocabulary, dropped from
    # both the prompt and the parser's reach when a capability does not
    # offer them.
    #
    # Cross-platform on purpose: ``capture_state`` is the platform-neutral
    # capture fact every capability may provide (optional, so an
    # environment without one still runs). Browser page state
    # (``browser_meta``: URL, title, tabs) is for browser-specific agents
    # and is deliberately not declared here (``docs/architecture_design.md``
    # §4.4, "观测增强按消费方分层").
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
    # ``max_steps`` / ``max_actions_per_step`` are deliberately absent:
    # the Loop owns and enforces them, and AgentRuntime hands the limit to
    # ``prepare()`` so the prompt advertises exactly what will run.
    #
    # ``prompt_template``'s members are the on-disk stems, so nothing has
    # to case-fold a config value into a filename on the way to the loader
    # (``tests/test_agents/test_prompt_tree.py`` asserts the two agree);
    # ``coordinate_space``'s are :data:`CoordinateSpace`'s
    # (``tests/test_agents/test_ui_tars/test_ui_tars_coordinates.py`` asserts that).
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
    # Task text belongs to the prepare seed. Platform is measured by the
    # environment and becomes a system message on the first observation.
    #: The run's effective action space, handed over by ``prepare()``: its
    #: descriptors are what the prompt teaches, its bindings say which run
    #: action a verb means.
    _action_space: ActionSpace = field(default_factory=ActionSpace, init=False, repr=False)
    #: The verbs this run can execute, each with the descriptor of the run
    #: action it means: the table narrowed to the actions the run's bindings
    #: resolve its verbs to, by ``prepare()``. None until then.
    _verbs: dict[str, _Verb] = field(default_factory=dict, init=False, repr=False)
    _bound: dict[str, ActionDescriptor] = field(default_factory=dict, init=False, repr=False)
    _system_prompt_text: str = field(default="", init=False, repr=False)
    _prompt_snapshot: PromptSnapshot | None = field(default=None, init=False, repr=False)
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
    ) -> list[BaseMessage]:
        """Pure builder: produce the seed messages for this run.

        ``task`` is written once in the system prompt. The first observed
        platform is another system message, reset here for each execution
        or independently rendered training segment.

        *action_space* is the run's effective action space
        (``AgentRuntime.action_space``). A verb is offered when the run's
        bindings resolve its action (``executed_descriptor``): a short name
        two capabilities offer binds to neither, and its verb is not taught.

        ``max_actions_per_step`` comes from the Loop, which is what
        actually truncates; the prompt has to promise exactly that.
        """
        max_actions_per_step = execution_limits.max_actions_per_step
        if max_actions_per_step is None:
            raise ValueError("prepare requires the Loop's max_actions_per_step execution limit")
        self._action_space = action_space
        bound = {
            name: executed_descriptor(action_space, verb.action)
            for name, verb in _VERB_TABLE.items()
        }
        self._bound = {name: d for name, d in bound.items() if d is not None}
        self._verbs = {name: _VERB_TABLE[name] for name in self._bound}
        self._input_platform_text = None
        self._system_prompt_text, self._prompt_snapshot = self._build_system_prompt(
            task, max_actions_per_step
        )
        # cache=True: static-prefix breakpoint — see BrowserUseAgent for why it
        # is kept alongside the per-step one.
        return [SystemMessage(content=self._system_prompt_text, cache=True)]

    # ── ReactLoop slots ────────────────────────────────────────

    async def build_react_prompt(
        self, observation: Any, *, context: ReactPromptContext
    ) -> list[BaseMessage]:
        """Return this observation and the preceding batch's execution feedback.

        The first call after prepare also declares the measured platform
        as fixed system context. This is independent of the step number:
        an exported segment may begin at a nonzero source step. Builders
        return messages; only the Loop or offline renderer writes memory.

        UI-TARS is purely vision-driven, so the screenshot is always
        attached. With no screenshot bytes the step degrades to a text
        placeholder so the LLM call still succeeds — and since that step's
        frame has no grid, the framework refuses any point in its reply.

        The pointer of the capture state is written on this step's frame
        (``context.frame``); the previous batch on the frame of the step it
        was chosen on (its record's). A pixel frame states the screenshot's
        size.
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
        """Render the last batch without treating proposed actions as completed.

        History is the existing loop-source contract shared with branch
        reconstruction and export. Earlier batches already have their own
        messages; the singular action_result fragment loses all but the
        last result. Sensitivity markers remain trace-only evidence. A
        returned result's effect is shown only as its ``describe()`` line
        (``"effect"`` in the result), and only with ``show_action_effects``.

        Each action is the call this agent writes for it and every point —
        the actions' and the effect lines' — is on the frame the batch was
        chosen on, its record's (``StepRecord.frame``), put there by the
        framework.
        """
        if not history.items:
            return "<action_results>\nNo previous action batch.\n</action_results>"
        record = history.items[-1]
        if len(record.action_results) > len(record.actions):
            raise ValueError("UI-TARS action feedback has more results than actions")
        returned = len(record.action_results)
        write_point = point_writer(record.frame)

        def fact(action: Action) -> str | dict[str, Any]:
            return self.format_action_fact(
                fact_in_model_frame(action, record.frame, self._action_space)
            )

        facts = {
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
                fact(action) for action in record.actions[returned:]
            ],
            "discarded_action_count": record.truncated_actions,
            "error_phase": record.error_phase or None,
            "error": record.error,
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
        points — the executed actions', their effect lines' and the recorded
        source actions' — are written on its frame (``context.frame``, which
        the restored step's record keeps for the next step's feedback).
        """
        history.validate_execution()
        messages = await self.build_react_prompt(observation, context=context)
        restored = restored_history_message(
            history,
            show_action_effects=self.show_action_effects,
            context=context,
            action_space=self._action_space,
            write_action=self.format_action_fact,
        )
        return [*messages, restored]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        """Read every ``Action:`` line of a reply into an Action, its points as the model wrote them.

        All or nothing: one unreadable line fails the whole reply. Running
        the readable half of a multi-action plan — the tiles but not the
        line that was meant to come between them — does something the
        model never asked for. A point is the two numbers the reply writes
        (a box's centre for four), on the model's grid: the framework moves
        it into the capability's units with this step's frame.
        """
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        calls = _action_calls(text, example=self._example_call())
        try:
            return [self._action_for(call) for call in calls]
        except _VerbError as exc:
            raise ParseError(str(exc), raw_output=text[:500]) from exc

    def get_react_output_model(self) -> type[BaseModel] | None:
        # UI-TARS speaks a native text protocol, not structured output.
        return None

    # ── Training-export slots (optional ReAct slots, arch §4.1) ──

    def format_react_output(self, actions: Sequence[Action], *, thought: str | None = None) -> str:
        """The reply this agent writes for *actions* — the inverse of :meth:`parse_react_output`.

        *actions* are already on the model's grid: the training export puts a
        recorded action's points there with the step's frame
        (``coordinate_frames.actions_in_model_frame``), so a point the
        framework reads back from this text is within the grid's ``quantum``
        of where it was, and the text is a fixed point of format → parse →
        format. Stateless: the same input writes the same text whenever it
        is called.

        One ``Action:`` line per action, in order, each written as its verb's
        call (``_Verb.write``); before them, unless the template is
        ``grounding`` (which teaches no thought) or *thought* is ``None`` or
        blank, one ``Thought:`` line holding *thought* with every run of
        whitespace collapsed to a space — one line, so nothing in a thought
        can start an action line.

        Raises:
            UnrepresentableActionError: an action the call syntax cannot
                write, naming what — a verb UI-TARS has no word for or this
                run did not negotiate, a qualified name the run binds the
                verb to a different capability for, a point that is not two
                numbers, or a param value the verb cannot carry (``type``'s
                ``clear``, ``scroll``'s ``amount`` / ``index``, ``wait``'s
                ``seconds``, ``done``'s ``success`` set to either claim, a
                key chord with whitespace in it). Never silently dropped: a
                reply that executes differently from the action it was
                written for is a wrong training label.
            ValueError: *actions* is empty; no reply carries no action.
        """
        if not actions:
            raise ValueError("format_react_output needs at least one action")
        lines: list[str] = []
        if thought is not None and self.prompt_template != "grounding":
            collapsed = " ".join(thought.split())
            if collapsed:
                lines.append(f"Thought: {collapsed}")
        lines.extend(f"Action: {self._call_text(action)}" for action in actions)
        return "\n".join(lines)

    def format_action_fact(self, action: Action) -> str | dict[str, Any]:
        """*action* as this agent shows an action as a fact, its points already on the model's grid.

        The writing of every action UI-TARS shows rather than replies with:
        the previous batch in each observation, restored history, and the
        training export's ``keep_as_text`` turns. The framework has put the
        action's points on the frame of the step it was chosen on
        (``coordinate_frames.fact_in_model_frame``), where a point with no
        place there holds the sentence that says so. This writes the call
        :meth:`format_react_output` writes for it; an action that call syntax
        cannot carry — no verb, not negotiated, a value with no spelling, or
        a point that is such a sentence — keeps its JSON. Stateless, so it
        can be called in any order. Optional ReAct slot (arch §4.1).
        """
        try:
            return self._call_text(action)
        except UnrepresentableActionError:
            return action.model_dump(mode="json")

    @staticmethod
    def parse_react_thought(llm_output: Any) -> tuple[str, str] | None:
        """The thought of a recorded reply and the field it came from, or ``None``.

        UI-TARS's native ``Thought:`` — the text after the label (a line
        start, with the same markdown dressing an action label may carry) up
        to the first ``Action:`` line, stripped. Pure: reads the reply only,
        so the training export calls it on the class (arch §4.1).
        """
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        label = _THOUGHT_LABEL.search(text)
        if label is None:
            return None
        action = _ACTION_LABEL.search(text, label.end())
        body = text[label.end() : action.start() if action is not None else len(text)].strip()
        return (body, "Thought") if body else None

    # ── private helpers ────────────────────────────────────────

    def _example_call(self) -> str:
        """A call the model could legally write this run, for error hints."""
        verb = next(iter(self._verbs.values()), None)
        return verb.signature if verb is not None else ""

    def _call_text(self, action: Action) -> str:
        """*action*, its points on the model's grid, as the call :meth:`_action_for` reads back into it.

        The verb is the one whose action this run executes *action* as
        (``executed_name``): a qualified name the run binds the verb to a
        different capability for is not this run's action, and has no call.
        """
        full = executed_name(self._action_space, action.name)
        verb_name = next(
            (
                name
                for name, bound in self._bound.items()
                if full is not None and bound.name == full
            ),
            None,
        )
        if verb_name is None:
            offered = "; ".join(
                f"{name}() runs as {bound.name}" for name, bound in self._bound.items()
            )
            detail = (
                f"this run does not execute it ({offered or 'no verb runs here'})"
                if full is None
                else f"UI-TARS has no verb for {full} ({offered})"
            )
            raise UnrepresentableActionError(action.name, (), detail)
        verb = self._verbs[verb_name]
        params = dict(action.params)
        extra = [
            key for key, value in params.items() if value is not None and key not in verb.fills
        ]
        if extra:
            raise UnrepresentableActionError(
                action.name, extra, f"{verb.signature} has no argument that carries it"
            )
        try:
            return verb.write(params)
        except _Unwritable as exc:
            raise UnrepresentableActionError(action.name, exc.params, exc.detail) from exc

    def _action_for(self, call: _Call) -> Action:
        """Translate one call through the negotiated verbs, its points as the model wrote them."""
        verb = self._verbs.get(call.verb)
        if verb is None:
            offered = ", ".join(self._verbs) or "none"
            raise _VerbError(
                f"{call.label}: unknown verb {call.verb!r}; this run offers: {offered}"
            )
        where = f"{call.label}, {_clip(call.source)}"
        if call.positional:
            raise _VerbError(f"{where}: pass arguments by name, as in {verb.signature}")
        unexpected = [name for name in call.kwargs if name not in verb.accepts]
        if unexpected:
            raise _VerbError(
                f"{where}: {call.verb}() does not take {', '.join(unexpected)}; "
                f"write it as {verb.signature}"
            )
        try:
            params = verb.to_params(call.kwargs)
        except _VerbError as exc:
            raise _VerbError(f"{where}: {exc}") from exc
        return Action(name=verb.action, params=params)

    def _build_system_prompt(
        self,
        task: str,
        max_actions_per_step: int,
    ) -> tuple[str, PromptSnapshot]:
        action_format, action_budget = _action_contract(
            max_actions_per_step, can_finish="finished" in self._verbs
        )
        return self.render(
            self.prompt_template,
            {
                "task": task,
                "action_space": _render_action_space(self._action_space.descriptors, self._bound),
                "action_format": action_format,
                "action_budget": action_budget,
                "coordinate_space": (
                    f"{_COORDINATE_PROMPTS[self.coordinate_space]} {_POINT_SYNTAX}"
                ),
            },
        )


def _render_action_space(
    descriptors: Sequence[ActionDescriptor], bound: Mapping[str, ActionDescriptor]
) -> str:
    """Render the negotiated actions as UI-TARS call syntax.

    One block per negotiated action::

        - type(content='text to type', point='(x, y)')
          Click a point in the page viewport to focus a text field, then type.
          executes: text: str

    The verb and its syntax come from :data:`_VERB_TABLE`, which is what
    the parser reads; the description and the parameter types come from
    the descriptor, which is what the action actually executes. Both
    halves are derived, so the prompt has no second vocabulary to drift
    from. *bound* is each offered verb's descriptor (``UITarsAgent.prepare``).

    The ``executes`` line lists the params the verb produces *as written*,
    named as a consequence rather than as syntax: the descriptor's names
    belong to the executor (``text``), the model writes the verb's
    (``content``). Point params (the descriptor's ``point_params``) are left
    out of it: the verb's syntax spells a point as ``point='(x, y)'``, and the
    prompt's Coordinates section is what says which grid it is on.

    A negotiated action with no verb is skipped rather than advertised:
    the model has no syntax for it, so teaching its name would invite an
    unparseable call. The wiring and the table are kept in agreement by
    ``tests/test_agents/test_ui_tars/test_ui_tars_react_compatible.py``.
    """
    verb_of = {descriptor.name: name for name, descriptor in bound.items()}
    blocks: list[str] = []
    for d in descriptors:
        verb_name = verb_of.get(d.name)
        if verb_name is None:
            continue
        verb = _VERB_TABLE[verb_name]
        lines = [f"- {verb.signature}", f"  {d.description}"]
        converted = {key for pair in d.point_params for key in pair}
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

#: The start of a ``Thought:`` line, dressed like :data:`_ACTION_LABEL` may be.
_THOUGHT_LABEL = re.compile(
    r"^[ \t>*_-]*thought[ \t*_]*:[ \t*_]*",
    re.IGNORECASE | re.MULTILINE,
)

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
