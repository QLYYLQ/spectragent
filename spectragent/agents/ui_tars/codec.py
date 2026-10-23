"""The UI-TARS text protocol, stateless: reply text to thought and actions, and back.

A UI-TARS model answers in the text protocol it was trained on::

    Thought: the search box is at the top
    Action: click(point='(640, 88)')

This module reads and writes that protocol and nothing else. It keeps no
state: what a run can say is a :class:`UITarsCodec`, an immutable object built
once from the run's :class:`~spectragent.schemas.action_space.ActionSpace`
(``UITarsAgent.prepare``). The agent's Loop slots are one-line calls into it,
so a live step, a branch replay and the training export read and write the
same text (``docs/architecture_design.md`` §4.1, §4.15).

**One declaration per verb.** :data:`VERBS` gives each verb the action it means,
a capability-relative id that the run's bindings resolve
(``core/action_guard.py::executed_descriptor``), and its keyword arguments:
the spelling the prompt teaches, the other spellings a UI-TARS model writes
for the same thing (``start_box``), the kind of value, and the action param it
fills. That one entry reads a call into an action, writes an action back as
the call, and gives the prompt its signature. A point argument names no
param: it fills the next point the action declares
(``ActionDescriptor.point_params``), so this module never restates which
params are a point.

**Points stay on the model's grid.** A point read is the numbers the reply
writes (a box's centre for four); a point written is the numbers it is handed.
The framework moves points between the model's grid and the capability's units
(``core/loops/coordinate_frames.py``). A point spelling is read, never scraped:
the documented forms and nothing else (:func:`_point_coordinates`), so a model
that names its target (``point='btn2'``) is refused, never clicked at digits
taken out of the name.

**What a reply looks like.** An action is a line that starts with ``Action:``;
:func:`_action_calls` reads those and nothing else. Every way a reply can fail
that reading (empty, no action line, a call cut off, text after a call, an
unknown verb, an argument the verb does not take) is a :class:`ParseError`
whose detail says which and how to fix it, because the Loop hands that detail
back to the model verbatim. All or nothing: one unreadable line fails the
whole reply.

**Writing is the inverse.** :meth:`UITarsCodec.write` writes the reply whose
reading executes as the actions it is handed, or raises
:class:`UnrepresentableActionError` for an action or a param value the
protocol cannot carry. Never a silent drop: a reply that executes differently
from the action it was written for is a wrong training label.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from spectragent.core.action_guard import executed_descriptor, executed_name
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace

__all__ = ["VERBS", "Argument", "BoundVerb", "UITarsCodec", "Verb", "read_thought"]

# ── The verb table ────────────────────────────────────────────

#: How an argument's value is read and written. ``point``: two numbers, or a
#: box's four; ``text``: any string, quoted; ``choice``: one of
#: :attr:`Argument.choices`; ``keys``: a key chord, ``+``-joined.
ArgumentKind = Literal["point", "text", "choice", "keys"]


@dataclass(frozen=True)
class Argument:
    """One keyword argument of a verb.

    Attributes:
        name: The spelling the prompt teaches (``point``, ``content``).
        kind: How its value is read and written (:data:`ArgumentKind`).
        example: The value the prompt's signature shows (``'(x, y)'``).
        param: The action param it fills. ``None`` for a point, which fills
            the next point the action declares.
        spellings: Other names a UI-TARS-family model writes for the same
            argument: ``start_box`` / ``end_box`` are UI-TARS 1.0 and 1.5's,
            which a model fine-tuned on that format writes whatever the prompt
            says. Two spellings of one argument in one call are refused.
        required: A call without it is refused. An optional text reads as
            ``''`` when absent and is left out of a written call when empty.
        choices: The values a ``choice`` takes.
    """

    name: str
    kind: ArgumentKind
    example: str
    param: str | None = None
    spellings: tuple[str, ...] = ()
    required: bool = True
    choices: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Every name the argument is read under, the taught one first."""
        return (self.name, *self.spellings)


