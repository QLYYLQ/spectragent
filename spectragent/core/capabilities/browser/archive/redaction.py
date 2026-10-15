"""The browser's redaction rules for environment evidence (§4.14, §5.6).

The framework's shared rules (``core/archive/privacy.py``) recognise
credential-shaped *names* and the credential positions of a URL. This module
applies them, plus the browser's own rules, to what the browser archivers
capture — always **before** an ``ArchiveFile`` is built, so nothing withheld
here ever exists as trace bytes:

- **Request headers** (:func:`strip_headers`): ``Cookie``, ``Authorization``,
  ``Proxy-Authorization`` and the archiver's ``strip_request_headers``
  (case-insensitive) are dropped from every header set a browser archiver
  stores — the paused request's, ``requestWillBeSent``'s, the raw set of
  ``requestWillBeSentExtraInfo`` and a WebSocket handshake's. Only the header
  *name* reaches the ledger (rule ``request_header``). From the ExtraInfo
  events nothing but ``headers`` is ever stored, so ``associatedCookies`` and
  the ``blockedCookies`` / ``exemptedCookies`` values are never captured at
  all. ``Set-Cookie`` *response* headers are kept: a replay's cookie jar
  needs them, and the network archiver declares ``contains_session_state``.
  None of the withheld values even enters the archive scope: the network
  archiver admits its events with the values blanked
  (:func:`blank_stripped_headers`) and the cookie lists left out.
- **POST bodies** (:func:`redact_post_body`): in urlencoded, multipart and
  JSON bodies the value of every credential-shaped field becomes
  ``<redacted>`` (rule ``field_name``). Exported because replay (M30) must
  redact a live request with the *same* function before it computes a match
  key — recording and replay then agree byte for byte.
- **Raw DOM** (:func:`redact_raw_capture`): for every sensitive control
  (``dom/sensitivity.py``) the ``value`` attribute, the text below it — its
  user-agent shadow root included, which ``pierce: true`` exposes and which
  holds the typed value — the ``DOMSnapshot`` ``inputValue`` / ``textValue``
  and layout text, and the accessibility ``value`` (rule ``input_value``).
  The BrowserUse cleaner withholds the same positions with the same
  placeholder while it builds its tree, so an offline re-clean of an
  archived capture matches the live one.

Cookie and Web Storage values are stored as they are (``browser.storage``):
the user's ruling of 2026-10-13 keeps the trace and ``env/`` on the machine
that recorded them (§5.6), and ``browser-2`` is the version without the
cookie-value hash.

The rule version is :data:`BROWSER_REDACTION_POLICY`; every ledger these rules
write carries it. ``tests/test_core/test_capabilities/test_browser_redaction.py``
pins a digest of the rule sources, so a rule cannot change without the
version changing with it.
"""

import copy
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote_plus

from spectragent.core.archive.privacy import (
    PRIVACY_POLICY,
    REDACTED,
    RedactionEntry,
    RedactionLedger,
    is_credential_name,
    redact_value,
)
from spectragent.core.capabilities.browser.dom.models import TargetCapture
from spectragent.core.capabilities.browser.dom.sensitivity import (
    attribute_map,
    is_sensitive_control,
)

#: Version of the browser rules; names the shared rules it builds on.
BROWSER_REDACTION_POLICY = f"{PRIVACY_POLICY}+browser-2"

#: Marker every browser archiver declares: its capture applied these rules.
MARKER_BROWSER_REDACTED = f"redacted:{BROWSER_REDACTION_POLICY}"

#: Request headers stripped at capture whatever an archiver configures.
DEFAULT_STRIPPED_REQUEST_HEADERS: tuple[str, ...] = (
    "cookie",
    "authorization",
    "proxy-authorization",
)

