"""``extract_content``'s element markup, with the sensitivity rule applied (§4.12, §5.6).

``browser.extract_content`` of one element returns its outer HTML, and the
result goes into the agent's next prompt. ``withhold_sensitive_markup`` applies
the shared rule (``dom/sensitivity.py``) to that markup at the observation
path's positions: the ``value`` attribute of a sensitive control and of every
element inside it, and the text inside it. The markup below is what Chrome 155
serializes (``DOM.getOuterHTML``: attributes double-quoted, ``&``, ``"``, ``<``
and ``>`` escaped in values, ``<noscript>`` content raw). The real-Chrome half
is ``test_browser_archive_live.py::test_privacy_guard_on_the_dom_path``.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.dom import sensitivity
from spectragent.core.capabilities.browser.dom.models import DOMElementNode
from spectragent.core.capabilities.browser.dom.sensitivity import (
    REDACTED_MARKUP,
    withhold_sensitive_markup,
)
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import ExtractContentEvent
from tests.test_core.test_capabilities.test_browser_sensitivity import CONTROL_TABLE

SECRET = "secret-Qv7"


def _control(tag: str, attrs: dict[str, str]) -> str:
    """One control as Chrome serializes it, holding :data:`SECRET` where it holds a value."""
    written = "".join(f' {name}="{value}"' for name, value in attrs.items())
    if tag == "textarea":
        return f"<textarea{written}>{SECRET}</textarea>"
    if tag == "select":
        return f'<select{written}><option value="{SECRET}">{SECRET}</option></select>'
    if tag == "input":
        return f'<input{written} value="{SECRET}">'
    return f'<{tag}{written} value="{SECRET}">{SECRET}</{tag}>'


class TestTheRule:
    @pytest.mark.parametrize(("tag", "attrs", "sensitive"), CONTROL_TABLE)
    def test_the_shared_control_table(
        self, tag: str, attrs: dict[str, str], sensitive: bool
    ) -> None:
        """Withheld exactly where the shared rule says so; untouched elsewhere."""
        markup = _control(tag, attrs)
        withheld = withhold_sensitive_markup(markup)
        if sensitive:
            assert SECRET not in withheld and REDACTED_MARKUP in withheld, withheld
        else:
            assert withheld == markup

    def test_markup_without_a_sensitive_control_comes_back_byte_for_byte(self) -> None:
        markup = (
            '<div id="card" class="a &amp; b">\n'
            '  <label for="q">Search &lt;all&gt;</label>\n'
            '  <input type="text" id="q" value="shoes &amp; socks">\n'
            "  <!-- a comment -->\n"
            '  <select name="country"><option value="de" selected="">Deutschland</option></select>\n'
            '  <textarea name="comment">hello\nworld</textarea>\n'
            '  <script>var s = "<input type=password value=x>";</script>\n'
            "  <style>p > b { color: red }</style>\n"
            '  <br><img src="a.png" alt=""><p>café &nbsp; ok</p>\n'
            "</div>"
        )
        assert withhold_sensitive_markup(markup) == markup

    def test_only_the_value_changes(self) -> None:
        markup = (
            '<input type="password" id="pw" name="password" value="hunter2" autocomplete="off">'
        )
        assert withhold_sensitive_markup(markup) == (
            '<input type="password" id="pw" name="password" value="&lt;redacted&gt;" '
            'autocomplete="off">'
        )

    def test_an_escaped_value_is_withheld_whole(self) -> None:
        markup = '<input type="password" value="Qv7&quot;&lt;x&gt;&amp;9">'
        assert (
            withhold_sensitive_markup(markup) == '<input type="password" value="&lt;redacted&gt;">'
        )

    @pytest.mark.parametrize(
        "markup",
        [
            '<input type="password" value="">',
            '<input type="password">',
            '<textarea name="otp_code"></textarea>',
            '<textarea name="otp_code">  \n </textarea>',
        ],
        ids=["empty value", "no value", "empty text", "blank text"],
    )
    def test_empty_stays_empty(self, markup: str) -> None:
        assert withhold_sensitive_markup(markup) == markup

    def test_withholding_twice_changes_nothing_more(self) -> None:
        once = withhold_sensitive_markup(_control("select", {"name": "pin"}))
        assert withhold_sensitive_markup(once) == once

    def test_everything_inside_a_sensitive_select(self) -> None:
        """A customizable select copies the chosen option into ``<selectedcontent>``."""
        markup = (
            '<select name="pin"><button><selectedcontent>4711</selectedcontent></button>'
            '<option value="4711" selected="">4711</option>'
            '<optgroup label="more"><option value="0815">0815</option></optgroup></select>'
        )
        withheld = withhold_sensitive_markup(markup)
        assert "4711" not in withheld and "0815" not in withheld, withheld
        assert withheld.count(REDACTED_MARKUP) == 5, withheld

    def test_only_the_sensitive_controls_of_a_form(self) -> None:
        markup = (
            '<form action="/login">\n'
            '<input type="text" name="user" value="alice">\n'
            f'<input type="password" name="password" value="{SECRET}">\n'
            f'<textarea name="otp">{SECRET}</textarea>\n'
            '<textarea name="note">keep me</textarea>\n'
            '<button type="submit" value="go">Sign in</button>\n'
            "</form>"
        )
        withheld = withhold_sensitive_markup(markup)
        assert SECRET not in withheld
        for kept in ('value="alice"', "keep me", 'value="go"', "Sign in", "\n</form>"):
            assert kept in withheld, kept

    def test_a_template_and_noscript_content_are_markup(self) -> None:
        markup = (
            f'<div><template><input type="password" value="{SECRET}"></template>'
            f"<noscript><input type=password value={SECRET}></noscript></div>"
        )
        withheld = withhold_sensitive_markup(markup)
        assert SECRET not in withheld
        assert '<noscript><input type="password" value="&lt;redacted&gt;"></noscript>' in withheld

    def test_comments_inside_a_sensitive_control(self) -> None:
        withheld = withhold_sensitive_markup(f'<select name="pin"><!-- {SECRET} --></select>')
        assert withheld == '<select name="pin"><!--<redacted>--></select>'


class TestInsideASensitiveControl:
    def test_an_option_of_a_sensitive_select(self) -> None:
        markup = f'<option value="{SECRET}">{SECRET} <b>{SECRET}</b></option>'
        withheld = withhold_sensitive_markup(markup, inside_sensitive=True)
        assert withheld == (
            '<option value="&lt;redacted&gt;">&lt;redacted&gt;<b>&lt;redacted&gt;</b></option>'
        )
        assert withhold_sensitive_markup(markup) == markup


def test_markup_that_does_not_parse_is_withheld_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """What cannot be checked is not passed on (the stdlib parser asserts on some declarations)."""

    def give_up(self: Any, data: str) -> None:
        raise AssertionError("unknown status keyword")

    monkeypatch.setattr(sensitivity._MarkupScan, "feed", give_up)
    assert withhold_sensitive_markup('<input type="text" value="x">') == REDACTED_MARKUP


# ── the extract handler applies it ────────────────────────────────────


class _OuterHTMLCDP:
    """Answers ``DOM.getOuterHTML`` with *markup* and the page-text read with *text*."""

    def __init__(self, markup: str, text: str = "") -> None:
        self.markup = markup
        self.text = text

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        if method == "DOM.getOuterHTML":
            return {"outerHTML": self.markup}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": self.text}}
        return {}


def _option_in(select_attrs: dict[str, str]) -> tuple[DOMElementNode, DOMElementNode]:
    """A ``<select>`` and its indexed ``<option>``, linked the way the cleaners link them."""
    select = DOMElementNode(tag_name="select", backend_node_id=10, attributes=select_attrs)
    option = DOMElementNode(tag_name="option", backend_node_id=11, attributes={"value": SECRET})
    option.parent = select
    select.children.append(option)
    return select, option


async def _extract(markup: str, selector_map: dict[int, DOMElementNode], backend_id: int) -> Any:
    watchdog = ActionWatchdog(
        cdp=_OuterHTMLCDP(markup),  # type: ignore[arg-type]
        selector_map_getter=lambda: selector_map,
    )
    return await watchdog._on_extract_content(ExtractContentEvent(backend_node_id=backend_id))


class TestTheExtractHandler:
    async def test_an_element_comes_back_withheld(self) -> None:
        markup = f'<input type="password" id="pw" value="{SECRET}">'
        extracted = await _extract(markup, {}, backend_id=7)
        assert extracted == '<input type="password" id="pw" value="&lt;redacted&gt;">'

    async def test_an_option_of_a_sensitive_select(self) -> None:
        _select, option = _option_in({"name": "pin"})
        markup = f'<option value="{SECRET}">{SECRET}</option>'
        extracted = await _extract(markup, {11: option}, backend_id=11)
        assert SECRET not in extracted and REDACTED_MARKUP in extracted

    async def test_an_option_of_a_plain_select(self) -> None:
        _select, option = _option_in({"name": "country"})
        markup = f'<option value="{SECRET}">{SECRET}</option>'
        assert await _extract(markup, {11: option}, backend_id=11) == markup

    async def test_the_page_text_is_returned_as_read(self) -> None:
        watchdog = ActionWatchdog(cdp=_OuterHTMLCDP("", text="Welcome back"))  # type: ignore[arg-type]
        assert await watchdog._on_extract_content(ExtractContentEvent()) == "Welcome back"
