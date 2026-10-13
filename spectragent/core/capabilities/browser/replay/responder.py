"""What a replay sends back for one request — pure functions (§4.14 "按记录的形态应答").

The replay server (``server.py``) decides *which* record answers a paused
request (the matcher) and *whether* a miss is fulfilled or failed (the miss
policy); this module turns that into the one response Chrome gets through
``Fetch.fulfillRequest``. Nothing here sends anything or knows CDP beyond the
shape of the fulfil command (:meth:`Fulfilment.command_params`).

The shapes, one function each:

- **A recorded response** (:func:`serve_record`), as recorded — a ``3xx`` with
  its ``Location`` included: Chrome follows it and the next hop pauses on its
  own (Playwright's ``harBackend`` follows the chain inside its lookup and
  serves the final response; we serve the recorded hop, which is more
  faithful under Fetch interception). A ``304`` never answers an
  unconditional request.
- **A byte range** (:func:`serve_range`): ``206`` with ``Content-Range`` and
  ``Accept-Ranges`` cut from the archive's range map — never a ``200`` for a
  ``Range`` request (measured, arch §4.14: a 200 plays, but seeking breaks).
- **A CORS preflight the archive does not hold** (:func:`synthetic_preflight`):
  ``204`` echoing the request's ``Origin`` (never ``*``), method and headers,
  with ``Access-Control-Allow-Credentials: true`` and ``Vary: Origin``. A
  preflight is never answered 404 — the real request would never be sent.
- **A miss the policy fulfils** (:func:`miss_response`): the policy's status
  and headers, no body.

Every response goes through the same header hygiene (:func:`sanitised_headers`):
``Content-Encoding`` / ``Transfer-Encoding`` and HTTP/2 pseudo-headers dropped,
``Content-Length`` recomputed from the bytes served, a value spanning several
lines split into several entries (each ``Set-Cookie`` its own). Bodies are
stored decoded, and ``fulfillRequest`` sends bytes as they are, so this is
hygiene for what page JavaScript reads, not framing (measured, arch §4.14).

**Dates** (:func:`dated_headers`), ``shift_dates``:

- ``none`` — ``Date`` / ``Expires`` / ``Last-Modified`` and cookie expiry as
  recorded; pairs with ``base_time: from_archive``. Chrome rebases a cookie's
  expiry by the served ``Date`` (``Expires + (now − Date)``), so lifetimes stay
  right.
- ``all`` — all four shifted by one Δ = now − record time (the recorded
  ``Date``, else the request's start, the WARC-Date source); pairs with
  ``base_time: run_start``.
- Never a subset: shifting the cookie expiry alone double-shifts it (measured, arch §4.14).
  A response without ``Date`` gets one (record time + Δ) so Chrome's
  rebasing has its anchor. A value that does not parse as a date is served
  as recorded.

Follows (re-implemented, not copied) WPR ``proxy.go`` ``updateDates``
(webpagereplay @ 6a31bb09, BSD-3: shifts ``Last-Modified`` / ``Expires``
against the recorded ``Date``) and mitmproxy ``Response.refresh`` /
``refresh_set_cookie_header`` (@ 5253dcbd, MIT: the same Δ for cookie
expiry); see THIRD_PARTY_NOTICES.md. Modified: one switch for all four,
a synthesised ``Date``, an unparsable cookie expiry kept rather than dropped.

.. note::
   No ``from __future__ import annotations`` — the browser replay package
   rule.
"""

import base64
import email.utils
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from spectragent.core.capabilities.browser.replay.archive_index import IndexedRecord, RangeCover
from spectragent.core.capabilities.browser.replay.matcher import CONDITIONAL_HEADERS, LiveRequest
from spectragent.core.capabilities.browser.replay.miss_policy import MissDecision, MissReason
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule

ShiftDates = Literal["none", "all"]

#: Headers never served: the body is stored decoded and ``fulfillRequest``
#: sends it as it is, so these would lie to page JavaScript.
DROPPED_HEADERS = frozenset({"content-encoding", "transfer-encoding", "content-length"})
#: The headers ``shift_dates: all`` moves (cookie expiry is ``Set-Cookie``'s attribute).
DATE_HEADERS = frozenset({"date", "expires", "last-modified"})
#: Statuses that carry no body, hence no ``Content-Length``.
BODILESS_STATUSES = frozenset({204, 304})
#: ``; Expires=<date>`` inside one ``Set-Cookie`` line.
_COOKIE_EXPIRES = re.compile(r"(;\s*expires\s*=\s*)([^;]*)", re.IGNORECASE)