@dataclass(frozen=True)
class Verb:
    """One UI-TARS verb: the action it means and its arguments, in the order the prompt teaches them.

    Every param the verb fills comes from an argument, so an action param no
    argument fills cannot be written: ``type``'s ``clear``, ``scroll``'s
    ``amount``, ``wait``'s ``seconds``, ``done``'s ``success``.
    """

    name: str
    #: The capability-relative action id. It names no provider: the run's
    #: bindings resolve it to the one this run holds.
    action: str
    arguments: tuple[Argument, ...] = ()

    @property
    def signature(self) -> str:
        """The call the prompt teaches: every argument, with its example value."""
        return f"{self.name}({', '.join(f'{a.name}={a.example}' for a in self.arguments)})"

    @property
    def accepts(self) -> tuple[str, ...]:
        """Every argument name the verb reads; anything else is refused, never dropped."""
        return tuple(name for argument in self.arguments for name in argument.names)


DIRECTIONS = ("up", "down", "left", "right")


def _point(name: str, example: str, *spellings: str) -> Argument:
    return Argument(name, "point", example, spellings=spellings)


#: Every verb UI-TARS may emit, in the order the prompt and the error hints
#: list them. A verb absent from this table is a parse error, not a silently
#: made-up action name. The mobile-only verbs (``long_press``, ``open_app``,
#: ``press_home``, ``press_back``) have no browser action and no entry here;
#: an Android capability brings them as its actions.
VERBS: Mapping[str, Verb] = MappingProxyType(
    {
        verb.name: verb
        for verb in (
            Verb("click", "click_coordinate", (_point("point", "'(x, y)'", "start_box"),)),
            # UI-TARS says ``content``; the action reads ``text``. Required:
            # with ``clear_by_default`` a missing text would clear the field
            # and type nothing, reported as a success.
            Verb(
                "type",
                "type_coordinate",
                (
                    Argument("content", "text", "'text to type'", param="text"),
                    _point("point", "'(x, y)'", "start_box"),
                ),
            ),
            # A direction and nothing else. Not a point: ScrollAction scrolls
            # under the pointer or a selector-map element, and a vision-only
            # run has none. Not an amount: how far is ``ScrollAction.default_amount``,
            # capability-side config; the agent declares that it can scroll.
            Verb(
                "scroll",
                "scroll",
                (
                    Argument(
                        "direction",
                        "choice",
                        "'up|down|left|right'",
                        param="direction",
                        choices=DIRECTIONS,
                    ),
                ),
            ),
            Verb(
                "hotkey",
                "send_keys",
                (Argument("key", "keys", "'ctrl+a'", param="keys", spellings=("keys",)),),
            ),
            Verb(
                "drag",
                "drag",
                (
                    _point("start_point", "'(x1, y1)'", "start_box"),
                    _point("end_point", "'(x2, y2)'", "end_box"),
                ),
            ),
            # How long a wait waits is ``WaitAction.default_seconds``.
            Verb("wait", "wait"),
            # ``done`` reads ``extracted_content``. The task claim: ``finished``
            # has no argument that says whether the task was accomplished (a
            # model ends with ``finished(content='无法完成…')`` too), so it
            # reads back with no claim (``reported_success`` is ``None``), and
            # an action that states one, true or false, cannot be written.
            Verb(
                "finished",
                "done",
                (
                    Argument(
                        "content",
                        "text",
                        "'answer if applicable'",
                        param="extracted_content",
                        required=False,
                    ),
                ),
            ),
        )
    }
)


# ── A verb bound to a run ─────────────────────────────────────


class _VerbError(ValueError):
    """A call the verb cannot read: the detail goes back to the model."""


