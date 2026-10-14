"""JSONP callback names in a served response (§4.14 "JSONP 回调名").

A JSONP endpoint answers ``…?callback=NAME`` with a script that calls
``NAME`` with the data. The page invents ``NAME`` — jQuery as
``jQuery<random digits>_<request counter>``, other scripts with a random
suffix — so a replay asks for another name than the recording did whenever
its scripts drew their random numbers or counted their requests in another
order. The recorded script then calls a function the page never defined: the
request is a hit and the data never arrives. When the archive serves such a
response, :func:`rewrite_callback` writes this request's name where the
recorded one stood, and only where that is provable:

0. **A script the page runs or reads** (:func:`may_be_script`). The request
   is a ``Script``, ``XHR`` or ``Fetch`` (a script element, or the text a
   library evaluates — jQuery fetches a same-origin JSONP that way), never a
   document, image, stylesheet, font or media request; and the recorded
   response is served as JavaScript, JSON, plain text, HTML or
   ``application/octet-stream``, or with no type — every type the live sites
   answered JSONP with. An image whose first bytes happen to read
   ``GIF89a(`` is not a call.
1. **The wrapper** (:func:`leading_call`). The body opens with a call of one
   name, in one of these shapes, ``NAME`` being the same name in each place:

   - ``NAME(``;
   - ``typeof NAME === 'function' && NAME(`` (also ``==`` and double
     quotes) — what Express's ``res.jsonp`` writes;
   - ``NAME && NAME(``;
   - any of them inside ``try {`` (``try{NAME(…)}catch(e){}``).

   Before the call, and between its tokens, only whitespace, comments
   (``/* … */``, ``// …``), ``;`` before the first token and a UTF-8 byte
   order mark at the very start. ``NAME`` is a JavaScript identifier of
   ASCII letters, digits, ``_`` and ``$`` not starting with a digit, or
   several joined by dots (``angular.callbacks._0``). Whatever follows the
   opening parenthesis is never read.
2. **The parameter** (:func:`callback_parameter`). Exactly one query
   parameter of the recorded request has ``NAME`` as its value: the
   parameter the server echoed. A name under two parameters, or a parameter
   given twice, is not provably the one.
3. **This request's name.** This request carries that parameter exactly
   once, its value is a name by the same rule — nothing else is ever written
   into a script — and it differs from ``NAME``.

Anything else is served as recorded, byte for byte: a body that is JSON,
HTML or anything that does not open with such a call; a call of a fixed name
the server writes whatever the request asks (``LETV.Plugin.AD.resoJsonp(``,
an endpoint that always calls ``callback(``); a callback carried in the path
or the body. The name is ASCII and the rest of the body is copied unchanged,
so the rewrite needs no charset: a GBK body stays GBK. The responder
(``responder.py``) serves the new bytes with their own ``Content-Length`` and
without the recorded body digests.

.. note::
   No ``from __future__ import annotations`` — the browser replay package
   rule.
"""

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field

#: A UTF-8 byte order mark: allowed once, at the very start of the body.
_BOM = b"\xef\xbb\xbf"
_WHITESPACE = frozenset(b" \t\n\r\f\v")
_LINE_ENDS = frozenset(b"\n\r")
_NAME_START = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$")
_NAME_PART = _NAME_START | frozenset(b"0123456789")
_DOT = ord(".")
_SEMICOLON = ord(";")
#: The guard's comparison operators, longest first.
_EQUALS = (b"===", b"==")
_FUNCTION = (b"'function'", b'"function"')

#: The resource types a JSONP answer arrives as: a script element's, or an XHR
#: or fetch whose text a library evaluates.
SCRIPT_RESOURCE_TYPES = frozenset({"Script", "XHR", "Fetch"})
#: Media types a JSONP answer is served as besides the JavaScript and JSON ones
#: (:func:`may_be_script`): the live sites used ``text/plain``, ``text/html`` and
#: ``application/octet-stream`` too; ``text/jscript`` and ``text/livescript`` are
#: JavaScript MIME types whose names say so less plainly.
_SCRIPT_TEXT_TYPES = frozenset(
    {"text/plain", "text/html", "application/octet-stream", "text/jscript", "text/livescript"}
)
#: A media type naming one of these is a JavaScript or JSON type
#: (``application/x-javascript``, ``text/ecmascript``, ``application/ld+json`` …).
_SCRIPT_TYPE_WORDS = ("javascript", "ecmascript", "json")