_REDACTED_BYTES = REDACTED.encode("utf-8")
#: ``name="…"`` in a multipart part's Content-Disposition (not ``filename=``).
_DISPOSITION_NAME = re.compile(r'(?:^|;)\s*name="([^"]*)"', re.IGNORECASE)
#: A body that is nothing but ``k=v`` pairs joined by ``&`` (text/plain beacons).
_FORM_SHAPE = re.compile(rb"^[^=&\s]+=[^&\s]*(?:&[^=&\s]+=[^&\s]*)*$")


def browser_ledger(entries: Iterable[RedactionEntry]) -> RedactionLedger:
    """A ledger stamped with :data:`BROWSER_REDACTION_POLICY`."""
    return RedactionLedger(policy=BROWSER_REDACTION_POLICY, entries=list(entries))


# ── request headers ───────────────────────────────────────────────────


def stripped_header_set(extra: Iterable[str] = ()) -> frozenset[str]:
    """The lower-cased header names to strip: the defaults plus *extra*."""
    return frozenset(
        name.strip().lower() for name in (*DEFAULT_STRIPPED_REQUEST_HEADERS, *extra) if name.strip()
    )


def strip_headers(
    headers: Any, strip: frozenset[str], *, location: str
) -> tuple[Any, list[RedactionEntry]]:
    """*headers* without the stripped names, and one ledger entry per name removed.

    Accepts both CDP shapes: a ``Network.Headers`` mapping (name → value) and
    a ``Fetch`` ``HeaderEntry`` list (``[{"name": …, "value": …}]``); returns
    the same shape, as a new object. Anything else comes back as an empty
    mapping — an unknown shape is not stored.
    """
    entries: list[RedactionEntry] = []
    if isinstance(headers, Mapping):
        kept: dict[str, Any] = {}
        for name, value in headers.items():
            if str(name).lower() in strip:
                entries.append(
                    RedactionEntry(location=location, field=str(name), rule="request_header")
                )
            else:
                kept[str(name)] = value
        return kept, entries
    if isinstance(headers, list):
        kept_list: list[dict[str, Any]] = []
        for item in headers:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name", ""))
            if name.lower() in strip:
                entries.append(RedactionEntry(location=location, field=name, rule="request_header"))
            else:
                kept_list.append({"name": name, "value": item.get("value", "")})
        return kept_list, entries
    return {}, entries


def blank_stripped_headers(headers: Any, strip: frozenset[str]) -> Any:
    """*headers* with every stripped name's value emptied and the name kept.

    For what travels before :func:`strip_headers` runs (a metadata event on
    its way through the archive scope): the credential value never leaves the
    recorder's receive loop, while the name still reaches ``strip_headers``,
    which drops it and writes it to the ledger. Both CDP shapes, like
    :func:`strip_headers`; anything else becomes an empty mapping.
    """
    if isinstance(headers, Mapping):
        return {
            str(name): ("" if str(name).lower() in strip else value)
            for name, value in headers.items()
        }
    if isinstance(headers, list):
        return [
            {
                "name": str(item.get("name", "")),
                "value": (
                    "" if str(item.get("name", "")).lower() in strip else item.get("value", "")
                ),
            }
            for item in headers
            if isinstance(item, Mapping)
        ]
    return {}


# ── POST bodies ───────────────────────────────────────────────────────


def _media_type(content_type: str | None) -> tuple[str, dict[str, str]]:
    """``"multipart/form-data; boundary=x"`` → ``("multipart/form-data", {"boundary": "x"})``."""
    if not content_type:
        return "", {}
    head, *params = content_type.split(";")
    parsed: dict[str, str] = {}
    for param in params:
        key, eq, value = param.strip().partition("=")
        if eq:
            parsed[key.strip().lower()] = value.strip().strip('"')
    return head.strip().lower(), parsed


def _redact_urlencoded(body: bytes) -> tuple[bytes, list[RedactionEntry]]:
    entries: list[RedactionEntry] = []
    parts = body.split(b"&")
    for index, part in enumerate(parts):
        name, eq, value = part.partition(b"=")
        if not eq or not value:
            continue
        decoded = unquote_plus(name.decode("latin-1"), encoding="utf-8", errors="replace")
        if is_credential_name(decoded):
            parts[index] = name + b"=" + _REDACTED_BYTES
            entries.append(
                RedactionEntry(
                    location=f"/{_pointer_token(decoded)}", field=decoded, rule="field_name"
                )
            )
    return b"&".join(parts), entries


