"""What browser actions measurably did: the ``Effect`` models of the browser's actions.

Outside the ``actions`` package on purpose: the executor that measures most of
these facts (``watchdogs/action_watchdog.py``) builds them too, and the action
package imports the watchdogs.

``docs/architecture_design.md`` §4.5, "动作效果". An action measures its effect
while it runs — before and after its own dispatch, so the facts belong to that
action alone; the page after a whole batch is the next observation's — and
returns it in ``ActionResult.effect``. The models here are shared between
sibling actions:

==========================  ====================================================
Effect                      Actions
==========================  ====================================================
:class:`PointerEffect`      ``click``, ``click_coordinate``, ``drag``
:class:`TextInputEffect`    ``type``, ``type_coordinate``, ``send_keys``
:class:`ScrollEffect`       ``scroll``
:class:`NavigationEffect`   ``navigate``, ``go_back``, ``go_forward``
:class:`TabSwitchEffect`    ``switch_tab``
:class:`SelectionEffect`    ``select_dropdown``
:class:`WaitEffect`         ``wait``
==========================  ====================================================

``done``, ``screenshot`` and ``extract_content`` report none.

Every fact is a value or one of the two gaps of
:data:`~spectragent.schemas.action.Unmeasured`: ``"unknown"`` (the probe failed
or ran out of ``action_effect_timeout_s``) or ``"unavailable"`` (it cannot be
measured there, such as inside a frame of another site). A measured zero is a
value. String facts are wrapped in small models (:class:`Location`,
:class:`TabFacts`, :class:`OptionFacts`) so a page whose URL or option text
happens to read ``unknown`` is never mistaken for a gap. ``"nothing"`` is a
measured value too: no element at the point, or no element focused.

No fact echoes what was typed or how long it was: a text-input effect says
whether the field's value changed, and a sensitive field (the shared rule in
``dom/sensitivity.py``) is marked so. :meth:`describe` derives one English line
from these fields alone; Agents render that line. The facts are CSS px of the
viewport; every *point* a line states (where a pointer pressed and released) is
written by the caller's ``write_point``, so an Agent shows it on the grid its
model writes points on. Scroll offsets are distances, not points: they stay in px.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.action import (
    ActionEffect,
    PointWriter,
    Unmeasured,
    write_viewport_point,
)

#: Measured: no element at the point, or no element focused.
Nothing = Literal["nothing"]
#: How far a readable name or URL is quoted in :meth:`ActionEffect.describe`.
_DESCRIBE_TEXT_CHARS = 160


class _Fact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ViewportPoint(_Fact):
    """A point in CSS px of the page viewport, where input was dispatched."""

    x: int
    y: int

    def text(self, write_point: PointWriter = write_viewport_point) -> str:
        """The point as *write_point* writes it (the renderer's grid; viewport units by default)."""
        return write_point(self.x, self.y)


class ElementFacts(_Fact):
    """An element as a model may name it: its role and readable name, never its value.

    ``role`` is the ARIA role, explicit or implied by the tag (``button``,
    ``textbox``, ``link``, ``combobox``, ``img``; ``generic`` when neither
    says). ``name`` is its accessible name as far as the page states it
    (``aria-label``, a ``<label>``, ``alt``, ``title``, ``placeholder``, its own
    text), whitespace collapsed, at most 80 characters; ``""`` when it has none.
    A field's value is never its name, and neither is the text of editable
    content (``contenteditable``), which is what was typed into it.
    """

    role: str
    name: str = ""

    def text(self) -> str:
        return f'{self.role} "{_clip(self.name)}"' if self.name else self.role


class ScrollOffset(_Fact):
    """A scroll container's offset, CSS px from its top-left scroll origin."""

    x: int
    y: int


class Location(_Fact):
    """The URL of the document a tab showed."""

    url: str


class TabFacts(_Fact):
    """A tab: its identifier (as ``browser_meta`` lists it) and the document it showed.

    ``location`` is a gap of its own, so a tab whose identifier is known keeps
    it when its URL could not be read.
    """

    tab_id: str
    location: Location | Unmeasured

    def text(self) -> str:
        if isinstance(self.location, Location):
            return f"tab {self.tab_id} ({_clip(self.location.url)})"
        return f"tab {self.tab_id} (its URL is {_gap(self.location)})"


class OptionFacts(_Fact):
    """The selected option of a dropdown: its 0-based index and visible text.

    ``text`` is ``None`` when the dropdown is a sensitive control (the shared
    rule): its options are the user's data, so the text is withheld.
    """

    index: int
    text: str | None


ElementFact = ElementFacts | Nothing | Unmeasured


def _gap(fact: str) -> str:
    """How a gap reads in a sentence: ``unknown``, or ``not measurable here``."""
    return "not measurable here" if fact == "unavailable" else fact


def _clip(text: str) -> str:
    return text if len(text) <= _DESCRIBE_TEXT_CHARS else text[: _DESCRIBE_TEXT_CHARS - 3] + "..."


def _element(fact: ElementFact, *, nothing: str) -> str:
    if isinstance(fact, ElementFacts):
        return fact.text()
    if fact == "nothing":
        return nothing
    return "an unknown element" if fact == "unknown" else "an element that is not measurable here"


def _focus(fact: ElementFact) -> str:
    if isinstance(fact, ElementFacts):
        return f"focus is now on {fact.text()}"
    if fact == "nothing":
        return "nothing is focused now"
    return f"what is focused now is {_gap(fact)}"


def _loaded(fact: bool | Unmeasured) -> str:
    if fact is True:
        return "the document has finished loading"
    if fact is False:
        return "the document was still loading when the action returned"
    return f"whether the document finished loading is {_gap(fact)}"


class PointerEffect(ActionEffect):
    """Where the pointer pressed, what was there, and where focus went afterwards.

    ``point``: where the press was dispatched. ``release_point``: a drag's
    release point; ``None`` for a click, which releases where it pressed.
    ``hit``: the element under ``point`` right before the press — what the
    page hit-tested, which an overlay can make something other than the
    intended element. ``on_target``: for a click that named an element
    (``click`` by index), whether ``hit`` is that element or inside it;
    ``None`` when the action named no element. ``focus_after``: the focused
    element once the action returned.
    """

    point: ViewportPoint | Unmeasured
    release_point: ViewportPoint | None = None
    hit: ElementFact
    on_target: bool | Unmeasured | None = None
    focus_after: ElementFact

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        point = (
            self.point.text(write_point)
            if isinstance(self.point, ViewportPoint)
            else f"a point that is {_gap(self.point)}"
        )
        if self.release_point is not None:
            release = self.release_point.text(write_point)
            head = f"Dragged from {point} to {release}, pressing on "
        else:
            head = f"Clicked at {point} on "
        head += _element(self.hit, nothing="no element")
        if self.on_target is True:
            head += ", the targeted element"
        elif self.on_target is False:
            head += ", not the targeted element"
        elif self.on_target is not None:
            head += f"; whether that is the targeted element is {_gap(self.on_target)}"
        return f"{head}; {_focus(self.focus_after)}."


class TextInputEffect(ActionEffect):
    """Which control received keyboard input, whether its value changed, and the keys sent.

    ``target``: the element that had keyboard focus when the input was sent
    (``"nothing"``: the keys went to the page itself). ``sensitive``: whether
    the shared rule judges it a password, one-time-code or payment field.
    ``value_changed``: whether its value differs after the action — compared
    in the page, never recorded; ``"unavailable"`` when there was no field.
    ``keys``: the named keys and chords dispatched, canonically written
    (``Control+A``, ``Backspace``, ``Enter``); typed characters are never
    listed. ``text_entered``: whether text was inserted (``type``), as opposed
    to keys alone (``send_keys``). ``focus_after``: the focused element once the
    action returned.
    """

    target: ElementFact
    sensitive: bool | Unmeasured
    value_changed: bool | Unmeasured
    keys: tuple[str, ...] = ()
    text_entered: bool
    focus_after: ElementFact

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        target = _element(self.target, nothing="nothing focused")
        if self.sensitive is True:
            target += " (a sensitive field; its content is not shown)"
        steps: list[str] = []
        if self.keys:
            steps.append("pressed " + ", ".join(self.keys))
        if self.text_entered:
            steps.append("typed text")
        verb = ", then ".join(steps) if steps else "sent no input"
        line = (
            f"{verb[0].upper()}{verb[1:]} {'with' if self.target == 'nothing' else 'into'} {target}"
        )
        if self.target != "nothing":
            if self.value_changed is True:
                line += "; its value changed"
            elif self.value_changed is False:
                line += "; its value did not change"
            else:
                line += f"; whether its value changed is {_gap(self.value_changed)}"
        return f"{line}; {_focus(self.focus_after)}."


class ScrollEffect(ActionEffect):
    """Which container scrolled, its offsets before and after, and whether it reached its edge.

    ``container``: ``page`` (the document's scrolling element) or ``element``
    (a scroll box, named by ``element``), whichever actually moved — or, when
    nothing moved, the one the wheel was aimed at. ``"unavailable"``: the
    wheel went into a frame whose scrolling cannot be read. ``before`` /
    ``after``: that container's offsets around the action; their difference is
    :attr:`delta`, ``(0, 0)`` when nothing moved because every container the
    wheel could reach was already at its edge. When one still had room and no
    move was seen within the measurement budget, the wheel may land later:
    ``after`` and ``at_boundary`` are then ``"unknown"``, never a measured zero.
    ``at_boundary``: after the action, the container cannot scroll further in
    ``direction``.
    """

    direction: Literal["up", "down", "left", "right"]
    container: Literal["page", "element"] | Unmeasured
    element: ElementFacts | None = None
    before: ScrollOffset | Unmeasured
    after: ScrollOffset | Unmeasured
    at_boundary: bool | Unmeasured

    @property
    def delta(self) -> ScrollOffset | None:
        """``after - before`` when both were measured, else ``None``."""
        if isinstance(self.before, ScrollOffset) and isinstance(self.after, ScrollOffset):
            return ScrollOffset(x=self.after.x - self.before.x, y=self.after.y - self.before.y)
        return None

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        if self.container == "page":
            what = "the page"
        elif self.container == "element":
            what = f"element {self.element.text()}" if self.element is not None else "an element"
        else:
            what = f"a container that is {_gap(self.container)}"
        axis = "y" if self.direction in ("up", "down") else "x"
        delta = self.delta
        if delta is not None and isinstance(self.before, ScrollOffset):
            assert isinstance(self.after, ScrollOffset)
            moved = abs(getattr(delta, axis))
            line = (
                f"Scroll {self.direction} moved {what} by {moved} px "
                f"({axis} {getattr(self.before, axis)} → {getattr(self.after, axis)})"
            )
        else:
            gap = self.after if not isinstance(self.after, ScrollOffset) else self.before
            assert not isinstance(gap, ScrollOffset)
            line = f"Scroll {self.direction} went to {what}; how far it moved is {_gap(gap)}"
        edge = {"up": "top", "down": "bottom", "left": "left", "right": "right"}[self.direction]
        if self.at_boundary is True:
            line += f"; it is at its {edge} edge"
        elif self.at_boundary is False:
            line += f"; it can scroll further {self.direction}"
        else:
            line += f"; whether it is at its {edge} edge is {_gap(self.at_boundary)}"
        return line + "."


class NavigationEffect(ActionEffect):
    """The URL before and after a navigation, and whether the new document finished loading."""

    before: Location | Unmeasured
    after: Location | Unmeasured
    document_loaded: bool | Unmeasured

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        if isinstance(self.before, Location) and isinstance(self.after, Location):
            if self.before.url == self.after.url:
                line = f"The URL stayed {_clip(self.after.url)}"
            else:
                line = f"The URL changed from {_clip(self.before.url)} to {_clip(self.after.url)}"
        elif isinstance(self.after, Location):
            assert not isinstance(self.before, Location)
            line = f"The URL is now {_clip(self.after.url)} (the URL before is {_gap(self.before)})"
        else:
            line = f"The URL after the action is {_gap(self.after)}"
        return f"{line}; {_loaded(self.document_loaded)}."


class TabSwitchEffect(ActionEffect):
    """Which tab was focused before and after, and whether the new tab's document had loaded."""

    before: TabFacts | Unmeasured
    after: TabFacts | Unmeasured
    document_loaded: bool | Unmeasured

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        before = (
            f"tab {self.before.tab_id}"
            if isinstance(self.before, TabFacts)
            else f"a tab that is {_gap(self.before)}"
        )
        if isinstance(self.after, TabFacts):
            after = self.after.text()
        else:
            after = f"a tab that is {_gap(self.after)}"
        return f"Switched from {before} to {after}; {_loaded(self.document_loaded)}."


class SelectionEffect(ActionEffect):
    """The dropdown's selected option after the action, and whether the selection changed."""

    selected: OptionFacts | Unmeasured
    value_changed: bool | Unmeasured

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        if isinstance(self.selected, OptionFacts):
            text = (
                f'"{_clip(self.selected.text)}"'
                if self.selected.text is not None
                else "(text withheld: a sensitive field)"
            )
            line = f"The dropdown now shows option {self.selected.index} {text}"
        else:
            line = f"The dropdown's selected option is {_gap(self.selected)}"
        if self.value_changed is True:
            line += "; the selection changed"
        elif self.value_changed is False:
            line += "; the selection did not change"
        else:
            line += f"; whether the selection changed is {_gap(self.value_changed)}"
        return line + "."


class WaitEffect(ActionEffect):
    """How long the wait actually lasted, by the monotonic clock."""

    waited_ms: int = Field(ge=0)

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return f"Waited {self.waited_ms} ms."


__all__ = [
    "ElementFact",
    "ElementFacts",
    "Location",
    "NavigationEffect",
    "Nothing",
    "OptionFacts",
    "PointerEffect",
    "ScrollEffect",
    "ScrollOffset",
    "SelectionEffect",
    "TabFacts",
    "TabSwitchEffect",
    "TextInputEffect",
    "ViewportPoint",
    "WaitEffect",
]
