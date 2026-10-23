# Portions of this module — the request-body canonicalisation (`_body_params`,
# `_flatten_json`, the `__wb_method` / `__wb_post_data` parameters) — are derived
# from cdxj-indexer's cdxj_indexer/postquery.py
# (https://github.com/webrecorder/cdxj-indexer, commit
# a65917b12fbef23e58ee3e66878b852923811fb6, by Ilya Kreymer):
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
# Modified by af (2026-10-11): re-implemented as a pure key function. Kept: the
# `__wb_method=<METHOD>` parameter; urlencoded bodies read as their decoded
# parameters; multipart bodies read as their parts' names and values, never the
# boundary; JSON flattened into parameters, newline-delimited JSON line by line;
# text/plain tried as JSON first; anything else as one `__wb_post_data` value.
# Changed: every method with a body is keyed (upstream: POST and PUT only); the
# parameters are sorted here, merged with the query (upstream leaves sorting to
# SURT, which is AGPL and not used); JSON keys are full paths (`a.b.0.c`;
# upstream: the leaf name with `.N_` suffixes for duplicates), `+json` media
# types and form-shaped untyped bodies included, empty containers kept; a
# multipart file part is its sha256 (upstream: its raw bytes) and multipart is
# parsed without the `multipart` package; an unparsable body is its sha256
# (upstream: its base64); the 4096-character cap replaces the body by its sha256
# (upstream: truncates it, so long GraphQL requests collided); AMF dropped;
# GraphQL operations keyed by operation; site rules' ignored paths removed and
# the fields they declare as holding fields parsed into sub-fields.
"""One canonicaliser for recorded and live requests (§4.14 "匹配").

Replay keys a recorded request when it loads the archive and a live request
when Chrome pauses it, with **these functions and no others**, so the two
sides can only disagree when the requests do. The archive stores raw request
facts and no key (M29): a rule change here recomputes every key at the next
load and leaves nothing stale.

**Bodies arrive redacted.** A recorded POST body is what M29 stored — the
output of ``archive/redaction.redact_post_body``. A live body must go through
the same function *before* it reaches :func:`request_keys`, so a password or
token field is ``<redacted>`` on both sides and the request still matches
exactly. This module never redacts: it would be a second implementation of
the rule.

:func:`request_keys` gives three keys, one per matcher level:

- **exact** (L0) — ``(METHOD, URL without fragment, sha256 of the body)``;
  a multipart body is hashed with its boundary removed (Playwright
  ``harBackend``'s exact match), since the boundary is random per request.
- **normalized** (L1) — the canonical URL with every parameter, sorted:
  scheme and host lower-cased, default port and fragment dropped, the query
  decoded, its ``parse_query`` fields replaced by their sub-fields
  (:func:`parsed_subfields`) and stripped only of the applicable rule's
  ``ignore_query``, plus ``__wb_method=<METHOD>`` and the body's parameters
  (:func:`body_params`).
- **body_key** — the body's parameters on their own (``None`` without a
  body), for the replay log.

``host``, ``path`` and ``params`` (query and body parameters, body names
prefixed ``body:``) are what the fuzzy level scores.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from spectragent.core.capabilities.browser.replay.site_rules import FieldFormat, SiteRule

#: Ports a canonical URL leaves out.
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
#: A body's canonical parameter string longer than this is replaced by its sha256.
MAX_BODY_KEY_CHARS = 4096
METHOD_PARAM = "__wb_method"
POST_DATA_PARAM = "__wb_post_data"
BODY_SHA256_PARAM = "__wb_body_sha256"
#: Prefix of a body parameter's name in :attr:`RequestKeys.params`.
BODY_PARAM_PREFIX = "body:"
#: The URL parameters a GraphQL GET request carries its operation in.
GRAPHQL_URL_PARAMS = frozenset({"query", "variables", "operationName", "extensions"})

Params = tuple[tuple[str, str], ...]

# ── bodies ──────────────────────────────────────────────────────────────
#: ``name="…"`` in a multipart part's Content-Disposition (not ``filename=``).
_DISPOSITION_NAME = re.compile(r'(?:^|;)\s*name="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(r";\s*filename\*?=", re.IGNORECASE)
#: A body that is nothing but ``k=v`` pairs joined by ``&`` (an untyped beacon).
_FORM_SHAPE = re.compile(rb"^[^=&\s]+=[^&\s]*(?:&[^=&\s]+=[^&\s]*)*$")
#: The start of a GraphQL document.
_GRAPHQL_DOCUMENT = re.compile(r"^\s*(?:\{|query\b|mutation\b|subscription\b|fragment\b)")


@dataclass(frozen=True)
class RequestKeys:
    """Every key one request has, computed once.

    Attributes:
        exact: ``(METHOD, URL without fragment, body sha256 or None)``.
        normalized: The canonical URL with the method and body parameters.
        body_key: The body's canonical parameters (``None`` without a body).
        host: The lower-cased host, without port.
        port: The effective port, including the scheme's default when omitted.
        path: The URL path (``/`` when empty).
        params: Query and body parameters (body names prefixed ``body:``),
            sorted — what the fuzzy level scores.
    """

    exact: tuple[str, str, str | None]
    normalized: str
    body_key: str | None
    host: str
    path: str
    params: Params
    port: int | None = None


def request_keys(
    method: str,
    url: str,
    body: bytes | None,
    content_type: str | None,
    rule: SiteRule | None = None,
) -> RequestKeys:
    """The keys of one request — recorded or live, the body already redacted.

    Args:
        method: The request method (any case).
        url: The request URL; a fragment is ignored.
        body: The (redacted) request body; ``b""`` is no body.
        content_type: The request's ``Content-Type``.
        rule: The selected request rule, if any.
    """
    verb = method.upper()
    bare = url.split("#", 1)[0]
    payload = body or None
    try:
        parts = urlsplit(bare)
        host = (parts.hostname or "").lower()
    except ValueError:  # a malformed authority ("http://[::1/x"): the URL is its own key
        return _opaque_keys(verb, bare, payload, content_type, rule)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    path = parts.path or "/"
    query = canonical_query(parts.query, rule)
    body_pairs = body_params(payload, content_type, rule) if payload is not None else ()
    authority = f"[{host}]" if ":" in host else host
    if port is not None and DEFAULT_PORTS.get(scheme) != port:
        authority = f"{authority}:{port}"
    merged = tuple(sorted((*query, (METHOD_PARAM, verb), *body_pairs)))
    return RequestKeys(
        exact=(verb, bare, exact_body_digest(payload, content_type)),
        normalized=f"{scheme}://{authority}{path}?{encode_params(merged)}",
        body_key=encode_params(body_pairs) if payload is not None else None,
        host=host,
        port=port if port is not None else DEFAULT_PORTS.get(scheme),
        path=path,
        params=tuple(
            sorted((*query, *((BODY_PARAM_PREFIX + name, value) for name, value in body_pairs)))
        ),
    )


def _opaque_keys(
    verb: str, bare: str, payload: bytes | None, content_type: str | None, rule: SiteRule | None
) -> RequestKeys:
    """Keys of a URL ``urlsplit`` refuses: compared as a whole string, never fuzzily."""
    body_pairs = body_params(payload, content_type, rule) if payload is not None else ()
    merged = tuple(sorted(((METHOD_PARAM, verb), *body_pairs)))
    return RequestKeys(
        exact=(verb, bare, exact_body_digest(payload, content_type)),
        normalized=f"{bare}?{encode_params(merged)}",
        body_key=encode_params(body_pairs) if payload is not None else None,
        host="",
        path=bare,
        params=(),
    )


def host_of(url: str) -> str:
    """The lower-cased host of *url* (no port, no brackets) — without interpreting any request rule."""
    try:
        return (urlsplit(url.split("#", 1)[0]).hostname or "").lower()
    except ValueError:
        return ""


def encode_params(params: Sequence[tuple[str, str]]) -> str:
    """``a=1&b=x%20y`` — every name and value percent-encoded, in the given order."""
    return "&".join(f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in params)


def decode_params(encoded: str) -> Params:
    """The parameters :func:`encode_params` encoded, in order (``""`` is none).

    Exact inverse: ``encode_params`` escapes every ``&`` and ``=``, so the
    split is unambiguous, and an empty value stays an empty value.
    """
    if not encoded:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in encoded.split("&"):
        name, _, value = item.partition("=")
        pairs.append((unquote(name), unquote(value)))
    return tuple(pairs)


def exact_body_digest(body: bytes | None, content_type: str | None) -> str | None:
    """sha256 of *body* for the exact level; a multipart boundary is removed first."""
    if not body:
        return None
    media, options = media_type(content_type)
    boundary = options.get("boundary")
    if media.startswith("multipart/") and boundary:
        body = body.replace(boundary.encode("utf-8"), b"")
    return hashlib.sha256(body).hexdigest()


def media_type(content_type: str | None) -> tuple[str, dict[str, str]]:
    """``"multipart/form-data; boundary=x"`` → ``("multipart/form-data", {"boundary": "x"})``."""
    if not content_type:
        return "", {}
    head, *options = content_type.split(";")
    parsed: dict[str, str] = {}
    for option in options:
        key, eq, value = option.strip().partition("=")
        if eq:
            parsed[key.strip().lower()] = value.strip().strip('"')
    return head.strip().lower(), parsed


# ── the query ──────────────────────────────────────────────────────────


def canonical_query(query: str, rule: SiteRule | None = None) -> Params:
    """A query string's canonical parameters, sorted.

    Decoded (``%20`` and ``+`` are the same space); a parameter the rule's
    ``parse_query`` declares is replaced by its sub-fields
    (:func:`parsed_subfields`); then only the explicit rule's
    ``ignore_query`` is removed. A GraphQL GET request's ``query`` /
    ``variables`` / ``operationName`` / ``extensions`` become its operation's
    parameters (:func:`graphql_params`).
    """
    pairs = parse_qsl(query, keep_blank_values=True)
    operation = _graphql_get_operation(pairs)
    kept: list[tuple[str, str]] = []
    for name, value in pairs:
        if operation is not None and name in GRAPHQL_URL_PARAMS:
            continue
        if rule is None:
            kept.append((name, value))
            continue
        kept.extend(
            (field, part)
            for field, part in _parsed(name, value, rule.query_format(name))
            if not rule.ignores_query(field)
        )
    if operation is not None:
        kept.extend(graphql_params([operation], rule))
    return tuple(sorted(kept))


def _graphql_get_operation(pairs: Sequence[tuple[str, str]]) -> dict[str, Any] | None:
    """The GraphQL operation a GET request's parameters carry, if they carry one."""
    values = {name: value for name, value in pairs if name in GRAPHQL_URL_PARAMS}
    operation: dict[str, Any] = {}
    for name, value in values.items():
        if name in ("variables", "extensions"):
            try:
                operation[name] = json.loads(value)
            except (ValueError, RecursionError):
                operation[name] = value
        else:
            operation[name] = value
    return operation if _is_graphql_operation(operation) else None