@dataclass(frozen=True)
class Fulfilment:
    """One response for ``Fetch.fulfillRequest``.

    Attributes:
        status: The status code.
        headers: The response headers, in order, one entry per header line.
        body: The bytes served (decoded).
        status_text: The reason phrase (``""``: Chrome's standard one).
    """

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""
    status_text: str = ""

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    def command_params(self, request_id: str) -> dict[str, Any]:
        """The ``Fetch.fulfillRequest`` parameters answering *request_id*."""
        params: dict[str, Any] = {
            "requestId": request_id,
            "responseCode": self.status,
            "responseHeaders": [{"name": name, "value": value} for name, value in self.headers],
            "body": base64.b64encode(self.body).decode("ascii"),
        }
        if self.status_text and self.status_text.isascii():
            params["responsePhrase"] = self.status_text
        return params


@dataclass(frozen=True)
class Refusal:
    """A matched record that cannot be served after all; the miss policy answers instead."""

    reason: MissReason


# ── predicates ─────────────────────────────────────────────────────────


def is_preflight(request: LiveRequest) -> bool:
    """A CORS preflight: ``OPTIONS`` with ``Access-Control-Request-Method``.

    By method and header, never by ``resourceType`` (Chrome reports ``XHR``).
    """
    return (
        request.method.upper() == "OPTIONS"
        and request.header("access-control-request-method") is not None
    )


def is_conditional(request: LiveRequest) -> bool:
    """Whether a ``304`` may answer *request* (``If-None-Match`` and the like)."""
    return any(request.header(name) is not None for name in CONDITIONAL_HEADERS)


def exceeds_cap(length: int | None, cap: int) -> bool:
    """Whether a body of *length* bytes is over the fulfil cap (unknown length: not known to be)."""
    return length is not None and length > cap


# ── the shapes ─────────────────────────────────────────────────────────


def serve_record(
    record: IndexedRecord,
    body: bytes,
    *,
    request: LiveRequest,
    raw_body: bytes | None,
    rule: SiteRule | None,
    shift_dates: ShiftDates,
    now: float,
    cap: int,
) -> Fulfilment | Refusal:
    """The recorded response of *record*, with *body*, for *request*.

    *raw_body* is the live request's body as the page sent it (the site
    rule's ``response_echo`` copies values from it); *now* is wall-clock
    seconds; *cap* the fulfil cap.
    """
    if record.status is None:
        return Refusal("body_unavailable")
    if record.status == 304 and not is_conditional(request):
        return Refusal("no_match")
    if rule is not None and raw_body:
        body = rule.echo(request.url, raw_body, request.content_type, body)
    if len(body) > cap:
        return Refusal("too_large")
    headers = dated_headers(
        record.response_headers,
        shift_dates=shift_dates,
        now=now,
        record_time=record.started_at,
    )
    return Fulfilment(
        status=record.status,
        headers=sanitised_headers(headers, status=record.status, body_length=len(body)),
        body=body,
        status_text=record.status_text,
    )


def serve_range(
    cover: RangeCover,
    data: bytes,
    *,
    shift_dates: ShiftDates,
    now: float,
) -> Fulfilment:
    """``206`` for the bytes of *cover* (already clipped to the cap), *data* being those bytes.

    The head is the first covering record's, without its own range and
    length headers; ``Content-Range`` says which bytes these are of how many
    (``*`` when the total was never recorded).
    """
    record = cover.parts[0][0]
    total = "*" if cover.total is None else str(cover.total)
    base = [
        (name, value)
        for name, value in record.response_headers
        if name.lower() not in ("content-range", "accept-ranges")
    ]
    headers = dated_headers(base, shift_dates=shift_dates, now=now, record_time=record.started_at)
    headers.extend(
        [
            ("Content-Range", f"bytes {cover.start}-{cover.start + len(data) - 1}/{total}"),
            ("Accept-Ranges", "bytes"),
        ]
    )
    return Fulfilment(
        status=206,
        headers=sanitised_headers(headers, status=206, body_length=len(data)),
        body=data,
        status_text="Partial Content",
    )


