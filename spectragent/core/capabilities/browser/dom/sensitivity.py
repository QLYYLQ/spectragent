"""Is this form control's value a secret? — the one Python rule (§4.12).

A sensitive control's value never reaches the prompt, the recorded DOM
observation or the archive. The Skyvern page-side script
(``dom/js/skyvern_extraction.py``) applies the rule inside the page while it
builds its tree; this module is the same rule for everything in Python:

- the BrowserUse cleaner (``dom/cleaning/browser_use.py``), which withholds
  the values while it builds its tree;
- the archive's raw-DOM redaction (``archive/redaction.py``);
- the typing actions, which judge the focused element before they type and
  record the verdict (``ActionResult.typed_into_sensitive_field``);
- ``extract_content`` of one element, whose outer HTML has the same values
  withheld (:func:`withhold_sensitive_markup`) before it becomes the action's
  result, and so part of the next prompt.

The page script and this module are kept equal by tests, not by generating
one from the other: the hint pattern's literal must equal the one in the
page script character for character, and a table of controls is judged by
both sides in a real Chrome.

Outside the rule's reach: the text a typing action enters stays in the
action's parameters (the step record, the model's own replies), and the
page text ``extract_content`` returns without an element (the body's
``innerText``) holds no field's value but does list a ``<select>``'s option
labels, a sensitive one's included.

The rule: a control is an ``INPUT``, ``TEXTAREA`` or ``SELECT`` and

- it is an ``INPUT`` whose ``type`` is ``password``, ``file`` or ``hidden``, or
- its ``autocomplete`` starts with ``cc-`` (a payment card field) or is
  ``one-time-code``, or
- its ``id``, ``name`` or ``autocomplete`` matches :data:`SENSITIVE_HINT_PATTERN`
  (one-time codes, verification codes, PINs).

It errs wide on purpose (``pin`` matches ``shipping``): a false positive
costs one recorded value, a false negative a leaked one.
"""

import html
import logging
import re
from collections.abc import Mapping
from html.parser import HTMLParser

from spectragent.core.archive.privacy import REDACTED

logger = logging.getLogger(__name__)

#: The page script's ``sensitiveHint`` regex, verbatim (case-insensitive).
SENSITIVE_HINT_PATTERN = re.compile(
    r"(?:otp|one.?time|verification|auth.?code|security.?code|pass.?code|two.?factor|2fa|mfa|pin)",
    re.IGNORECASE,
)

#: Elements that hold a user-entered value.
FORM_CONTROL_TAGS = frozenset({"INPUT", "TEXTAREA", "SELECT"})
#: ``INPUT`` types whose value is always withheld.
SENSITIVE_INPUT_TYPES = frozenset({"password", "file", "hidden"})


def is_sensitive_control(tag: str, attrs: Mapping[str, str]) -> bool:
    """Whether a control's value must be withheld, by the rule above.

    Args:
        tag: The element's tag name, any case (CDP's ``nodeName`` is upper
            case for HTML elements, ``localName`` lower case).
        attrs: Its attributes by (lower-case) name — markup attributes, as
            ``DOM.getDocument`` / ``DOMSnapshot`` / ``DOM.describeNode``
            report them.
    """
    upper = tag.upper()
    if upper not in FORM_CONTROL_TAGS:
        return False
    input_type = str(attrs.get("type") or "").lower()
    autocomplete = str(attrs.get("autocomplete") or "").lower()
    if upper == "INPUT" and input_type in SENSITIVE_INPUT_TYPES:
        return True
    if autocomplete.startswith("cc-") or autocomplete == "one-time-code":
        return True
    # ``[attrs.id, attrs.name, autocomplete].join(' ')`` in the page script:
    # a missing attribute joins as the empty string.
    hint = " ".join((str(attrs.get("id") or ""), str(attrs.get("name") or ""), autocomplete))
    return SENSITIVE_HINT_PATTERN.search(hint) is not None


def attribute_map(flat: list[str] | tuple[str, ...]) -> dict[str, str]:
    """CDP's flat ``[name, value, name, value, …]`` attribute list as a mapping."""
    return {str(flat[i]).lower(): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}


# ── the rule applied to serialized markup ─────────────────────────────

#: Elements the HTML serializer writes with no end tag: the HTML standard's void
#: elements and the legacy ones Chrome still serializes that way.
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
#: The placeholder as serialized markup holds it: Chrome's serializer escapes
#: ``<`` and ``>`` in text and in attribute values alike (measured on Chrome 155).
REDACTED_MARKUP = html.escape(REDACTED)

_TAG_NAME = re.compile(r"<([^\s/>]+)")