# ── the body ────────────────────────────────────────────────────────────


def body_params(body: bytes, content_type: str | None, rule: SiteRule | None = None) -> Params:
    """A (redacted) body's canonical parameters, sorted — or its sha256 past the cap.

    By media type: urlencoded → its parameters; multipart → each part's name
    and value (a file part, or a part that is not UTF-8, as ``sha256:<hex>``);
    JSON and ``*+json`` → flattened by full path (a GraphQL body → its
    operations' parameters); ``text/plain`` or no type → JSON if it parses,
    else urlencoded if it is form-shaped; anything else, or a body that does
    not parse as its type → ``__wb_post_data=<sha256>``. A field the site
    rule's ``parse_body_paths`` declares is replaced by its sub-fields
    (:func:`parsed_subfields`), then the rule's ``ignore_body_paths`` are
    removed; both happen before the cap. When the encoded parameters exceed
    :data:`MAX_BODY_KEY_CHARS`, the body is ``__wb_body_sha256=<sha256 of them>``.
    """
    media, options = media_type(content_type)
    pairs = _body_pairs(body, media, options, rule)
    if pairs is None:
        pairs = [(POST_DATA_PARAM, hashlib.sha256(body).hexdigest())]
    if rule is not None:
        pairs = [(name, value) for name, value in pairs if not rule.ignores_body_path(name)]
    ordered = tuple(sorted(pairs))
    encoded = encode_params(ordered)
    if len(encoded) > MAX_BODY_KEY_CHARS:
        return ((BODY_SHA256_PARAM, hashlib.sha256(encoded.encode("utf-8")).hexdigest()),)
    return ordered


