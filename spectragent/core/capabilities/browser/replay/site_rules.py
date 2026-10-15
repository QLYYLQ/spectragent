"""Per-site replay rules: ``<domain>.yaml`` files in one directory (§4.14 "站点规则").

The archive source's ``matcher.site_rules`` names a directory (shipped example:
``configs/replay_rules/``). Each ``<domain>.yaml`` holds one :class:`SiteRule`
for that domain and every host below it; a request's host picks its file by
the **longest suffix** on label boundaries (``a.b.example.com`` tries
``a.b.example.com.yaml``, ``b.example.com.yaml``, ``example.com.yaml``,
``com.yaml``), so a file named after the registrable domain works without a
Public Suffix List.

A rule only ever loosens or shapes what the shared canonicaliser
(``canonical.py``) and the matcher do for that site:

- ``ignore_query`` — query parameter names (``fnmatch`` globs) left out of the
  normalised key and the fuzzy score;
- ``ignore_body_paths`` — full-path globs (``variables.cursor``,
  ``client.*``) left out of a body's key — form field names, flattened JSON
  paths, GraphQL variables (matched within one operation);
- ``ignore_headers`` — request header names (globs, case-insensitive) left
  out of the header tie-break;
- ``fuzzy: on`` — the fuzzy level for this site even for documents and
  non-GET requests, and even when ``matcher.levels`` lacks ``fuzzy``
  (YAML 1.1 reads a bare ``on`` as ``true``);
- ``synthesize_204`` — URL globs whose misses are benign: answered 204 by the
  miss policy, like analytics;
- ``response_echo`` — on a hit, copy values from the request's JSON into the
  served response's JSON (a request id the page checks for, the Salesforce
  Aura shape WARC-Bench handles): :meth:`SiteRule.echo`.

The directory's digest (each file's name and sha256, sorted) goes into the
replay fingerprint, so two replays with different rules are told apart.
"""

import fnmatch
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The file suffix of a site rule.
RULE_SUFFIX = ".yaml"


class SiteRuleError(ValueError):
    """A site-rule directory or file that cannot be used, naming it."""