def _redact_multipart(body: bytes, boundary: str) -> tuple[bytes, list[RedactionEntry]]:
    entries: list[RedactionEntry] = []
    delimiter = b"--" + boundary.encode("latin-1")
    pieces = body.split(delimiter)
    for index in range(1, len(pieces)):
        piece = pieces[index]
        if piece.startswith(b"--"):  # the close delimiter and the epilogue
            continue
        head, sep, rest = piece.partition(b"\r\n\r\n")
        if not sep:
            continue
        match = _DISPOSITION_NAME.search(head.decode("latin-1"))
        if match is None or not is_credential_name(match.group(1)):
            continue
        value, tail = (rest[:-2], b"\r\n") if rest.endswith(b"\r\n") else (rest, b"")
        if not value:
            continue
        pieces[index] = head + sep + _REDACTED_BYTES + tail
        entries.append(
            RedactionEntry(
                location=f"/{_pointer_token(match.group(1))}",
                field=match.group(1),
                rule="field_name",
            )
        )
    return delimiter.join(pieces), entries


def _redact_json(body: bytes) -> tuple[bytes, list[RedactionEntry]] | None:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict | list):
        return None
    redacted, entries = redact_value(parsed)
    if not entries:
        return body, []
    text = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8"), entries


def redact_post_body(body: bytes, content_type: str | None) -> tuple[bytes, list[RedactionEntry]]:
    """*body* with the value of every credential-shaped field replaced.

    By the request's ``Content-Type``:

    - ``application/x-www-form-urlencoded`` — each ``name=value`` whose
      unquoted name :func:`~spectragent.core.archive.privacy.is_credential_name`
      gets ``<redacted>`` as its value; every other byte is kept;
    - ``multipart/form-data`` — the body of each such part becomes
      ``<redacted>``; boundaries, part headers and the other parts are kept;
    - ``application/json`` and ``*+json`` — the shared value rule
      (:func:`~spectragent.core.archive.privacy.redact_value`: credential-named
      keys, credentials inside URLs), re-serialised compactly only when
      something was replaced;
    - ``text/plain`` or no type (beacons, some GraphQL clients) — JSON if it
      parses as an object or array, else urlencoded if the whole body is
      ``k=v`` pairs, else unchanged.

    Anything else (binary uploads) is returned unchanged. Deterministic and
    pure: the same body and type always give the same bytes, which is what
    lets replay compute the same match key from a live request.

    Returns:
        The redacted body and its ledger entries (rule ``field_name``; the
        location is a JSON pointer to the field).
    """
    media, params = _media_type(content_type)
    if media == "application/x-www-form-urlencoded":
        return _redact_urlencoded(body)
    if media == "multipart/form-data" and params.get("boundary"):
        return _redact_multipart(body, params["boundary"])
    if media == "application/json" or media.endswith("+json"):
        return _redact_json(body) or (body, [])
    if media in ("", "text/plain"):
        as_json = _redact_json(body)
        if as_json is not None:
            return as_json
        if _FORM_SHAPE.match(body):
            return _redact_urlencoded(body)
    return body, []


# ── raw DOM captures ──────────────────────────────────────────────────


