# Portions of this module are derived from AndroidWorld's
# android_world/agents/m3a_utils.py (parse_reason_action_output, extract_json),
# android_world/agents/agent_utils.py (extract_json),
# android_world/env/json_action.py (JSONAction's checks) and
# android_world/env/actuation.py (what each action type does)
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
# Modified by Spectragent (2026-11-03): rewritten to read a reply into Spectragent's
# actions, by relative name. Kept: the reading of the reason and the action,
# JSONAction's checks, and M3A's two sentences for a reply it cannot read. Changed:
# each action type runs as the capability's actions and the Loop's done; the
# departures from AndroidWorld are listed in the module docstring.

"""M3A's reply, read as AndroidWorld reads it, and the framework actions it means.

Ported from AndroidWorld (``google-research/android_world`` at commit
``55ddaf5edc6f6c25673aed343907d5b9dca66f84``, Apache-2.0; ``THIRD_PARTY_NOTICES.md``):
``m3a_utils.parse_reason_action_output`` and its ``extract_json``,
``agent_utils.extract_json``, ``json_action.JSONAction``'s checks, and what
``m3a.py`` and ``actuation.py`` do with each action type.

A reply is ``Reason: ...`` then ``Action: {json}``. The reason is everything
between the first ``Reason:`` and the last ``Action:``; the action is the
first ``{...}`` after the first ``Action:``, written back as ``json.dumps``
writes it, which is how M3A quotes it in its history and its summary prompt
(:attr:`M3AReply.action`). A reply without either is refused with M3A's own
words (:data:`NOT_IN_FORMAT`), an action that is not one M3A's prompt teaches
or that AndroidWorld's ``JSONAction`` would refuse with its other words
(:data:`NOT_AN_ACTION`); both go back as a :class:`ParseError`, and M3A's
history says the same (``agent.py``).

**What each action type runs as** (relative names, bound by the run, §4.5):

- ``click`` / ``long_press``: ``click`` / ``long_press`` with the element's
  ``index`` (the capability taps or holds the centre of its bounds);
- ``input_text``: ``type`` with the ``index``, the ``text`` and ``clear`` set
  only by ``clear_text`` (M3A never clears unless told), then ``press_key``
  ``enter``, because AndroidWorld's ``input_text`` taps the field, types and
  presses Enter. So one reply can run as two actions. Without an index (and
  without a point) it is ``type_coordinate`` without a point, into the field
  that has focus, as AndroidWorld then types without a tap;
- ``keyboard_enter`` / ``navigate_home`` / ``navigate_back``: ``press_key``
  ``enter`` / ``home`` / ``back``;
- ``scroll``: ``scroll`` with the ``direction`` and the optional ``index``.
  M3A's direction is the content's, as ours is: ``down`` shows what is
  further down. AndroidWorld swipes for it from the centre of the element (or
  the screen) up to its top edge in 300 ms, ``input swipe``'s default; the
  Android ``scroll`` swipes so with ``swipe_to: target_edge`` and
  ``duration_ms: 300``, which a run for M3A sets;
- ``open_app``: ``open_app`` with the ``app_name``; ``wait``: ``wait``;
- ``answer``: ``done`` with the text as its answer and no claim;
- ``status``: ``done`` with no answer, claiming success for ``complete`` and
  failure for ``infeasible``; another ``goal_status`` ends the run too, as
  AndroidWorld's does, with no claim (``docs/architecture_design.md`` §4.1,
  "agent 的任务结论": the claim and the answer stay apart).

**Where this departs from AndroidWorld** (``docs/architecture_design.md`` §4.1
"M3A" and ``THIRD_PARTY_NOTICES.md`` list the same):

- *Points.* ``click``, ``long_press`` and ``input_text`` given a point (``x``
  and ``y``) instead of an index are refused: M3A's prompt never teaches
  points, and AndroidWorld would touch the point. A point beside an index is
  refused, as ``JSONAction`` refuses it; the other action types ignore a
  point, as AndroidWorld's actuation does.
- *Index 0.* A ``scroll`` with ``index`` 0 scrolls element 0; AndroidWorld's
  ``if action.index:`` reads 0 as no index and scrolls the whole screen.
- *The element an index names.* An index names an element of the UI tree the
  agent saw, and the action touches where that element was then (the user's
  ruling of 2026-11-01, ``docs/architecture_design.md`` §11.2 "动作").
  AndroidWorld reads the element list again just before it runs the action
  (``AsyncAndroidEnv.execute_action``) and takes the element with that index
  in the new list: another element when the screen changed after the agent
  saw it, and a failed action when the new list is shorter.
- *Typing needs a text field with focus.* ``type`` taps the element and then
  types only once a text field has focus; ``type_coordinate`` without a point
  types only into a text field that has focus (the capability's focus rule,
  ``docs/architecture_design.md`` §4.9). AndroidWorld types whether or not a
  field took focus.
- *A failed type skips the Enter.* ``input_text`` is ``type`` (or
  ``type_coordinate`` without a point) and then ``press_key`` ``enter``; when
  the typing fails (the focus rule refuses it, or it fails for another reason,
  such as the typing backend refusing on the device), the step stops there and
  the Enter is not pressed (``docs/architecture_design.md`` §4.1: a failed
  action stops its step). AndroidWorld's ``input_text`` taps, types and then presses Enter
  whether or not the text went in (``env/actuation.py``).
- *Refused before running*, with :data:`NOT_AN_ACTION`: the action types
  M3A's prompt does not teach (``double_tap``, ``swipe``, ``unknown``), which
  AndroidWorld runs (``unknown`` as nothing); ``input_text`` without text and
  ``scroll`` without a direction, for which AndroidWorld runs nothing and
  summarizes the step; ``click`` or ``long_press`` with neither an index nor
  a point, and ``open_app`` without an app name, which AndroidWorld fails
  while running.
- *A failed action* gets the history line ``Action selected: <action>. The
  action failed: <error>``, in the capability's words (``agent.py``):
  AndroidWorld writes no line for an action that failed while running, and
  refuses an index past the element list before running, in its own words.
- *``answer`` ends the run* with its text as the answer; AndroidWorld keeps
  the answer and goes on until ``status``.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
from dataclasses import dataclass

from spectragent.core.loops.errors import ParseError
from spectragent.schemas.action import Action

__all__ = ["NOT_AN_ACTION", "NOT_IN_FORMAT", "M3AReply", "actions_of", "read_reason", "read_reply"]

#: M3A's history line for a reply without a reason and an action.
NOT_IN_FORMAT = (
    "Output for action selection is not in the correct format, so no action is performed."
)
#: M3A's history line for an action it cannot run as written.
NOT_AN_ACTION = (
    "Can not parse the output to a valid action. Please make sure to pick the action from the "
    "list with required parameters (if any) in the correct JSON format!"
)

_REASON = re.compile(r"Reason:(.*)Action:", re.DOTALL)
_ACTION = re.compile(r"Action:(.*)", re.DOTALL)
#: ``m3a_utils.extract_json``: the first object, across lines.
_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)
#: ``agent_utils.extract_json``: the first object on one line.
_OBJECT_ON_A_LINE = re.compile(r"\{.*?\}")

#: ``JSONAction``'s fields: an action carrying any other key is refused.
_FIELDS = frozenset(
    {
        "action_type",
        "index",
        "x",
        "y",
        "text",
        "direction",
        "goal_status",
        "app_name",
        "keycode",
        "clear_text",
    }
)
_DIRECTIONS = ("left", "right", "down", "up")
#: The system key each key action presses.
_KEYS = {"keyboard_enter": "enter", "navigate_home": "home", "navigate_back": "back"}
#: The goal statuses M3A's prompt teaches, and the claim each states.
_CLAIMS = {"complete": True, "infeasible": False}


@dataclass(frozen=True)
class M3AReply:
    """A reply's reason and action, as M3A reads them.

    Attributes:
        reason: The text between ``Reason:`` and ``Action:``, stripped.
        action: The action as M3A writes it into its history and summary
            prompt: its object, ``json.dumps``-ed, when Python can read it as
            a literal, else the text after ``Action:``.
    """

    reason: str
    action: str


def read_reply(text: str) -> M3AReply:
    """The reason and the action of *text*, as ``parse_reason_action_output`` reads them.

    Raises:
        ParseError: :data:`NOT_IN_FORMAT`, when either is missing or empty.
    """
    reason = read_reason(text)
    found = _ACTION.search(text)
    action = found.group(1).strip() if found else ""
    if action:
        extracted = _literal(action)
        if extracted is not None:
            # A literal JSON cannot write (a set) is kept as written.
            with contextlib.suppress(TypeError):
                action = json.dumps(extracted)
    if not reason or not action:
        raise ParseError(NOT_IN_FORMAT, raw_output=text[:500])
    return M3AReply(reason=reason, action=action)


def read_reason(text: str) -> str | None:
    """The reason of *text*, stripped, or ``None`` when it states none."""
    found = _REASON.search(text)
    reason = found.group(1).strip() if found else ""
    return reason or None


def actions_of(reply: M3AReply) -> list[Action]:
    """The actions *reply*'s action runs as, by relative name (module docstring).

    Raises:
        ParseError: :data:`NOT_AN_ACTION`, for an action ``JSONAction`` would
            refuse, one this port does not run, or one missing what its type
            needs.
    """
    try:
        return _actions(_fields(reply.action))
    except _Refused:
        raise ParseError(NOT_AN_ACTION, raw_output=reply.action[:500]) from None


class _Refused(ValueError):
    """An action M3A cannot run as written; it is refused with :data:`NOT_AN_ACTION`."""


def _literal(text: str) -> object | None:
    """``m3a_utils.extract_json``: the first object in *text*, read as a Python literal, or ``None``."""
    found = _OBJECT.search(text)
    if found is None:
        return None
    try:
        return ast.literal_eval(found.group())
    except (SyntaxError, ValueError, TypeError):
        return None


def _fields(action: str) -> dict[str, object]:
    """``agent_utils.extract_json``: the first object on a line, read as a literal, else as JSON."""
    found = _OBJECT_ON_A_LINE.search(action)
    if found is None:
        raise _Refused
    value: object
    try:
        value = ast.literal_eval(found.group())
    except (SyntaxError, ValueError, TypeError):
        try:
            value = json.loads(found.group())
        except ValueError:
            raise _Refused from None
    if not isinstance(value, dict):
        raise _Refused
    fields: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _FIELDS:
            raise _Refused
        fields[key] = item
    return fields


def _actions(fields: dict[str, object]) -> list[Action]:
    """The actions *fields* run as, after ``JSONAction``'s checks."""
    kind = fields.get("action_type")
    index = _index(fields)
    direction = fields.get("direction")
    if direction and direction not in _DIRECTIONS:
        raise _Refused
    text = fields.get("text")
    if text is not None and not isinstance(text, str):
        text = str(text)
    keycode = fields.get("keycode")
    if keycode is not None and not (isinstance(keycode, str) and keycode.startswith("KEYCODE_")):
        raise _Refused
    if kind == "status":
        params: dict[str, object] = {"extracted_content": None}
        claim = _CLAIMS.get(str(fields.get("goal_status")))
        if claim is not None:
            params["success"] = claim
        return [Action(name="done", params=params)]
    if kind == "answer":
        return [Action(name="done", params={"extracted_content": text})]
    if kind in ("click", "long_press"):
        return [Action(name=str(kind), params={"index": _required(index)})]
    if kind == "input_text":
        if not text:
            raise _Refused  # AndroidWorld then runs nothing
        typed: dict[str, object] = {"text": text, "clear": bool(fields.get("clear_text"))}
        if index is not None:
            typing = Action(name="type", params={"index": index, **typed})
        elif fields.get("x") is not None and fields.get("y") is not None:
            raise _Refused  # a point, which M3A's prompt never teaches
        else:  # into the field with focus, as AndroidWorld then types without a tap
            typing = Action(name="type_coordinate", params=typed)
        return [typing, Action(name="press_key", params={"key": "enter"})]
    if isinstance(kind, str) and kind in _KEYS:
        return [Action(name="press_key", params={"key": _KEYS[kind]})]
    if kind == "scroll":
        if not direction:
            raise _Refused  # AndroidWorld then runs nothing
        scroll: dict[str, object] = {"direction": direction}
        if index is not None:
            scroll["index"] = index
        return [Action(name="scroll", params=scroll)]
    if kind == "open_app":
        app = fields.get("app_name")
        if not isinstance(app, str) or not app:
            raise _Refused
        return [Action(name="open_app", params={"app_name": app})]
    if kind == "wait":
        return [Action(name="wait", params={})]
    raise _Refused


def _index(fields: dict[str, object]) -> int | None:
    """``JSONAction``'s index: an int, or ``None``; with a point beside it, refused."""
    index = fields.get("index")
    if index is None:
        return None
    if fields.get("x") is not None or fields.get("y") is not None:
        raise _Refused
    if isinstance(index, (int, float, str)):  # ``int()`` as ``JSONAction`` converts
        try:
            return int(index)
        except (ValueError, OverflowError):  # ``"two"``, ``nan``; ``Infinity``, ``1e999``
            pass
    raise _Refused


def _required(index: int | None) -> int:
    """The element *index* an action addresses; a point instead, or nothing, is refused."""
    if index is None:
        raise _Refused
    return index
