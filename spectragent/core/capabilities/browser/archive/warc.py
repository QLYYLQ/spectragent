"""The network archiver's WARC/1.1 writer (§4.14 ``browser.network``).

One ``exchanges.warc.gz`` per collection, written with warcio (a core
dependency, Apache-2.0), one gzip member per record:

- a ``request`` record per exchange — the request line, the stored
  (stripped) request headers with a ``Host`` header synthesised from the URL
  when the Fetch-visible set lacks one, and the redacted POST body;
- a ``response`` record when the response is fully known — a body read in
  full (``ok``), a redirect (no body), a WebSocket handshake (``101``). The
  HTTP header block is the raw one where Chrome reported it
  (``responseReceivedExtraInfo``, the only source of ``Set-Cookie``), with
  ``Content-Encoding`` / ``Transfer-Encoding`` dropped and ``Content-Length``
  set to the *decoded* body's length: the body is stored decoded (measured,
  arch §4.14 "回放实测": ``Fetch.fulfillRequest`` serves bytes verbatim, so replay needs them
  decoded), and the original values go into the index instead;
- a ``revisit`` record (profile ``identical-payload-digest``, pointing at the
  first response) instead, when a non-empty payload was already written
  earlier in the run — :class:`PayloadIndex` is run-scoped.

The request and response records are linked by ``WARC-Concurrent-To``. An
exchange whose body is not known (``too_large``, ``streamed_later``,
``incomplete``) gets only its request record: a response record with an
empty or partial payload would lie to anyone reading the WARC without the
index. Its status and headers are in the index line.
"""

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from warcio.statusandheaders import StatusAndHeaders
from warcio.timeutils import datetime_to_iso_date
from warcio.warcwriter import WARCWriter

#: Response headers that describe the wire encoding, not the stored body.
ENCODING_HEADERS = ("content-encoding", "transfer-encoding", "content-length")

IDENTICAL_PAYLOAD_PROFILE = "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest"


@dataclass(frozen=True)
class FirstPayload:
    """Where a payload was first written in this run (what a revisit points at)."""

    record_id: str
    uri: str
    date: str
    #: warcio's ``WARC-Payload-Digest`` of that record (``sha1:<base32>``).
    warc_digest: str


class PayloadIndex:
    """Payload sha256 → its first response record, for the whole run."""

    def __init__(self) -> None:
        self._first: dict[str, FirstPayload] = {}

    def get(self, sha256: str) -> FirstPayload | None:
        return self._first.get(sha256)

    def remember(self, sha256: str, first: FirstPayload) -> None:
        self._first.setdefault(sha256, first)

    def __len__(self) -> int:
        return len(self._first)


@dataclass(frozen=True)
class Written:
    """Where one exchange's records landed in the step's WARC."""

    request_record_id: str
    request_offset: int
    request_length: int
    response_record_id: str | None = None
    response_offset: int | None = None
    response_length: int | None = None
    #: The response record is a revisit of this record id.
    revisit_of: str | None = None


def warc_date(epoch_seconds: float) -> str:
    """WARC/1.1 ``WARC-Date`` (microseconds) of a Unix time."""
    moment = datetime.fromtimestamp(epoch_seconds, UTC).replace(tzinfo=None)
    return datetime_to_iso_date(moment, use_micros=True)


def header_lines(headers: Any) -> list[tuple[str, str]]:
    """CDP headers as ``(name, value)`` lines, a ``\\n``-joined value split into lines."""
    pairs: list[tuple[str, str]] = []
    if isinstance(headers, dict):
        items = [(str(k), str(v)) for k, v in headers.items()]
    elif isinstance(headers, list):
        items = [
            (str(item.get("name", "")), str(item.get("value", "")))
            for item in headers
            if isinstance(item, dict)
        ]
    else:
        items = []
    for name, value in items:
        for line in value.split("\n"):
            pairs.append((name, line))
    return pairs


def reason_phrase(status: int, text: str) -> str:
    if text:
        return text
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


