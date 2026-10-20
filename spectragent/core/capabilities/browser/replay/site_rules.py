"""Frozen, explicitly scoped replay rules and their reproducible source snapshots.

Rules select by method, host/path or URL and effective port. The greatest
explicit priority wins; equal-priority matches are configuration conflicts.
Every rule declares response-sequence exhaustion and how a recorded request
without a body is answered. File order never selects behavior, and all rule
collections are immutable after parsing.
"""

import fnmatch
import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The file suffix of a site rule.
RULE_SUFFIX = ".yaml"


class SiteRuleError(ValueError):
    """A site-rule directory or file that cannot be used, naming it."""


MatchLevel = Literal["exact", "normalized", "fuzzy"]
ExhaustionPolicy = Literal["repeat_last", "miss"]
#: How a consumed record without a body (``body_unavailable``) is answered:
#: by the miss policy, or by an available record of the same sequence when
#: every available record there has the same status and body (§4.14 "匹配").
UnavailablePolicy = Literal["miss", "serve_identical"]


class RequestScope(BaseModel):
    """Conjunctive request selectors; omitted selectors match every value.

    Host, path and URL are fnmatch globs. Host matching ignores case and a
    trailing dot; it does not implicitly include subdomains. URL matching
    ignores the fragment. Port is the effective port, including 80/443.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str | None = None
    path: str | None = None
    url: str | None = None
    method: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)

    def matches(self, method: str, url: str) -> bool:
        bare = url.split("#", 1)[0]
        if self.method is not None and self.method.upper() != method.upper():
            return False
        if self.url is not None and not fnmatch.fnmatchcase(bare, self.url):
            return False
        if self.host is None and self.path is None and self.port is None:
            return True
        try:
            parts = urlsplit(bare)
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port
        except ValueError:
            return False
        if port is None:
            port = {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(parts.scheme.lower())
        return (
            (self.host is None or fnmatch.fnmatchcase(host, self.host.lower().rstrip(".")))
            and (self.path is None or fnmatch.fnmatchcase(parts.path or "/", self.path))
            and (self.port is None or self.port == port)
        )


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
    """One complete request policy; matching rules never implicitly merge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    scope: RequestScope = Field(default_factory=RequestScope)
    priority: int = 0
    exhaustion: ExhaustionPolicy
    unavailable: UnavailablePolicy
    levels: tuple[MatchLevel, ...] | None = Field(default=None, min_length=1)
    ignore_query: tuple[str, ...] = ()
    keep_query: tuple[str, ...] = ()
    ignore_body_paths: tuple[str, ...] = ()
    keep_body_paths: tuple[str, ...] = ()
    ignore_headers: tuple[str, ...] = ()
    keep_headers: tuple[str, ...] = ()
    fuzzy: bool = False
    synthesize_204: tuple[str, ...] = ()
    response_echo: tuple[EchoRule, ...] = ()

    @field_validator("levels")
    @classmethod
    def unique_levels(cls, levels: tuple[MatchLevel, ...] | None) -> tuple[MatchLevel, ...] | None:
        if levels is not None and len(levels) != len(set(levels)):
            raise ValueError("each matching level must appear once")
        return levels

    def ignores_query(self, name: str) -> bool:
        return not any(fnmatch.fnmatchcase(name, p) for p in self.keep_query) and any(
            fnmatch.fnmatchcase(name, p) for p in self.ignore_query
        )

    def ignores_body_path(self, path: str) -> bool:
        return not any(fnmatch.fnmatchcase(path, p) for p in self.keep_body_paths) and any(
            fnmatch.fnmatchcase(path, p) for p in self.ignore_body_paths
        )

    def ignores_header(self, name: str) -> bool:
        lowered = name.lower()
        return not any(fnmatch.fnmatchcase(lowered, p.lower()) for p in self.keep_headers) and any(
            fnmatch.fnmatchcase(lowered, p.lower()) for p in self.ignore_headers
        )

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


class RuleFileSnapshot(BaseModel):
    """The exact UTF-8 source adopted by this run, with its provenance and digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    origin: str
    sha256: str
    text: str


class SiteRulesSnapshot(BaseModel):
    """Self-contained replay input; digests are checked by ``from_snapshot``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: str | None
    sha256: str
    files: tuple[RuleFileSnapshot, ...]