def _body_pairs(
    body: bytes, media: str, options: dict[str, str], rule: SiteRule | None
) -> list[tuple[str, str]] | None:
    """The body's parameters by its media type; ``None`` when it does not parse as one."""
    if media == "application/x-www-form-urlencoded":
        return _parsed_body(_form_pairs(body), rule)
    if media.startswith("multipart/"):
        boundary = options.get("boundary")
        return _parsed_body(_multipart_pairs(body, boundary), rule) if boundary else None
    if media == "application/json" or media.endswith("+json"):
        return _json_pairs(body, rule)
    if media in ("", "text/plain"):
        found = _json_pairs(body, rule)
        if found is not None:
            return found
        if _FORM_SHAPE.match(body):
            return _parsed_body(_form_pairs(body), rule)
    return None


def _form_pairs(body: bytes) -> list[tuple[str, str]] | None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return parse_qsl(text, keep_blank_values=True)


def _multipart_pairs(body: bytes, boundary: str) -> list[tuple[str, str]] | None:
    """Each part's name and value; the boundary never reaches the key."""
    delimiter = b"--" + boundary.encode("utf-8")
    pieces = body.split(delimiter)
    if len(pieces) < 2:
        return None
    pairs: list[tuple[str, str]] = []
    for piece in pieces[1:]:
        if piece.startswith(b"--"):  # the close delimiter and the epilogue
            break
        head, separator, rest = piece.partition(b"\r\n\r\n")
        if not separator:
            return None
        value = rest[:-2] if rest.endswith(b"\r\n") else rest
        headers = head.decode("latin-1")
        match = _DISPOSITION_NAME.search(headers)
        name = match.group(1) if match is not None else ""
        if _DISPOSITION_FILENAME.search(headers):
            pairs.append((name, _digest(value)))
            continue
        try:
            pairs.append((name, value.decode("utf-8")))
        except UnicodeDecodeError:
            pairs.append((name, _digest(value)))
    return pairs