class StepWarc:
    """The WARC of one collection, built in memory."""

    def __init__(self, payloads: PayloadIndex) -> None:
        self._payloads = payloads
        self._buffer = io.BytesIO()
        self._writer = WARCWriter(self._buffer, gzip=True, warc_version="1.1")
        self.records = 0

    def _write(self, record: Any) -> tuple[int, int]:
        start = self._buffer.tell()
        self._writer.write_record(record)
        self.records += 1
        return start, self._buffer.tell() - start

    def write_exchange(
        self,
        *,
        record_id: str,
        url: str,
        method: str,
        request_headers: Any,
        request_body: bytes | None,
        date: str,
        response: tuple[int, str, Any, bytes] | None,
        payload_sha256: str | None,
        remote_address: str | None = None,
    ) -> Written:
        """Write one exchange's request record and, if *response* is given, its response.

        Args:
            response: ``(status, status_text, headers, decoded_body)``, or
                ``None`` when the body is not known.
            payload_sha256: sha256 of the decoded body (revisit dedup key).
        """
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        lines = header_lines(request_headers)
        if not any(name.lower() == "host" for name, _ in lines) and parts.netloc:
            lines.insert(0, ("Host", parts.netloc))
        if request_body:
            lines = [(n, v) for n, v in lines if n.lower() != "content-length"]
            lines.append(("Content-Length", str(len(request_body))))
        request_http = StatusAndHeaders(f"{method} {target} HTTP/1.1", lines, is_http_request=True)

        response_id: str | None = None
        response_record: Any = None
        revisit_of: str | None = None
        if response is not None:
            status, status_text, headers, body = response
            kept = [
                (name, value)
                for name, value in header_lines(headers)
                if name.lower() not in ENCODING_HEADERS
            ]
            kept.append(("Content-Length", str(len(body))))
            response_http = StatusAndHeaders(
                f"{status} {reason_phrase(status, status_text)}".rstrip(),
                kept,
                protocol="HTTP/1.1",
            )
            first = self._payloads.get(payload_sha256) if body and payload_sha256 else None
            warc_headers: dict[str, str] = {"WARC-Date": date}
            if remote_address:
                warc_headers["WARC-IP-Address"] = remote_address.rsplit(":", 1)[0].strip("[]")
            if first is not None:
                response_record = self._writer.create_revisit_record(
                    url,
                    first.warc_digest,
                    first.uri,
                    first.date,
                    http_headers=response_http,
                    warc_headers_dict={**warc_headers, "WARC-Refers-To": f"<{first.record_id}>"},
                )
                revisit_of = first.record_id
            else:
                response_record = self._writer.create_warc_record(
                    url,
                    "response",
                    payload=io.BytesIO(body),
                    length=len(body),
                    http_headers=response_http,
                    warc_headers_dict=warc_headers,
                )
            response_id = str(response_record.rec_headers.get_header("WARC-Record-ID")).strip("<>")
            if first is None and body and payload_sha256:
                self._payloads.remember(
                    payload_sha256,
                    FirstPayload(
                        record_id=response_id,
                        uri=url,
                        date=date,
                        warc_digest=str(
                            response_record.rec_headers.get_header("WARC-Payload-Digest")
                        ),
                    ),
                )

        request_headers_dict: dict[str, str] = {
            "WARC-Record-ID": f"<{record_id}>",
            "WARC-Date": date,
        }
        if response_id is not None:
            request_headers_dict["WARC-Concurrent-To"] = f"<{response_id}>"
        body_bytes = request_body or b""
        request_record = self._writer.create_warc_record(
            url,
            "request",
            payload=io.BytesIO(body_bytes),
            length=len(body_bytes),
            http_headers=request_http,
            warc_headers_dict=request_headers_dict,
        )
        request_offset, request_length = self._write(request_record)
        if response_record is None:
            return Written(record_id, request_offset, request_length)
        response_offset, response_length = self._write(response_record)
        return Written(
            record_id,
            request_offset,
            request_length,
            response_id,
            response_offset,
            response_length,
            revisit_of,
        )

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()