class CallbackRewrite(BaseModel):
    """One served body whose JSONP callback name was replaced: the replay log's ``callback_rewrite``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter: str = Field(description="The query parameter that carries the callback name.")
    recorded: str = Field(
        description="The name the recorded body called, from the recorded request."
    )
    requested: str = Field(description="The name this request asked for, written in its place.")


@dataclass(frozen=True)
class LeadingCall:
    """The call a body opens with.

    Attributes:
        name: The called name.
        spans: Where the name stands in the body, as ``(start, end)`` byte
            offsets in order — the guard's occurrences, then the call's.
    """

    name: str
    spans: tuple[tuple[int, int], ...]


def may_be_script(resource_type: str | None, content_type: str | None) -> bool:
    """Whether a response to a *resource_type* request served as *content_type* may be a script.

    The request is a ``Script``, ``XHR`` or ``Fetch``; the response's media
    type is absent, a JavaScript or JSON type, ``text/plain``, ``text/html``
    or ``application/octet-stream`` (module docstring, condition 0).
    """
    if resource_type not in SCRIPT_RESOURCE_TYPES:
        return False
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return (
        not media
        or media in _SCRIPT_TEXT_TYPES
        or any(word in media for word in _SCRIPT_TYPE_WORDS)
    )


def leading_call(body: bytes) -> LeadingCall | None:
    """The call *body* opens with, in one of the wrapper shapes (module docstring), or ``None``."""
    at = _skip_prefix(body, len(_BOM) if body.startswith(_BOM) else 0)
    if at is None:
        return None
    if _keyword_at(body, at, b"try"):
        brace = _skip_fill(body, at + 3)
        if brace is not None and body[brace : brace + 1] == b"{":
            inside = _skip_fill(body, brace + 1)
            if inside is None:
                return None
            at = inside
    if _keyword_at(body, at, b"typeof"):
        return _typeof_guarded(body, at)
    end = _name_end(body, at)
    if end == at:
        return None
    after = _skip_fill(body, end)
    if after is None:
        return None
    if body[after : after + 1] == b"(":
        return LeadingCall(body[at:end].decode("ascii"), ((at, end),))
    if body.startswith(b"&&", after):
        call = _call_of(body, _skip_fill(body, after + 2), body[at:end])
        if call is not None:
            return LeadingCall(body[at:end].decode("ascii"), ((at, end), call))
    return None


def callback_parameter(url: str, name: str) -> str | None:
    """The one query parameter of *url* whose value is *name*, or ``None``.

    ``None`` too when *name* is the value of two parameters, or of one
    parameter given twice: then which one the server echoed is not known.
    """
    carrying = [key for key, value in _query(url) if value == name]
    return carrying[0] if len(carrying) == 1 else None


def is_callback_name(value: str) -> bool:
    """Whether *value* is a name the rewrite may write: ASCII identifiers, joined by dots."""
    try:
        data = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(data) and _name_end(data, 0) == len(data)


def rewrite_callback(
    body: bytes,
    *,
    recorded_url: str,
    request_url: str,
    resource_type: str | None,
    content_type: str | None,
) -> tuple[bytes, CallbackRewrite] | None:
    """*body* calling the name *request_url* asks for, when it provably calls *recorded_url*'s.

    *recorded_url* is the recorded request's URL, *body* its response's body
    as served (decoded) and *content_type* that response's ``Content-Type``;
    *request_url* and *resource_type* describe the request being answered.
    Returns the new body and what changed, or ``None`` when the body is
    served as recorded: not a script (:func:`may_be_script`), no wrapper, no
    parameter carrying the called name, this request without that parameter
    (or with it twice, or with a value that is not a name), or the same name
    (module docstring).
    """
    if not may_be_script(resource_type, content_type):
        return None
    call = leading_call(body)
    if call is None:
        return None
    parameter = callback_parameter(recorded_url, call.name)
    if parameter is None:
        return None
    asked = [value for key, value in _query(request_url) if key == parameter]
    if len(asked) != 1:
        return None
    requested = asked[0]
    if requested == call.name or not is_callback_name(requested):
        return None
    replacement = requested.encode("ascii")
    pieces: list[bytes] = []
    last = 0
    for start, end in call.spans:
        pieces.append(body[last:start])
        pieces.append(replacement)
        last = end
    pieces.append(body[last:])
    return b"".join(pieces), CallbackRewrite(
        parameter=parameter, recorded=call.name, requested=requested
    )


# ── the scanner ────────────────────────────────────────────────────────


def _query(url: str) -> list[tuple[str, str]]:
    try:
        query = urlsplit(url.split("#", 1)[0]).query
    except ValueError:
        return []
    return parse_qsl(query, keep_blank_values=True)


def _skip_fill(body: bytes, at: int | None) -> int | None:
    """The first offset from *at* past whitespace and comments; ``None`` inside an unclosed comment."""
    if at is None:
        return None
    size = len(body)
    while at < size:
        if body[at] in _WHITESPACE:
            at += 1
        elif body.startswith(b"/*", at):
            close = body.find(b"*/", at + 2)
            if close < 0:
                return None
            at = close + 2
        elif body.startswith(b"//", at):
            at += 2
            while at < size and body[at] not in _LINE_ENDS:
                at += 1
        else:
            break
    return at


def _skip_prefix(body: bytes, at: int) -> int | None:
    """Past the fill and any ``;`` before the first token."""
    position = _skip_fill(body, at)
    while position is not None and position < len(body) and body[position] == _SEMICOLON:
        position = _skip_fill(body, position + 1)
    return position


def _name_end(body: bytes, at: int | None) -> int:
    """The end of the dotted identifier starting at *at* (*at* itself when there is none)."""
    if at is None:
        return -1
    size = len(body)
    if at >= size or body[at] not in _NAME_START:
        return at
    end = at + 1
    while end < size and body[end] in _NAME_PART:
        end += 1
    while end + 1 < size and body[end] == _DOT and body[end + 1] in _NAME_START:
        end += 2
        while end < size and body[end] in _NAME_PART:
            end += 1
    return end


def _keyword_at(body: bytes, at: int, word: bytes) -> bool:
    """*word* at *at*, as a whole word (``typeofx`` is a name, not ``typeof``)."""
    end = at + len(word)
    return body.startswith(word, at) and (end >= len(body) or body[end] not in _NAME_PART)


def _call_of(body: bytes, at: int | None, name: bytes) -> tuple[int, int] | None:
    """The span of *name* at *at* when a call of it starts there (``name (``), else ``None``."""
    if at is None:
        return None
    end = _name_end(body, at)
    if body[at:end] != name:
        return None
    after = _skip_fill(body, end)
    if after is None or body[after : after + 1] != b"(":
        return None
    return at, end


def _typeof_guarded(body: bytes, at: int) -> LeadingCall | None:
    """``typeof NAME === 'function' && NAME(`` starting at *at*."""
    start = _skip_fill(body, at + len(b"typeof"))
    end = _name_end(body, start)
    if start is None or end == start:
        return None
    name = body[start:end]
    position = _skip_fill(body, end)
    if position is None:
        return None
    operator = next((op for op in _EQUALS if body.startswith(op, position)), None)
    if operator is None:
        return None
    position = _skip_fill(body, position + len(operator))
    if position is None:
        return None
    literal = next((word for word in _FUNCTION if body.startswith(word, position)), None)
    if literal is None:
        return None
    position = _skip_fill(body, position + len(literal))
    if position is None or not body.startswith(b"&&", position):
        return None
    call = _call_of(body, _skip_fill(body, position + 2), name)
    if call is None:
        return None
    return LeadingCall(name.decode("ascii"), ((start, end), call))