def _pointer_token(token: str) -> str:
    """One JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


class _DomRedaction:
    """One target's redaction pass: which nodes were sensitive, and the entries.

    One pass per ``TargetCapture``: backend node ids form one namespace per
    target only, so a frame's ids must never meet the tab's.
    """

    def __init__(self, entries: list[RedactionEntry]) -> None:
        self.entries = entries
        #: Backend node ids of every node inside a sensitive control.
        self.sensitive_ids: set[int] = set()
        #: Backend node ids of the sensitive controls themselves.
        self.control_ids: set[int] = set()

    def note(self, location: str, field: str | None) -> None:
        self.entries.append(RedactionEntry(location=location, field=field, rule="input_value"))

    # DOM.getDocument / DOM.describeNode nodes ------------------------------

    def dom_node(self, node: Any, pointer: str, inside: bool) -> None:
        """Redact one CDP ``DOM.Node`` (in place, on a copy) and its subtree."""
        if not isinstance(node, dict):
            return
        sensitive = inside
        flat = node.get("attributes")
        if isinstance(flat, list) and node.get("nodeType", 1) == 1:
            tag = str(node.get("nodeName") or node.get("localName") or "")
            if is_sensitive_control(tag, attribute_map(flat)):
                sensitive = True
                if isinstance(node.get("backendNodeId"), int):
                    self.control_ids.add(node["backendNodeId"])
        if sensitive:
            if isinstance(node.get("backendNodeId"), int):
                self.sensitive_ids.add(node["backendNodeId"])
            if isinstance(flat, list):
                for i in range(0, len(flat) - 1, 2):
                    if str(flat[i]).lower() == "value" and flat[i + 1] not in ("", REDACTED):
                        flat[i + 1] = REDACTED
                        self.note(f"{pointer}/attributes/{i + 1}", "value")
            if node.get("nodeType") == 3 and node.get("nodeValue") not in (None, "", REDACTED):
                node["nodeValue"] = REDACTED
                self.note(f"{pointer}/nodeValue", None)
        for key in ("children", "shadowRoots", "pseudoElements"):
            items = node.get(key)
            if isinstance(items, list):
                for index, child in enumerate(items):
                    self.dom_node(child, f"{pointer}/{key}/{index}", sensitive)
        for key in ("contentDocument", "templateContent", "importedDocument"):
            if isinstance(node.get(key), dict):
                self.dom_node(node[key], f"{pointer}/{key}", sensitive)

    # the Skyvern page script's tree ----------------------------------------

    def skyvern_node(self, node: Any, pointer: str, inside: bool) -> None:
        """The page script already withholds sensitive values; this is the backstop."""
        if not isinstance(node, dict):
            return
        attrs = node.get("attributes")
        sensitive = inside or (
            isinstance(attrs, dict) and is_sensitive_control(str(node.get("tagName") or ""), attrs)
        )
        if sensitive:
            if isinstance(attrs, dict) and attrs.get("value") not in (None, "", REDACTED):
                attrs["value"] = REDACTED
                self.note(f"{pointer}/attributes/value", "value")
            if node.get("text") not in (None, "", REDACTED):
                node["text"] = REDACTED
                self.note(f"{pointer}/text", None)
        for key in ("children", "shadowRoots"):
            items = node.get(key)
            if isinstance(items, list):
                for index, child in enumerate(items):
                    self.skyvern_node(child, f"{pointer}/{key}/{index}", sensitive)

    # DOMSnapshot.captureSnapshot -------------------------------------------

    def snapshot(self, snap: Any, pointer: str) -> None:
        """Redact a ``DOMSnapshot`` result in place (it is already a copy).

        Its strings live in one shared table. A string referenced only from
        sensitive positions is overwritten in the table itself (pointing
        elsewhere would leave it on disk, unreferenced); one that is also
        used elsewhere — visible text, an attribute of another node — stays,
        and each sensitive reference to it is pointed at ``<redacted>``.
        """
        if not isinstance(snap, dict):
            return
        strings = snap.get("strings")
        documents = snap.get("documents")
        if not isinstance(strings, list) or not isinstance(documents, list):
            return
        sensitive_refs: list[tuple[list[Any], int, str, str | None]] = []
        other: set[int] = set()

        def text(index: Any) -> str:
            return (
                str(strings[index]) if isinstance(index, int) and 0 <= index < len(strings) else ""
            )

        for d, doc in enumerate(documents):
            if not isinstance(doc, dict):
                continue
            base = f"{pointer}/documents/{d}"
            for key in (
                "documentURL",
                "title",
                "baseURL",
                "contentLanguage",
                "encodingName",
                "publicId",
                "systemId",
                "frameId",
            ):
                if isinstance(doc.get(key), int):
                    other.add(doc[key])
            raw_nodes = doc.get("nodes")
            nodes: dict[str, Any] = raw_nodes if isinstance(raw_nodes, dict) else {}
            parents = nodes.get("parentIndex") or []
            names = nodes.get("nodeName") or []
            values = nodes.get("nodeValue") or []
            attributes = nodes.get("attributes") or []
            backend = nodes.get("backendNodeId") or []
            count = max(len(parents), len(names))
            flags = [False] * count
            for i in range(count):
                parent = parents[i] if i < len(parents) else -1
                inherited = flags[parent] if isinstance(parent, int) and 0 <= parent < i else False
                flat = attributes[i] if i < len(attributes) else []
                amap = {
                    text(flat[k]).lower(): text(flat[k + 1]) for k in range(0, len(flat) - 1, 2)
                }
                own = is_sensitive_control(text(names[i]) if i < len(names) else "", amap)
                flags[i] = inherited or own
                if flags[i] and i < len(backend) and isinstance(backend[i], int):
                    self.sensitive_ids.add(backend[i])
                    if own:
                        self.control_ids.add(backend[i])
            for i in range(count):
                if i < len(names):
                    other.add(names[i])
                flat = attributes[i] if i < len(attributes) else []
                for k in range(0, len(flat) - 1, 2):
                    other.add(flat[k])
                    if flags[i] and text(flat[k]).lower() == "value":
                        sensitive_refs.append(
                            (flat, k + 1, f"{base}/nodes/attributes/{i}/{k + 1}", "value")
                        )
                    else:
                        other.add(flat[k + 1])
                if i < len(values):
                    if flags[i]:
                        sensitive_refs.append((values, i, f"{base}/nodes/nodeValue/{i}", None))
                    else:
                        other.add(values[i])
            for key in (
                "shadowRootType",
                "pseudoType",
                "pseudoIdentifier",
                "currentSourceURL",
                "originURL",
            ):
                rare = nodes.get(key)
                if isinstance(rare, dict):
                    other.update(v for v in rare.get("value") or [] if isinstance(v, int))
            for key in ("inputValue", "textValue"):
                rare = nodes.get(key)
                if not isinstance(rare, dict):
                    continue
                rare_values = rare.get("value") or []
                for j, node_index in enumerate(rare.get("index") or []):
                    if j >= len(rare_values):
                        break
                    if (
                        isinstance(node_index, int)
                        and 0 <= node_index < count
                        and flags[node_index]
                    ):
                        sensitive_refs.append(
                            (rare_values, j, f"{base}/nodes/{key}/value/{j}", key)
                        )
                    else:
                        other.add(rare_values[j])
            raw_layout = doc.get("layout")
            layout: dict[str, Any] = raw_layout if isinstance(raw_layout, dict) else {}
            for style in layout.get("styles") or []:
                if isinstance(style, list):
                    other.update(v for v in style if isinstance(v, int))
            layout_text = layout.get("text") or []
            for j, node_index in enumerate(layout.get("nodeIndex") or []):
                if j >= len(layout_text):
                    break
                if isinstance(node_index, int) and 0 <= node_index < count and flags[node_index]:
                    sensitive_refs.append((layout_text, j, f"{base}/layout/text/{j}", None))
                else:
                    other.add(layout_text[j])

        redacted_index: int | None = None
        for container, position, location, field in sensitive_refs:
            index = container[position]
            if not isinstance(index, int) or not 0 <= index < len(strings):
                continue
            if strings[index] in ("", REDACTED):
                continue
            if index in other:
                if redacted_index is None:
                    strings.append(REDACTED)
                    redacted_index = len(strings) - 1
                container[position] = redacted_index
            else:
                strings[index] = REDACTED
            self.note(location, field)

    # Accessibility.getFullAXTree -------------------------------------------

    def ax_tree(self, tree: Any, pointer: str) -> None:
        if not isinstance(tree, dict) or not isinstance(tree.get("nodes"), list):
            return
        for index, node in enumerate(tree["nodes"]):
            if not isinstance(node, dict):
                continue
            backend = node.get("backendDOMNodeId")
            if backend not in self.sensitive_ids:
                continue
            location = f"{pointer}/nodes/{index}"
            value = node.get("value")
            if isinstance(value, dict) and value.get("value") not in (None, "", REDACTED):
                value["value"] = REDACTED
                self.note(f"{location}/value/value", "value")
            name = node.get("name")
            if isinstance(name, dict):
                if backend not in self.control_ids and name.get("value") not in (
                    None,
                    "",
                    REDACTED,
                ):
                    name["value"] = REDACTED
                    self.note(f"{location}/name/value", "name")
                for s, source in enumerate(name.get("sources") or []):
                    if not isinstance(source, dict) or source.get("attribute") != "value":
                        continue
                    inner = source.get("value")
                    if isinstance(inner, dict) and inner.get("value") not in (None, "", REDACTED):
                        inner["value"] = REDACTED
                        self.note(f"{location}/name/sources/{s}/value/value", "value")


def _capture_dict(
    capture: TargetCapture, pointer: str, entries: list[RedactionEntry]
) -> dict[str, Any]:
    pass_ = _DomRedaction(entries)
    raw_dom = copy.deepcopy(capture.raw_dom)
    snapshot = copy.deepcopy(capture.snapshot)
    ax_tree = copy.deepcopy(capture.ax_tree)
    if isinstance(raw_dom.get("root"), dict):
        pass_.dom_node(raw_dom["root"], f"{pointer}/raw_dom/root", False)
    if isinstance(raw_dom.get("skyvern_tree"), dict):
        pass_.skyvern_node(raw_dom["skyvern_tree"], f"{pointer}/raw_dom/skyvern_tree", False)
    nodes = raw_dom.get("skyvern_nodes")
    if isinstance(nodes, dict):
        for key, node in nodes.items():
            pass_.dom_node(
                node, f"{pointer}/raw_dom/skyvern_nodes/{_pointer_token(str(key))}", False
            )
    pass_.snapshot(snapshot, f"{pointer}/snapshot")
    frames = {
        frame_id: _capture_dict(child, f"{pointer}/frames/{_pointer_token(frame_id)}", entries)
        for frame_id, child in sorted(capture.frames.items())
    }
    # Accessibility last: it is matched against the sensitive nodes this
    # target's DOM and snapshot passes found (by backend node id).
    pass_.ax_tree(ax_tree, f"{pointer}/ax_tree")
    return {
        "raw_dom": raw_dom,
        "snapshot": snapshot,
        "ax_tree": ax_tree,
        "session_id": capture.session_id,
        "target_id": capture.target_id,
        "namespace": capture.namespace,
        "js_click_listener_ids": (
            sorted(capture.js_click_listener_ids)
            if capture.js_click_listener_ids is not None
            else None
        ),
        "frames": frames,
        "loader_id": capture.loader_id,
        "url": capture.url,
    }


def redact_raw_capture(capture: TargetCapture) -> tuple[dict[str, Any], list[RedactionEntry]]:
    """A raw DOM capture as JSON-ready data, with every sensitive value withheld.

    Works on copies — the capture itself (shared with the cycle that produced
    it) is never changed. Covers both capture paths: BrowserUse's
    ``DOM.getDocument`` / ``DOMSnapshot`` / accessibility triple and
    Skyvern's page-script tree plus ``DOM.describeNode`` results, recursively
    through out-of-process frame captures. Sets become sorted lists.

    Returns:
        The capture as a plain mapping, and the ledger entries (rule
        ``input_value``), each locating the replaced value by JSON pointer.
    """
    entries: list[RedactionEntry] = []
    data = _capture_dict(capture, "", entries)
    return data, entries
