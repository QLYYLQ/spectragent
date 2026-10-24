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
:class:`TextInputEffect`    ``type``, ``type_coordinate``
:class:`KeyPressEffect`     ``send_keys`` (a text-input effect, plus the browser
                            shortcut the chord is, ``input/shortcuts.py``)
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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.core.capabilities.browser.platform import InputPlatform
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


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _url_change(before: Location | Unmeasured, after: Location | Unmeasured) -> str:
    """How the tab's URL went, in lower case: what a navigation's line starts with."""
    if isinstance(before, Location) and isinstance(after, Location):
        if before.url == after.url:
            return f"the URL stayed {_clip(after.url)}"
        return f"the URL changed from {_clip(before.url)} to {_clip(after.url)}"
    if isinstance(after, Location):
        assert not isinstance(before, Location)
        return f"the URL is now {_clip(after.url)} (the URL before is {_gap(before)})"
    return f"the URL after the action is {_gap(after)}"


def _tab(fact: TabFacts | Unmeasured, *, with_url: bool) -> str:
    if isinstance(fact, TabFacts):
        return fact.text() if with_url else f"tab {fact.tab_id}"
    return f"a tab that is {_gap(fact)}"


def _tab_switch(before: TabFacts | Unmeasured, after: TabFacts | Unmeasured) -> str:
    """Which tab the focus left and which it is on, in lower case: a tab switch's line."""
    return f"switched from {_tab(before, with_url=False)} to {_tab(after, with_url=True)}"


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
    (``"nothing"``: the keys went to the page itself) — or, for a typing
    action that sent nothing because no text field had focus (it failed, its
    ``keys`` empty and ``text_entered`` false), the focus it found.
    ``sensitive``: whether the shared rule judges it a password, one-time-code
    or payment field.
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
        return f"{_capital(_url_change(self.before, self.after))}; {_loaded(self.document_loaded)}."


class TabSwitchEffect(ActionEffect):
    """Which tab was focused before and after, and whether the new tab's document had loaded."""

    before: TabFacts | Unmeasured
    after: TabFacts | Unmeasured
    document_loaded: bool | Unmeasured

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        return f"{_capital(_tab_switch(self.before, self.after))}; {_loaded(self.document_loaded)}."


#: A browser command a ``send_keys`` chord runs: the chord is the browser's own
#: shortcut for it on the measured input platform (``input/shortcuts.py``, user
#: ruling 2026-10-24). ``select_tab`` is a numbered tab (``ShortcutRan.tab``).
BrowserCommand = Literal[
    "back",
    "forward",
    "reload",
    "hard_reload",
    "close_tab",
    "next_tab",
    "previous_tab",
    "select_tab",
    "last_tab",
]
#: A browser command whose shortcut needs browser UI this browser does not
#: show. Never run: the key goes to the page, and the effect says so.
BrowserUiCommand = Literal[
    "address_bar",
    "new_tab",
    "reopen_closed_tab",
    "new_window",
    "history",
    "downloads",
    "bookmarks",
    "find",
]
#: The commands whose facts are a tab's URL; the others' are tabs.
NAVIGATION_COMMANDS: frozenset[str] = frozenset({"back", "forward", "reload", "hard_reload"})

_COMMAND_NAMES: dict[str, str] = {
    "back": "Back",
    "forward": "Forward",
    "reload": "Reload",
    "hard_reload": "Hard reload",
    "close_tab": "Close tab",
    "next_tab": "Next tab",
    "previous_tab": "Previous tab",
    "last_tab": "Select last tab",
    "address_bar": "address bar",
    "new_tab": "New tab",
    "reopen_closed_tab": "Reopen closed tab",
    "new_window": "New window",
    "history": "History",
    "downloads": "Downloads",
    "bookmarks": "Bookmarks",
    "find": "Find",
}
#: What the browser would show for a :data:`BrowserUiCommand`, and this one does not.
_MISSING_UI: dict[str, str] = {
    "address_bar": "address bar",
    "new_tab": "new-tab page",
    "reopen_closed_tab": "tab strip",
    "new_window": "window",
    "history": "history page",
    "downloads": "downloads page",
    "bookmarks": "bookmarks manager",
    "find": "find bar",
}
_PLATFORM_NAMES: dict[str, str] = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}


def command_name(command: str, tab: int | None = None) -> str:
    """A browser command as the effect line names it: ``Back``, ``Select tab 2``."""
    if command == "select_tab":
        return f"Select tab {tab}"
    return _COMMAND_NAMES[command]


