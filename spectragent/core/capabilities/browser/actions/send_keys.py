"""SendKeysAction — literal keys and native chords on the measured browser platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.capabilities.browser.actions._browser_commands import (
    close_focused_tab,
    history_step,
    reload_page,
    switch_to_tab,
)
from spectragent.core.capabilities.browser.actions._common import (
    effect_from,
    focused_scope,
    handler_field,
)
from spectragent.core.capabilities.browser.actions._mixins import SettleableAction
from spectragent.core.capabilities.browser.effect_probes import (
    FieldProbe,
    ProbeBudget,
    focused_element,
)
from spectragent.core.capabilities.browser.effects import KeyPressEffect, command_name
from spectragent.core.capabilities.browser.input.chords import (
    InvalidKeysError,
    KeyChord,
    parse_key_chord,
)
from spectragent.core.capabilities.browser.input.shortcuts import Runs, Shortcut, browser_shortcut
from spectragent.core.capabilities.browser.watchdogs.contracts import dispatch_request
from spectragent.core.capabilities.browser.watchdogs.events import SendKeysEvent
from spectragent.schemas.action import ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


@dataclass
class SendKeysAction(SettleableAction, ActionBase):
    """Send one key or chord: a named key, a single character, or a ``+`` chord.

    Any other string — a URL, a word, a sentence — is refused before a key
    event is dispatched, with an error that tells the model text belongs in
    ``type`` (``docs/architecture_design.md`` §4.13; ``input/chords.py``).

    A chord that is the browser's own shortcut for Back, Forward, Reload,
    Close tab or a tab switch on the measured input platform runs that command
    instead (``input/shortcuts.py``, user ruling 2026-10-24), through the code
    ``go_back`` / ``go_forward`` / ``switch_tab`` use
    (``actions/_browser_commands.py``); the key does not reach the page.
    macOS's Meta+ArrowLeft / Meta+ArrowRight go to the page when a field that
    takes text has focus, where they move the caret. Any other chord is
    pressed on the page; its effect names a browser shortcut this browser does
    not run.
    """

    __registry_key__ = "browser.send_keys"

    #: The control that had focus, whether its value changed, the chord
    #: pressed, the focus afterwards — and the browser shortcut the chord is.
    Effect = KeyPressEffect

    class Params(BaseModel):
        model_config = ConfigDict(extra="forbid")

        keys: str

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return self.describe(
            "Send literal keys or chords on the observed browser platform. "
            "For select-all use Meta/Cmd/Command+A on macOS or Ctrl/Control+A on Windows/Linux; "
            "other chords retain their literal modifiers. "
            "The browser's own shortcuts for Back, Forward, Reload, Close tab and switching tabs "
            "work (macOS: Meta+[, Meta+], Meta+R, Meta+W, Control+Tab, Meta+1 to Meta+9; "
            "Windows/Linux: Alt+ArrowLeft, Alt+ArrowRight, Control+R, Control+W, Control+Tab, "
            "Control+1 to Control+9); there is no address bar or new-tab page, so their "
            "shortcuts do nothing.",
            **context,
        )

    async def execute(
        self,
        params: dict[str, Any],
        *,
        capability: BrowserCapability,
    ) -> ActionResult:
        try:
            chord = parse_key_chord(str(params["keys"]))
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except InvalidKeysError as exc:
            return ActionResult(success=False, error=str(exc))
        try:
            match = browser_shortcut(chord, capability.input_platform)
            if isinstance(match, Runs):
                ran = await self._run_shortcut(capability, chord, match.shortcut)
                if ran is not None:
                    if ran.success:
                        await self.settle()
                    return ran
            scope = focused_scope(capability)
            event = SendKeysEvent(keys=params["keys"])
            await dispatch_request(scope, event)
            effect = effect_from(self, handler_field(event, "effect_facts"))
            await self.settle()
            return ActionResult(success=True, effect=effect)
        except KeyError as exc:
            return ActionResult(success=False, error=f"Missing param: {exc}")
        except Exception as exc:  # pragma: no cover
            return ActionResult(success=False, error=str(exc))

    async def _run_shortcut(
        self, capability: BrowserCapability, chord: KeyChord, shortcut: Shortcut
    ) -> ActionResult | None:
        """Run the browser command *chord* is the shortcut for; ``None``: the key is the page's.

        The focus is read first (``FieldProbe``), within
        ``action_effect_timeout_s`` like every probe of the action: it is the
        effect's ``target``, and a chord that yields to text goes to the page
        unless nothing that takes text has focus (``entry`` ``"none"``; a focus
        that cannot be read yields too). A command that cannot run — no
        history entry, one tab open, the only tab to close — fails the action
        and names the shortcut; nothing was sent to the page.
        """
        budget = ProbeBudget(capability.action_effect_timeout_s)
        cdp = capability.cdp
        field = FieldProbe(cdp, capability.default_session_id, budget)
        try:
            await field.before()
        finally:
            await budget.run(field.release, "release field")
        if shortcut.yields_to_text and field.entry != "none":
            return None
        try:
            facts = await _run_command(capability, shortcut, budget)
        except Exception as exc:
            name = command_name(shortcut.command, shortcut.tab)
            return ActionResult(
                success=False,
                error=f"{chord.text} is the browser's {name} shortcut, and it did not run: {exc}",
            )
        focus_after = await focused_element(cdp, capability.default_session_id, budget)
        ran = {
            "outcome": "ran",
            "chord": chord.text,
            "command": shortcut.command,
            "tab": shortcut.tab,
            **(facts if isinstance(facts, dict) else {}),
        }
        effect = effect_from(
            self,
            {
                "target": field.target,
                "sensitive": field.sensitive,
                "value_changed": "unavailable",
                "keys": (),
                "text_entered": False,
                "focus_after": focus_after,
                "shortcut": ran,
            },
        )
        return ActionResult(success=True, effect=effect)


async def _run_command(
    capability: BrowserCapability, shortcut: Shortcut, budget: ProbeBudget
) -> Any:
    """Run *shortcut*'s command; the facts of its effect."""
    command = shortcut.command
    if command in ("back", "forward"):
        return await history_step(capability, offset=-1 if command == "back" else 1)
    if command in ("reload", "hard_reload"):
        return await reload_page(capability, ignore_cache=command == "hard_reload")
    if command == "close_tab":
        return await close_focused_tab(capability, budget)
    _, facts = await switch_to_tab(capability, await _tab_to_select(capability, shortcut), budget)
    return facts


async def _tab_to_select(capability: BrowserCapability, shortcut: Shortcut) -> str:
    """The tab a tab-switching shortcut selects, in the order ``browser_meta`` lists the tabs.

    The order the tabs opened (the tab registry's): CDP does not expose
    Chrome's tab strip, and it is the order the model sees. Next and previous
    wrap around, as Chrome's do; a numbered tab past the last one is refused,
    where Chrome does nothing.
    """
    tabs = await capability.tab_ids()
    command = shortcut.command
    if command == "last_tab":
        return tabs[-1]
    if command == "select_tab":
        assert shortcut.tab is not None
        if shortcut.tab > len(tabs):
            raise ValueError(f"only {_tabs_open(len(tabs))}")
        return tabs[shortcut.tab - 1]
    if len(tabs) < 2:
        raise ValueError(f"only {_tabs_open(len(tabs))}")
    current = capability.focused_tab_id
    if current not in tabs:
        raise RuntimeError(f"the focused tab {current} is not among the open tabs {tabs}")
    step = 1 if command == "next_tab" else -1
    return tabs[(tabs.index(current) + step) % len(tabs)]


def _tabs_open(count: int) -> str:
    return "one tab is open" if count == 1 else f"{count} tabs are open"