class RuleDocument(BaseModel):
    """The only accepted YAML document shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[SiteRule, ...] = Field(min_length=1)


class SiteRules:
    """An immutable set of rules, preserving the exact source read at load time."""

    def __init__(self, rules: Sequence[SiteRule] = ()) -> None:
        self._rules = tuple(rules)
        self._validate_ids()
        document = RuleDocument(rules=self._rules) if self._rules else None
        files: tuple[RuleFileSnapshot, ...] = ()
        if document is not None:
            text = document.model_dump_json()
            files = (
                RuleFileSnapshot(
                    name="memory.yaml",
                    origin="memory",
                    text=text,
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
        self._snapshot = SiteRulesSnapshot(
            directory=None,
            sha256=_listing_digest([(f.name, f.sha256) for f in files]),
            files=files,
        )

    def _validate_ids(self) -> None:
        ids = [rule.id for rule in self._rules]
        repeated = sorted({identity for identity in ids if ids.count(identity) > 1})
        if repeated:
            raise SiteRuleError(f"duplicate replay rule ids: {repeated}")

    @property
    def sha256(self) -> str:
        return self._snapshot.sha256

    @property
    def directory(self) -> Path | None:
        return Path(self._snapshot.directory) if self._snapshot.directory is not None else None

    @classmethod
    def load(cls, directory: str | Path | None) -> "SiteRules":
        """Read each rule file once; its digest and behavior derive from those bytes."""
        if directory is None:
            return cls()
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise SiteRuleError(f"site rules directory {root} does not exist")
        files: list[RuleFileSnapshot] = []
        for path in sorted(root.glob(f"*{RULE_SUFFIX}")):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise SiteRuleError(f"cannot read site rule {path}: {exc}") from exc
            files.append(
                RuleFileSnapshot(
                    name=path.name,
                    origin=str(path),
                    text=text,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
        return cls.from_snapshot(
            SiteRulesSnapshot(
                directory=str(root),
                sha256=_listing_digest([(f.name, f.sha256) for f in files]),
                files=tuple(files),
            )
        )

    def snapshot(self) -> SiteRulesSnapshot:
        """Return frozen input without reading its source directory again."""
        return self._snapshot

    @classmethod
    def from_snapshot(cls, snapshot: SiteRulesSnapshot) -> "SiteRules":
        """Validate every digest and reconstruct behavior entirely from the snapshot."""
        names = [file.name for file in snapshot.files]
        if len(names) != len(set(names)):
            raise SiteRuleError("replay rule snapshot contains duplicate file names")
        rules: list[SiteRule] = []
        listing: list[tuple[str, str]] = []
        for file in snapshot.files:
            digest = hashlib.sha256(file.text.encode("utf-8")).hexdigest()
            if digest != file.sha256:
                raise SiteRuleError(f"replay rule snapshot {file.name}: sha256 mismatch")
            listing.append((file.name, digest))
            rules.extend(_parse_rules(file.origin, file.text))
        if _listing_digest(listing) != snapshot.sha256:
            raise SiteRuleError("replay rule snapshot: collection sha256 mismatch")
        instance = cls(rules)
        instance._snapshot = snapshot
        return instance

    def for_request(self, method: str, url: str) -> SiteRule | None:
        """Select the highest priority policy, refusing ties with both identities."""
        matches = [rule for rule in self._rules if rule.scope.matches(method, url)]
        if not matches:
            return None
        priority = max(rule.priority for rule in matches)
        best = [rule for rule in matches if rule.priority == priority]
        if len(best) != 1:
            raise SiteRuleError(
                f"replay rules {sorted(rule.id for rule in best)} conflict at priority "
                f"{priority} for {method.upper()} {url}"
            )
        return best[0]

    def require_for_request(self, method: str, url: str) -> SiteRule:
        """Require explicit policy for a known recording before replay can serve it."""
        rule = self.for_request(method, url)
        if rule is None:
            raise SiteRuleError(f"no replay rule declares exhaustion for {method.upper()} {url}")
        return rule

    def validate_requests(self, requests: Iterable[tuple[str, str]]) -> None:
        """Check coverage and priority conflicts for recorded or otherwise known requests."""
        for method, url in requests:
            self.require_for_request(method, url)

    def validate_levels(self, levels: Sequence[MatchLevel]) -> None:
        """Rules may restrict the run's allowed levels, never expand them."""
        allowed = set(levels)
        for rule in self._rules:
            required = set(rule.levels or ()) | ({"fuzzy"} if rule.fuzzy else set())
            if required - allowed:
                raise SiteRuleError(
                    f"replay rule {rule.id!r} requests levels {sorted(required - allowed)} "
                    f"but matcher.levels permits only {sorted(allowed)}"
                )
            if rule.fuzzy and rule.levels is not None and "fuzzy" not in rule.levels:
                raise SiteRuleError(
                    f"replay rule {rule.id!r}: fuzzy permission conflicts with levels"
                )

    def __len__(self) -> int:
        return len(self._rules)


def _parse_rules(source: str, text: str) -> tuple[SiteRule, ...]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SiteRuleError(f"site rule {source} is not YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SiteRuleError(f"site rule {source} is not a mapping of rule keys")
    try:
        return RuleDocument.model_validate(raw).rules
    except ValidationError as exc:
        raise SiteRuleError(f"site rule {source} is not a valid rule: {exc}") from exc


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