class ShortcutRan(_Fact):
    """The chord is the browser's shortcut for *command* on the measured platform, and it ran.

    The key did not reach the page (``docs/architecture_design.md`` §4.13):
    the command ran through the code the matching action uses. ``before`` /
    ``after``: the tab's URL (:class:`Location`) for the navigation commands,
    the focused tab (:class:`TabFacts`) for the tab commands — for
    ``close_tab`` the tab closed and the tab the focus moved to.
    ``document_loaded``: whether the document shown afterwards had finished
    loading. ``tab``: the 1-based position a ``select_tab`` named.
    """

    outcome: Literal["ran"] = "ran"
    chord: str
    command: BrowserCommand
    tab: int | None = Field(default=None, ge=1)
    before: Location | TabFacts | Unmeasured
    after: Location | TabFacts | Unmeasured
    document_loaded: bool | Unmeasured

    @model_validator(mode="after")
    def _facts_fit_the_command(self) -> ShortcutRan:
        wrong = TabFacts if self.command in NAVIGATION_COMMANDS else Location
        if isinstance(self.before, wrong) or isinstance(self.after, wrong):
            raise ValueError(f"{self.command} reports no {wrong.__name__}")
        if (self.tab is not None) != (self.command == "select_tab"):
            raise ValueError("a tab position belongs to select_tab, and only to it")
        return self

    def line(self) -> str:
        head = f"{self.chord} ran the browser's {command_name(self.command, self.tab)}"
        before, after = self.before, self.after
        if self.command in NAVIGATION_COMMANDS:
            assert not isinstance(before, TabFacts) and not isinstance(after, TabFacts)
            facts = _url_change(before, after)
        else:
            assert not isinstance(before, Location) and not isinstance(after, Location)
            if self.command == "close_tab":
                facts = (
                    f"closed {_tab(before, with_url=True)}, and the focus moved to "
                    f"{_tab(after, with_url=True)}"
                )
            else:
                facts = _tab_switch(before, after)
        return f"{head}; {facts}; {_loaded(self.document_loaded)}."


class ShortcutWithoutUi(_Fact):
    """The chord is the measured platform's shortcut for browser UI this browser does not show.

    Nothing ran in the browser; the key went to the page as any other key.
    """

    outcome: Literal["no_browser_ui"] = "no_browser_ui"
    chord: str
    command: BrowserUiCommand

    def line(self) -> str:
        return (
            f"{self.chord} is the browser's {command_name(self.command)} shortcut; this browser "
            f"shows no {_MISSING_UI[self.command]}, so nothing in the browser happened."
        )


class ShortcutOfAnotherPlatform(_Fact):
    """The chord is a browser shortcut on another platform only; *binding* is the measured one's.

    Reported only when nothing editable had focus: in a field such a chord is
    often an editing key of the measured platform (Alt+ArrowLeft on macOS moves
    the caret a word). ``platform``: the measured input platform.
    """

    outcome: Literal["other_platform"] = "other_platform"
    chord: str
    command: BrowserCommand | BrowserUiCommand
    tab: int | None = Field(default=None, ge=1)
    platform: InputPlatform
    binding: str

    def line(self) -> str:
        name = command_name(self.command, self.tab)
        platform = _PLATFORM_NAMES[self.platform]
        missing = _MISSING_UI.get(self.command)
        if missing is not None:
            return (
                f"On {platform} the browser's {name} shortcut is {self.binding}; this browser "
                f"shows no {missing}."
            )
        return f"On {platform} the browser's {name} is {self.binding}."


BrowserShortcut = Annotated[
    ShortcutRan | ShortcutWithoutUi | ShortcutOfAnotherPlatform, Field(discriminator="outcome")
]


class KeyPressEffect(TextInputEffect):
    """``send_keys``: a text-input effect, plus the browser shortcut the chord is.

    ``shortcut`` is ``None`` for a chord that is no browser shortcut the
    capability knows (``input/shortcuts.py``). :class:`ShortcutRan`: the
    browser ran the command, and no key reached the page — ``keys`` is empty,
    ``text_entered`` false and ``value_changed`` ``"unavailable"``; ``target``
    is the focus when the chord was pressed and ``focus_after`` the focus on
    the tab shown afterwards. :class:`ShortcutWithoutUi` and
    :class:`ShortcutOfAnotherPlatform`: the key went to the page as any key
    does, measured as such, and the line adds one sentence about the browser.
    """

    shortcut: BrowserShortcut | None = None

    def describe(self, *, write_point: PointWriter = write_viewport_point) -> str:
        if isinstance(self.shortcut, ShortcutRan):
            return self.shortcut.line()
        line = super().describe(write_point=write_point)
        return line if self.shortcut is None else f"{line} {self.shortcut.line()}"


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
    "NAVIGATION_COMMANDS",
    "BrowserCommand",
    "BrowserShortcut",
    "BrowserUiCommand",
    "ElementFact",
    "ElementFacts",
    "KeyPressEffect",
    "Location",
    "NavigationEffect",
    "Nothing",
    "OptionFacts",
    "PointerEffect",
    "ScrollEffect",
    "ScrollOffset",
    "SelectionEffect",
    "ShortcutOfAnotherPlatform",
    "ShortcutRan",
    "ShortcutWithoutUi",
    "TabFacts",
    "TabSwitchEffect",
    "TextInputEffect",
    "ViewportPoint",
    "WaitEffect",
    "command_name",
]