def _json_pairs(body: bytes, rule: SiteRule | None) -> list[tuple[str, str]] | None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        # Newline-delimited JSON: each line is one document, like an array.
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        try:
            document = [json.loads(line) for line in lines]
        except (ValueError, RecursionError):
            return None
    operations = _graphql_operations(document)
    if operations is not None:
        return graphql_params(operations, rule)
    pairs: list[tuple[str, str]] = []
    _flatten_json(document, "", pairs)
    return _parsed_body(pairs, rule)


def _parsed_body(
    pairs: list[tuple[str, str]] | None, rule: SiteRule | None
) -> list[tuple[str, str]] | None:
    """*pairs* with each field the rule's ``parse_body_paths`` declares replaced by its sub-fields."""
    if pairs is None or rule is None or not rule.parse_body_paths:
        return pairs
    parsed: list[tuple[str, str]] = []
    for name, value in pairs:
        parsed.extend(_parsed(name, value, rule.body_path_format(name)))
    return parsed


# ── fields whose value holds more fields ────────────────────────────────


def parsed_subfields(field: str, value: str, format: FieldFormat) -> Params | None:
    """*value* read as *format*, as sub-fields named below *field* — ``None`` when it does not parse.

    - ``urlencoded``: every non-empty ``&``-separated part is ``name=value``
      with a non-empty name; decoded as a query string is (``+`` and ``%20``
      are the same space). Each parameter becomes ``<field>.<name>``, in order.
    - ``json``: an object or an array, flattened by full path below the field
      as if it were written inline (``<field>.a.0.b``; an empty object or
      array is ``<field>={}`` / ``[]``).

    Anything else does not parse — an empty value, a plain token, a JSON
    string or number — and the caller keeps the field whole, so a value of
    another shape never takes the parsed shape's key.
    """
    if format == "urlencoded":
        parts = [part for part in value.split("&") if part]
        if not parts or any(not part.partition("=")[0] or "=" not in part for part in parts):
            return None
        return tuple(
            (f"{field}.{name}", item) for name, item in parse_qsl(value, keep_blank_values=True)
        )
    try:
        document = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict | list):
        return None
    flattened: list[tuple[str, str]] = []
    _flatten_json(document, field, flattened)
    return tuple(flattened)