class _Unwritable(ValueError):
    """A param value the verb's call syntax cannot carry."""

    def __init__(self, params: tuple[str, ...], detail: str) -> None:
        self.params = params
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class BoundVerb:
    """A verb as one run executes it: its declaration and the descriptor of the run action it means."""

    verb: Verb
    descriptor: ActionDescriptor
    #: Each point argument with the ``(x, y)`` params it fills, in order.
    points: tuple[tuple[Argument, tuple[str, str]], ...]

    @classmethod
    def bind(cls, verb: Verb, descriptor: ActionDescriptor) -> BoundVerb:
        """*verb* on *descriptor*: its point arguments matched, in order, to the points the action declares.

        Raises:
            ValueError: The verb writes a different number of points than the
                action declares (``PointAxis`` on its ``Params``): its points
                would have no params to fill, or a point the action takes
                could never be written.
        """
        arguments = [a for a in verb.arguments if a.kind == "point"]
        if len(arguments) != len(descriptor.point_params):
            raise ValueError(
                f"UI-TARS's {verb.name}() writes {len(arguments)} point(s), and "
                f"{descriptor.name}, the action it runs as here, declares "
                f"{len(descriptor.point_params)} (PointAxis on its Params). A verb's points "
                f"fill the points its action declares, so the two have to agree."
            )
        return cls(verb, descriptor, tuple(zip(arguments, descriptor.point_params, strict=True)))

    @property
    def params(self) -> tuple[str, ...]:
        """Every action param the verb fills: its points', then the other arguments'."""
        return (
            *(key for _argument, pair in self.points for key in pair),
            *(a.param for a in self.verb.arguments if a.param is not None),
        )

    def read(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The action params a call's arguments say, its points as written.

        Points are read first, so a call missing both a point and its text
        says the point is missing.

        Raises:
            _VerbError: A required argument is missing or malformed.
        """
        params: dict[str, Any] = {}
        for argument, (x_key, y_key) in self.points:
            params[x_key], params[y_key] = _read_point(kwargs, argument)
        for argument in self.verb.arguments:
            if argument.param is not None:
                params[argument.param] = _READ[argument.kind](kwargs, argument, self.verb)
        return params

    def write(self, params: Mapping[str, Any]) -> str:
        """The call text for *params*, the inverse of :meth:`read`.

        Raises:
            _Unwritable: A value the call syntax cannot carry.
        """
        pairs = dict(self.points)
        written: list[str] = []
        for argument in self.verb.arguments:
            if argument.kind == "point":
                text: str | None = _point_text(params, *pairs[argument])
            else:
                text = _WRITE[argument.kind](params, argument, self.verb)
            if text is not None:
                written.append(f"{argument.name}={text}")
        return f"{self.verb.name}({', '.join(written)})"


def _bind(space: ActionSpace) -> Mapping[str, BoundVerb]:
    """The verbs *space* executes, in table order, each bound to the run action its bindings resolve.

    A verb whose action the run does not resolve (not negotiated, or a short
    name two capabilities offer) is not offered.
    """
    bound: dict[str, BoundVerb] = {}
    for name, verb in VERBS.items():
        descriptor = executed_descriptor(space, verb.action)
        if descriptor is not None:
            bound[name] = BoundVerb.bind(verb, descriptor)
    return MappingProxyType(bound)


# ── The codec ─────────────────────────────────────────────────


@dataclass(frozen=True)
class UITarsCodec:
    """What one run can say in the UI-TARS protocol: the verbs its action space executes.

    Immutable and built once per run (``UITarsAgent.prepare``); every method
    reads only its arguments and this object, so it can be called in any
    order, by anyone holding it.

    Attributes:
        space: The run's effective action space. Its bindings say which run
            action each verb means.
        writes_thought: Whether a written reply carries its thought (the
            ``grounding`` template teaches none).
        verbs: The verbs this run offers (:func:`_bind`), in table order.

    Raises:
        ValueError: A verb and the action it runs as disagree on its points
            (:meth:`BoundVerb.bind`).
    """

    space: ActionSpace = field(default_factory=ActionSpace)
    writes_thought: bool = True
    verbs: Mapping[str, BoundVerb] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "verbs", _bind(self.space))

    def example_call(self) -> str:
        """A call the model could legally write in this run, for error hints."""
        bound = next(iter(self.verbs.values()), None)
        return bound.verb.signature if bound is not None else ""

    def read_actions(self, llm_output: Any) -> list[Action]:
        """Every ``Action:`` line of a reply as an Action named by its relative id, its points as written.

        Raises:
            ParseError: The reply cannot be read (module docstring); the
                detail says which line and what to write instead.
        """
        text = llm_output if isinstance(llm_output, str) else str(llm_output)
        calls = _action_calls(text, example=self.example_call())
        try:
            return [self._action_for(call) for call in calls]
        except _VerbError as exc:
            raise ParseError(str(exc), raw_output=text[:500]) from exc

    def write(self, actions: Sequence[Action], *, thought: str | None = None) -> str:
        """The reply whose reading executes as *actions*, their points already on the model's grid.

        One ``Action:`` line per action, in order. Before them, when
        :attr:`writes_thought` and *thought* is not blank, one ``Thought:``
        line with every run of whitespace collapsed to a space, so nothing in
        a thought can start an action line. The same input writes the same
        text, and the text is a fixed point of write, read, write.

        Raises:
            UnrepresentableActionError: An action no verb of this run writes,
                or a param value the call syntax cannot carry (see
                :meth:`write_call`).
            ValueError: *actions* is empty; no reply carries no action.
        """
        if not actions:
            raise ValueError("format_react_output needs at least one action")
        lines: list[str] = []
        if thought is not None and self.writes_thought:
            collapsed = " ".join(thought.split())
            if collapsed:
                lines.append(f"Thought: {collapsed}")
        lines.extend(f"Action: {self.write_call(action)}" for action in actions)
        return "\n".join(lines)

    def write_fact(self, action: Action) -> str | dict[str, Any]:
        """*action* as UI-TARS shows an action as a fact: its call, or its JSON when no call carries it.

        Its points are already on the frame of the step it belongs to
        (``coordinate_frames.fact_in_model_frame``); a point with no place
        there is the sentence that says so, which no call can spell.
        """
        try:
            return self.write_call(action)
        except UnrepresentableActionError:
            return action.model_dump(mode="json")

    def write_call(self, action: Action) -> str:
        """*action* as the call that reads back into it.

        The verb is the one whose action this run executes *action* as
        (``executed_name``): a qualified name the run binds the verb to a
        different capability for is not this run's action, and has no call.

        Raises:
            UnrepresentableActionError: Naming what cannot be written: a run
                action with no verb, an action this run does not execute, a
                set param no argument fills, or a value with no spelling (a
                point that is not two numbers, a key chord with whitespace in
                it, a direction outside the four).
        """
        full = executed_name(self.space, action.name)
        bound = next(
            (b for b in self.verbs.values() if full is not None and b.descriptor.name == full),
            None,
        )
        if bound is None:
            offered = "; ".join(
                f"{name}() runs as {b.descriptor.name}" for name, b in self.verbs.items()
            )
            detail = (
                f"this run does not execute it ({offered or 'no verb runs here'})"
                if full is None
                else f"UI-TARS has no verb for {full} ({offered})"
            )
            raise UnrepresentableActionError(action.name, (), detail)
        filled = bound.params
        extra = [
            key for key, value in action.params.items() if value is not None and key not in filled
        ]
        if extra:
            raise UnrepresentableActionError(
                action.name, extra, f"{bound.verb.signature} has no argument that carries it"
            )
        try:
            return bound.write(action.params)
        except _Unwritable as exc:
            raise UnrepresentableActionError(action.name, exc.params, exc.detail) from exc

    def _action_for(self, call: _Call) -> Action:
        """One call read through this run's verbs, its points as the model wrote them."""
        bound = self.verbs.get(call.verb)
        if bound is None:
            offered = ", ".join(self.verbs) or "none"
            raise _VerbError(
                f"{call.label}: unknown verb {call.verb!r}; this run offers: {offered}"
            )
        verb = bound.verb
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
            params = bound.read(call.kwargs)
        except _VerbError as exc:
            raise _VerbError(f"{where}: {exc}") from exc
        return Action(name=verb.action, params=params)


def read_thought(llm_output: Any) -> tuple[str, str] | None:
    """The thought of a reply and the field it came from (``"Thought"``), or ``None``.

    The text after the ``Thought:`` label (a line start, dressed as an action
    label may be) up to the first ``Action:`` line, stripped. Reads the reply
    only, so it needs no run.
    """
    text = llm_output if isinstance(llm_output, str) else str(llm_output)
    label = _THOUGHT_LABEL.search(text)
    if label is None:
        return None
    action = _ACTION_LABEL.search(text, label.end())
    body = text[label.end() : action.start() if action is not None else len(text)].strip()
    return (body, "Thought") if body else None


# ── Reading and writing one argument ─────────────────────────


def _argument(kwargs: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    """The value of whichever of *names* the call wrote, or ``None``.

    Two spellings of one argument in one call are refused: keeping either
    would silently discard the other, and they need not agree.
    """
    given = [name for name in names if name in kwargs]
    if len(given) > 1:
        raise _VerbError(f"{' and '.join(given)} say the same thing; write only {names[0]}")
    return kwargs[given[0]] if given else None


def _written(kwargs: Mapping[str, Any], argument: Argument) -> bool:
    return any(name in kwargs for name in argument.names)


def _read_point(kwargs: Mapping[str, Any], argument: Argument) -> tuple[float, float]:
    raw = _argument(kwargs, argument.names)
    if raw is None:
        raise _VerbError(f"a {argument.name} is missing; write {argument.name}='(x, y)'")
    return _parse_point(argument.name, raw)


def _read_text(kwargs: Mapping[str, Any], argument: Argument, verb: Verb) -> str:
    if not _written(kwargs, argument):
        if argument.required:
            raise _VerbError(f"{verb.name} needs {argument.name}={argument.example}")
        return ""
    return str(_argument(kwargs, argument.names))


def _read_choice(kwargs: Mapping[str, Any], argument: Argument, verb: Verb) -> str:
    value = str(_argument(kwargs, argument.names) or "").strip().lower()
    if value not in argument.choices:
        allowed = "|".join(f"'{choice}'" for choice in argument.choices)
        raise _VerbError(f"{verb.name} needs {argument.name}={allowed}")
    return value


def _read_keys(kwargs: Mapping[str, Any], argument: Argument, verb: Verb) -> str:
    raw = _argument(kwargs, argument.names)
    # The executor splits a chord on ``+``. UI-TARS-1.5 spells a chord with
    # spaces (``'ctrl c'``) and some models as a list of key names; either
    # would otherwise be looked up as one key named ``ctrl c``.
    if isinstance(raw, (list, tuple)):
        keys = "+".join(part for name in raw for part in str(name).split())
    else:
        keys = str(raw or "").strip()
        if "+" not in keys and len(keys.split()) > 1:
            keys = "+".join(keys.split())
    if not keys:
        raise _VerbError(f"{verb.name} needs {argument.name}={argument.example}")
    return keys


def _required(params: Mapping[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None:
        raise _Unwritable((key,), f"{key} has no value to write")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _point_text(params: Mapping[str, Any], x_key: str, y_key: str) -> str:
    """A point as a call writes it, ``'(x, y)'``: two numbers, or nothing the syntax can carry.

    A point with no place on the model's grid arrives as the sentence that
    says so (``NOT_PLACEABLE``, ``OUTSIDE_THE_SCREENSHOT``); a call cannot
    spell that, so the action keeps its JSON (:meth:`UITarsCodec.write_fact`).
    """
    x, y = _required(params, x_key), _required(params, y_key)
    if not _is_number(x) or not _is_number(y):
        raise _Unwritable((x_key, y_key), f"({x!r}, {y!r}) is not a point")
    return _quoted(f"({x}, {y})")


def _write_text(params: Mapping[str, Any], argument: Argument, verb: Verb) -> str | None:
    assert argument.param is not None
    if argument.required:
        return _quoted(str(_required(params, argument.param)))
    # No text and empty text read back alike (``''``), so both are left out:
    # one text per reading, which is what keeps the written form a fixed point.
    value = params.get(argument.param)
    return _quoted(str(value)) if value else None


def _write_choice(params: Mapping[str, Any], argument: Argument, verb: Verb) -> str:
    assert argument.param is not None
    value = _required(params, argument.param)
    if value not in argument.choices:
        raise _Unwritable(
            (argument.param,), f"{argument.param}={value!r} is not one of {argument.choices}"
        )
    return _quoted(value)


def _write_keys(params: Mapping[str, Any], argument: Argument, verb: Verb) -> str:
    assert argument.param is not None
    keys = str(_required(params, argument.param))
    # Reading takes whitespace for a chord separator and strips the ends, so a
    # key name with whitespace in it would come back as another chord.
    if not keys or any(ch.isspace() for ch in keys):
        raise _Unwritable(
            (argument.param,),
            f"{argument.param}={keys!r} is empty or holds whitespace, which {verb.name}() "
            f"reads as a chord",
        )
    return _quoted(keys)


_READ = MappingProxyType({"text": _read_text, "choice": _read_choice, "keys": _read_keys})
_WRITE = MappingProxyType({"text": _write_text, "choice": _write_choice, "keys": _write_keys})


def _quoted(text: str) -> str:
    """*text* as a single-quoted literal that :func:`_read_call` reads back as *text*.

    Backslashes and quotes are escaped, and so is every character that is
    not printable: newlines and carriage returns above all, which would
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


# ── Point spellings ───────────────────────────────────────────

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
#: coordinate, a bracket, or a separator. :func:`_point_coordinates` requires
#: these to tile the value with no gap, which is what stops a number glued to
#: a letter from being read as one: ``x1y2``, ``btn2``, ``#id2 > .cls3`` and
#: ``the 2nd button, row 3`` all leave a gap at their first letter.
_POINT_PIECE = re.compile(r"(?P<number>-?\d+(?:\.\d+)?)|[()\[\]{}]|[,\s]+")

#: One coordinate, whole. Used on the elements of a tuple/list literal, where
#: the pieces are already separated: ``point=['10', '20']``.
_COORDINATE = re.compile(r"-?\d+(?:\.\d+)?")


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

    The documented forms are the whole of what is accepted, and each one has
    to account for every character of the value: tags only as a matched pair
    around the rest, then nothing but numbers, brackets and separators. So
    ``'(10, 20)'``, ``'10,20'``, ``'[10, 20]'``, ``'<point>10 20</point>'``,
    ``'<|box_start|>(10,20)<|box_end|>'`` and the two-pair box
    ``'(10,20),(30,40)'`` read, while a selector, an element name or a
    description reads as no coordinate rather than as the digits in it. A
    tuple or list literal is read by element, each a number or a quoted one.
    The numbers come back as written, in order; how many there are and which
    grid they are on are the caller's business.
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
        _VerbError: *value* spells no coordinates, or a count that is neither
            a point nor a box; the detail says what to write instead, because
            an ambiguous point is refused, never guessed at.
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
#: and then refused the task. The price, accepted: a reasoning line that
#: itself begins ``Action:`` is an action line that is not a call, and fails
#: the reply by the all-or-nothing rule.
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