def synthetic_preflight(request: LiveRequest) -> Fulfilment:
    """``204`` allowing exactly what the preflight asks for, to the origin that asks.

    ``Access-Control-Allow-Origin`` is the request's ``Origin`` — never ``*``,
    which a credentialed request may not use — and credentials are allowed:
    a preflight never says whether the real request carries them.
    """
    headers: list[tuple[str, str]] = []
    origin = request.header("origin")
    if origin is not None:
        headers.append(("Access-Control-Allow-Origin", origin))
    headers.append(("Access-Control-Allow-Credentials", "true"))
    method = request.header("access-control-request-method")
    if method is not None:
        headers.append(("Access-Control-Allow-Methods", method))
    asked = request.header("access-control-request-headers")
    if asked is not None:
        headers.append(("Access-Control-Allow-Headers", asked))
    headers.append(("Vary", "Origin"))
    return Fulfilment(status=204, headers=tuple(headers), body=b"", status_text="No Content")


def miss_response(decision: MissDecision) -> Fulfilment:
    """The empty response a miss policy decided to fulfil a miss with."""
    if decision.answer != "fulfill" or decision.status is None:
        raise ValueError(f"a {decision.answer!r} decision has no response: {decision}")
    return Fulfilment(
        status=decision.status,
        headers=sanitised_headers(decision.headers, status=decision.status, body_length=0),
        body=b"",
    )


# ── headers ───────────────────────────────────────────────────────────


def sanitised_headers(
    headers: Iterable[tuple[str, str]], *, status: int, body_length: int
) -> tuple[tuple[str, str], ...]:
    """Headers page JavaScript can trust for the bytes actually served.

    Drops ``Content-Encoding``, ``Transfer-Encoding``, ``Content-Length`` and
    HTTP/2 pseudo-headers (``:status`` …), splits a value spanning several
    lines into one entry per line (each ``Set-Cookie`` separate), and adds
    ``Content-Length`` for the body served — none for ``204`` / ``304``.
    """
    out: list[tuple[str, str]] = []
    for name, value in headers:
        if not name or name.startswith(":") or name.lower() in DROPPED_HEADERS:
            continue
        for line in str(value).split("\n"):
            out.append((name, line.rstrip("\r")))
    if status not in BODILESS_STATUSES and not 100 <= status < 200:
        out.append(("Content-Length", str(body_length)))
    return tuple(out)


def dated_headers(
    headers: Sequence[tuple[str, str]],
    *,
    shift_dates: ShiftDates,
    now: float,
    record_time: float | None,
) -> list[tuple[str, str]]:
    """*headers* with their dates as ``shift_dates`` says, and a ``Date`` when they have none.

    Δ = *now* − the recorded ``Date`` (else *record_time*, the request's
    start) under ``all``, 0 under ``none``; ``Date``, ``Expires``,
    ``Last-Modified`` and every ``Set-Cookie`` ``Expires=`` move together.
    """
    recorded = next(
        (
            parsed
            for name, value in headers
            if name.lower() == "date" and (parsed := parse_http_date(value)) is not None
        ),
        None,
    )
    anchor = recorded if recorded is not None else record_time
    delta = now - anchor if shift_dates == "all" and anchor is not None else 0.0
    out: list[tuple[str, str]] = []
    for name, value in headers:
        lowered = name.lower()
        if delta and lowered in DATE_HEADERS:
            value = shifted_http_date(value, delta)
        elif delta and lowered == "set-cookie":
            value = shifted_cookie_expiry(value, delta)
        out.append((name, value))
    if recorded is None and anchor is not None:
        out = [(name, value) for name, value in out if name.lower() != "date"]
        out.append(("Date", format_http_date(anchor + delta)))
    return out


def parse_http_date(value: str) -> float | None:
    """Seconds since the epoch of an HTTP or cookie date, or ``None``."""
    parsed = email.utils.parsedate_tz(value.strip())
    if parsed is None:
        return None
    try:
        return float(email.utils.mktime_tz(parsed))
    except (OverflowError, ValueError):
        return None


def format_http_date(seconds: float) -> str:
    """``Wed, 21 Oct 2015 07:28:00 GMT``."""
    return email.utils.formatdate(seconds, usegmt=True)


def shifted_http_date(value: str, delta: float) -> str:
    """*value* moved by *delta* seconds; a value that is not a date is kept as recorded."""
    parsed = parse_http_date(value)
    if parsed is None:
        return value
    try:
        return format_http_date(parsed + delta)
    except (OverflowError, ValueError, OSError):
        return value


def shifted_cookie_expiry(value: str, delta: float) -> str:
    """One ``Set-Cookie`` line with its ``Expires=`` attribute moved by *delta* seconds."""

    def _shift(match: "re.Match[str]") -> str:
        return match.group(1) + shifted_http_date(match.group(2), delta)

    return _COOKIE_EXPIRES.sub(_shift, value)