def withhold_sensitive_markup(markup: str, *, inside_sensitive: bool = False) -> str:
    """An element's serialized HTML with every sensitive value withheld, by the rule above.

    The observation path's positions (``dom/cleaning/browser_use.py``): the
    ``value`` attribute of a sensitive control and of every element inside it,
    and the text inside it (a ``<textarea>``'s content, a ``<select>``'s
    options and the ``<selectedcontent>`` copy of the chosen one), plus the
    comments there, become ``<redacted>``, escaped as the serializer escapes
    it (:data:`REDACTED_MARKUP`). An empty value and blank text stay as they
    are; every other byte is kept, so markup without a sensitive control comes
    back unchanged. A start tag that loses a value is written again the way
    Chrome serializes one (``name="value"``, escaped).

    *markup* is what ``DOM.getOuterHTML`` returns. The serializer writes the
    content of ``<script>`` and ``<style>`` raw; that text is code, not a
    control, and is kept. A control written inside ``<noscript>`` (raw too,
    with scripting on) is markup all the same and is withheld.

    Args:
        markup: The element's outer HTML.
        inside_sensitive: The element itself lies inside a sensitive control
            (an ``<option>`` of a sensitive ``<select>``): every value in it is
            withheld.

    Returns:
        *markup* with the values withheld; :data:`REDACTED_MARKUP` alone when
        it cannot be read as HTML at all (the parser gave up): what cannot be
        checked is not passed on.
    """
    if not markup:
        return markup
    scan = _MarkupScan(markup, inside_sensitive)
    try:
        scan.feed(markup)
        scan.close()
    except Exception as exc:  # the stdlib parser asserts on some malformed declarations
        logger.warning("Withholding an element's markup whole: it does not parse (%s)", exc)
        return REDACTED_MARKUP
    return scan.rewritten()


def _withheld_text(raw: str) -> bool:
    """Whether *raw* markup text inside a sensitive control is replaced: not blank, not done."""
    text = html.unescape(raw).strip()
    return bool(text) and text != REDACTED


def _escape_attribute(value: str) -> str:
    """An attribute value as Chrome serializes it."""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("\xa0", "&nbsp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _start_tag_withheld(text: str, attrs: list[tuple[str, str | None]]) -> str:
    """The start tag *text*, written again with every ``value`` attribute withheld."""
    match = _TAG_NAME.match(text)
    parts = [f"<{match.group(1) if match else 'unknown'}"]
    for name, value in attrs:
        if name == "value" and value not in (None, "", REDACTED):
            value = REDACTED
        parts.append(f' {name}="{_escape_attribute(value or "")}"')
    parts.append("/>" if text.endswith("/>") else ">")
    return "".join(parts)


class _MarkupScan(HTMLParser):
    """Finds what :func:`withhold_sensitive_markup` replaces, as edits to the source.

    The stdlib parser reports where each token starts (``getpos``); a run of
    text ends where the next token starts. Each open element carries whether
    it lies inside a sensitive control, so the flag reaches every descendant
    the way the BrowserUse cleaner passes it down its tree.
    """

    def __init__(self, source: str, inside_sensitive: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._source = source
        self._line_starts = [0, *(found.end() for found in re.finditer("\n", source))]
        self._base = inside_sensitive
        #: Open elements: (tag, inside a sensitive control or one itself).
        self._open: list[tuple[str, bool]] = []
        #: Where the current run of text started, and whether a sensitive control holds it.
        self._text_start: int | None = None
        self._text_sensitive = False
        #: ``(start, end, replacement)`` in source order, never overlapping.
        self._edits: list[tuple[int, int, str]] = []

    def rewritten(self) -> str:
        self._end_text(len(self._source))
        parts: list[str] = []
        last = 0
        for start, end, replacement in self._edits:
            parts += (self._source[last:start], replacement)
            last = end
        parts.append(self._source[last:])
        return "".join(parts)

    def _here(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _sensitive(self) -> bool:
        return self._open[-1][1] if self._open else self._base

    def _end_text(self, end: int) -> None:
        start, self._text_start = self._text_start, None
        if start is not None and self._text_sensitive and _withheld_text(self._source[start:end]):
            self._edits.append((start, end, REDACTED_MARKUP))

    def handle_data(self, data: str) -> None:
        if self._text_start is None:
            self._text_start = self._here()
            self._text_sensitive = self._sensitive()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        start = self._here()
        self._end_text(start)
        text = self.get_starttag_text() or ""
        named: dict[str, str] = {}
        for name, value in attrs:  # the HTML parser keeps the first of a repeated attribute
            named.setdefault(name, value or "")
        sensitive = self._sensitive() or is_sensitive_control(tag, named)
        if sensitive and any(
            name == "value" and value not in (None, "", REDACTED) for name, value in attrs
        ):
            self._edits.append((start, start + len(text), _start_tag_withheld(text, attrs)))
        if not closed and tag not in _VOID_ELEMENTS:
            self._open.append((tag, sensitive))

    def handle_endtag(self, tag: str) -> None:
        self._end_text(self._here())
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth][0] == tag:
                del self._open[depth:]
                break

    def handle_comment(self, data: str) -> None:
        start = self._here()
        self._end_text(start)
        body = start + len("<!--")
        if (
            self._sensitive()
            and self._source.startswith("<!--", start)
            and self._source.startswith(data, body)
            and _withheld_text(data)
        ):
            # Comment data is serialized raw: the placeholder goes in unescaped.
            self._edits.append((body, body + len(data), REDACTED))

    def handle_decl(self, decl: str) -> None:
        self._end_text(self._here())

    def handle_pi(self, data: str) -> None:
        self._end_text(self._here())

    def unknown_decl(self, data: str) -> None:
        self._end_text(self._here())