def _parsed(field: str, value: str, format: FieldFormat | None) -> Params:
    """The field's sub-fields when it is declared with *format* and parses as it, else the field."""
    if format is not None:
        subfields = parsed_subfields(field, value, format)
        if subfields is not None:
            return subfields
    return ((field, value),)


def _flatten_json(value: Any, path: str, out: list[tuple[str, str]]) -> None:
    """``{"a": {"b": [1, {"c": 2}]}}`` → ``a.b.0=1``, ``a.b.1.c=2`` (full paths, never leaf names)."""
    if isinstance(value, dict):
        if not value:
            out.append((path, "{}"))
        for key, child in value.items():
            _flatten_json(child, f"{path}.{key}" if path else str(key), out)
    elif isinstance(value, list):
        if not value:
            out.append((path, "[]"))
        for index, child in enumerate(value):
            _flatten_json(child, f"{path}.{index}" if path else str(index), out)
    else:
        out.append((path, value if isinstance(value, str) else json.dumps(value)))


# ── GraphQL ─────────────────────────────────────────────────────────────


def _is_graphql_operation(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    query = value.get("query")
    if isinstance(query, str) and _GRAPHQL_DOCUMENT.match(query):
        return True
    return _persisted_hash(value) is not None


def _persisted_hash(operation: dict[str, Any]) -> str | None:
    extensions = operation.get("extensions")
    if not isinstance(extensions, dict):
        return None
    persisted = extensions.get("persistedQuery")
    if isinstance(persisted, dict) and isinstance(persisted.get("sha256Hash"), str):
        return persisted["sha256Hash"]
    return None


def _graphql_operations(document: Any) -> list[dict[str, Any]] | None:
    """The operations of a GraphQL body (one object, or a batch array), else ``None``."""
    operations = document if isinstance(document, list) else [document]
    if operations and all(_is_graphql_operation(item) for item in operations):
        return operations
    return None


def graphql_params(
    operations: Sequence[dict[str, Any]], rule: SiteRule | None = None
) -> list[tuple[str, str]]:
    """The parameters of GraphQL operations: name, query digest, variables.

    Per operation: ``operationName``; ``query_sha256`` — the sha256 of the
    query with whitespace runs collapsed — or, for a persisted query without
    its text, ``persisted_sha256``; and the variables flattened by full path
    (``variables.input.id``), its ``parse_body_paths`` fields replaced by
    their sub-fields, minus the site rule's ``ignore_body_paths`` (both
    matched within the operation, so one rule covers a single request and a
    batch). A batch's operations are numbered in order (``0.operationName``).
    """
    batch = len(operations) > 1
    pairs: list[tuple[str, str]] = []
    for index, operation in enumerate(operations):
        own: list[tuple[str, str]] = [("operationName", str(operation.get("operationName") or ""))]
        query = operation.get("query")
        if isinstance(query, str) and query.strip():
            normalised = " ".join(query.split())
            own.append(("query_sha256", hashlib.sha256(normalised.encode("utf-8")).hexdigest()))
        else:
            own.append(("persisted_sha256", _persisted_hash(operation) or ""))
        variables = operation.get("variables")
        if variables is not None:
            flat: list[tuple[str, str]] = []
            _flatten_json(variables, "variables", flat)
            own.extend(
                (name, value)
                for name, value in _parsed_body(flat, rule) or ()
                if rule is None or not rule.ignores_body_path(name)
            )
        pairs.extend((f"{index}.{name}" if batch else name, value) for name, value in own)
    return pairs


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