class EchoRule(BaseModel):
    """Copy request values into the response, for requests whose URL matches ``url``.

    Paths are dot-separated; a ``*`` segment walks every element of an array
    (or every value of an object). A form body's parameter whose value is a
    JSON object or array is walked into too (``message.actions.*.id``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(description="fnmatch glob over the request URL (no fragment).")
    request_path: str = Field(description="Where the values are in the request body.")
    response_path: str = Field(description="Where they go in the response body.")


class SiteRule(BaseModel):
    """One site's replay rules (``<domain>.yaml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ignore_query: list[str] = Field(default_factory=list)
    ignore_body_paths: list[str] = Field(default_factory=list)
    ignore_headers: list[str] = Field(default_factory=list)
    fuzzy: bool = False
    synthesize_204: list[str] = Field(default_factory=list)
    response_echo: list[EchoRule] = Field(default_factory=list)

    def ignores_query(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.ignore_query)

    def ignores_body_path(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.ignore_body_paths)

    def ignores_header(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in self.ignore_headers)

    def synthesizes_204(self, url: str) -> bool:
        """Whether a miss of *url* is benign by this rule."""
        bare = url.split("#", 1)[0]
        return any(fnmatch.fnmatchcase(bare, pattern) for pattern in self.synthesize_204)

    def echo(
        self,
        url: str,
        request_body: bytes | None,
        request_content_type: str | None,
        response: bytes,
    ) -> bytes:
        """*response* with this rule's ``response_echo`` applied for a request to *url*.

        Values found at ``request_path`` are written, in order, to the
        positions ``response_path`` finds (only positions that exist — echo
        never adds a key); one request value is written to every position.
        A response that is not JSON, a request that carries no values there,
        or values that are already equal leave the response unchanged, byte
        for byte.
        """
        bare = url.split("#", 1)[0]
        rules = [rule for rule in self.response_echo if fnmatch.fnmatchcase(bare, rule.url)]
        if not rules or not request_body:
            return response
        request = _request_document(request_body, request_content_type)
        if request is None:
            return response
        try:
            document = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return response
        changed = False
        for rule in rules:
            values = list(_values_at(request, _segments(rule.request_path)))
            if not values:
                continue
            slots = list(_slots_at(document, _segments(rule.response_path)))
            for index, (parent, key) in enumerate(slots):
                if len(values) == 1:
                    value = values[0]
                elif index < len(values):
                    value = values[index]
                else:
                    break
                if parent[key] != value:
                    parent[key] = value
                    changed = True
        if not changed:
            return response
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class SiteRules:
    """Every rule of one directory, by domain."""

    def __init__(
        self,
        rules: Mapping[str, SiteRule] | None = None,
        *,
        sha256: str | None = None,
        directory: Path | None = None,
    ) -> None:
        self._rules = {domain.lower(): rule for domain, rule in (rules or {}).items()}
        #: The directory's digest (``None`` when no directory is configured).
        self.sha256 = sha256
        self.directory = directory

    @classmethod
    def load(cls, directory: str | Path | None) -> "SiteRules":
        """Read every ``<domain>.yaml`` of *directory*; ``None`` → no rules.

        Raises:
            SiteRuleError: The directory does not exist, or a file is not
                YAML, or not a mapping, or carries an unknown key — naming
                the file.
        """
        if directory is None:
            return cls()
        root = Path(directory)
        if not root.is_dir():
            raise SiteRuleError(f"site rules directory {root} does not exist")
        rules: dict[str, SiteRule] = {}
        listing: list[tuple[str, str]] = []
        for path in sorted(root.glob(f"*{RULE_SUFFIX}")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            listing.append((path.name, hashlib.sha256(data).hexdigest()))
            rules[path.name[: -len(RULE_SUFFIX)].lower()] = _parse_rule(path, data)
        return cls(rules, sha256=_listing_digest(listing), directory=root)

    def for_host(self, host: str) -> SiteRule | None:
        """The rule of the longest domain suffix of *host*, or ``None``."""
        labels = host.lower().rstrip(".").split(".")
        for start in range(len(labels)):
            rule = self._rules.get(".".join(labels[start:]))
            if rule is not None:
                return rule
        return None

    def domains(self) -> list[str]:
        return sorted(self._rules)

    def __len__(self) -> int:
        return len(self._rules)


def _parse_rule(path: Path, data: bytes) -> SiteRule:
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SiteRuleError(f"site rule {path} is not YAML: {exc}") from exc
    if raw is None:
        return SiteRule()
    if not isinstance(raw, dict):
        raise SiteRuleError(f"site rule {path} is not a mapping of rule keys")
    try:
        return SiteRule.model_validate(raw)
    except ValidationError as exc:
        raise SiteRuleError(f"site rule {path} is not a valid rule: {exc}") from exc


def _listing_digest(listing: Sequence[tuple[str, str]]) -> str:
    """sha256 over ``name NUL sha256 LF`` for every file, sorted by name."""
    digest = hashlib.sha256()
    for name, sha256 in sorted(listing):
        digest.update(f"{name}\0{sha256}\n".encode())
    return digest.hexdigest()


# ── response_echo: paths through JSON documents ──────────────────────


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split(".") if segment]


def _children(node: Any) -> Iterator[tuple[Any, Any]]:
    """``(key, child)`` for every child of a container."""
    if isinstance(node, list):
        yield from enumerate(node)
    elif isinstance(node, dict):
        yield from node.items()


def _child(node: Any, segment: str) -> tuple[bool, Any]:
    if isinstance(node, dict) and segment in node:
        return True, node[segment]
    if isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
        return True, node[int(segment)]
    return False, None


def _values_at(node: Any, segments: Sequence[str]) -> Iterator[Any]:
    """Every value at *segments* below *node*, in document order."""
    if not segments:
        yield node
        return
    head, rest = segments[0], segments[1:]
    if head == "*":
        for _, child in _children(node):
            yield from _values_at(child, rest)
        return
    found, child = _child(node, head)
    if found:
        yield from _values_at(child, rest)


def _slots_at(node: Any, segments: Sequence[str]) -> Iterator[tuple[Any, Any]]:
    """``(container, key)`` for every existing position at *segments*, in document order."""
    if not segments:
        return
    head, rest = segments[0], segments[1:]
    keys: list[Any]
    if head == "*":
        keys = [key for key, _ in _children(node)]
    else:
        found, _ = _child(node, head)
        if not found:
            return
        keys = [int(head) if isinstance(node, list) else head]
    for key in keys:
        if not rest:
            yield node, key
        else:
            yield from _slots_at(node[key], rest)


def _request_document(body: bytes, content_type: str | None) -> Any:
    """The request body as a JSON document — JSON, or a form whose JSON values are parsed."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        pass
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media not in ("", "application/x-www-form-urlencoded", "text/plain"):
        return None
    form: dict[str, Any] = {}
    for name, value in parse_qsl(text, keep_blank_values=True):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = value
        form[name] = parsed if isinstance(parsed, dict | list) else value
    return form or None
