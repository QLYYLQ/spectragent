"""Shared privacy rules: one vocabulary for every capability that redacts.

The framework never reads or changes evidence bytes — capturing and
masking are the capability's job (``docs/architecture_design.md`` §4.9,
§5.6). What lives here is what every capability, and the framework's own
``TraceConfig.task_setup``, must agree on:

- :data:`PRIVACY_POLICY` — the version of the rules below. A trace records
  it beside every ledger, so a reader knows which rules produced a
  ``<redacted>``. **Changing a rule means bumping it**;
  ``tests/test_core/test_archive/test_privacy.py`` pins a digest of the
  rule sources — both name patterns, the placeholder, the URL pattern and
  the code of every function below that decides what is replaced, the URL
  rules included — to make forgetting that impossible.
- Two name patterns: :data:`SENSITIVE_FIELD_PATTERN` (the PRD §5.10 list
  for form and POST fields, a substring search — deliberately
  over-inclusive) and :data:`CREDENTIAL_NAME_PATTERN` (a trailing
  ``key`` / ``apikey`` / ``sig`` / ``signature`` / ``auth`` /
  ``credential`` segment — ``api_key``, ``OPENAI_API_KEY``, signed-URL
  parameters, not ``keyword`` or ``monkey``).
- The URL rules (:func:`redact_url`) and the value walk
  (:func:`redact_value`).
- The ledger (:class:`RedactionLedger`) — *where* something was replaced
  and by which rule, so a reader never has to guess whether an empty-looking
  value was empty.
- Privacy marker constants an archiver may declare
  (``ArchiverBase.privacy_markers``).

``<redacted>`` is deliberately not the config redaction's ``"<omitted>"``:
that one means "set, and withheld because nothing marked it safe" (deny by
default, :mod:`spectragent.core.trace.redaction`); this one means "this
value was replaced by a rule". Neither matches a credential *shape*, so the
secret guard never mistakes a placeholder for a leak.

**The residual, stated:** these rules recognise credential-shaped *names*
and the credential positions of a URL. A secret under a name neither
pattern matches (``{"note": "the password is hunter2"}``, a key named
``pass``) is kept as is, and so is a credential inside a URL nested in a
non-credential query parameter — its userinfo when unencoded, a ``token=``
parameter when percent-encoded — and a CDP devtools URL under ``cdp_url`` (a
key ``BrowserCapability.setup`` accepts, though no shipped entry point
routes it through the setup context). Covering any of them is a new rule —
and a new :data:`PRIVACY_POLICY`. Recorded as a to-do, not done: the
user's ruling of 2026-10-13 keeps traces on the machine that recorded them.

.. note::
   No ``from __future__ import annotations`` — the package rule
   (:mod:`spectragent.core.archive._registrar`). This is also the one
   module of the package ``core/trace`` may import (``TaskSetupRecord``
   types its ledger with :class:`RedactionLedger`).
"""

import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import unquote_plus

from pydantic import BaseModel, ConfigDict, Field

#: Version of the rules in this module. Bump it whenever a pattern, the
#: placeholder or a URL rule changes.
PRIVACY_POLICY = "af-privacy-1"

#: What a redacted value is replaced with.
REDACTED = "<redacted>"

#: Field names whose value is sensitive wherever it appears — form fields,
#: POST bodies, mapping keys. A substring search, case-insensitive, and
#: over-inclusive on purpose (``discard`` matches ``card``): a false
#: positive costs one recorded value, a false negative a leaked one.
SENSITIVE_FIELD_PATTERN = re.compile(
    r"password|passwd|pwd|otp|token|secret|card|cvv", re.IGNORECASE
)

#: Names that are credentials when they *end* in one of these segments —
#: ``key=`` and ``api_key=`` in a URL, ``OPENAI_API_KEY`` in a mapping, the
#: signature and credential parameters of a signed URL. Anchored to a
#: segment boundary so ``keyword`` and ``monkey`` stay readable.
CREDENTIAL_NAME_PATTERN = re.compile(
    r"(?:^|[-_.])(?:key|apikey|sig|signature|auth|credential)$", re.IGNORECASE
)

#: Marker an archiver declares when its capture applies these rules.
MARKER_REDACTED = f"redacted:{PRIVACY_POLICY}"

#: Marker an archiver (and its capability's run record) declares when the
#: evidence carries session state — cookies kept for a replay's cookie jar.
MARKER_SESSION_STATE = "contains_session_state"

#: Which rule replaced a value. The first four are this module's own rules;
#: the last two name capability rules that write into the same ledger
#: format (the browser's, ``core/capabilities/browser/archive/redaction.py``):
#: ``request_header`` — a request header stripped at capture (only its name is
#: recorded); ``input_value`` — a sensitive form control's value in a raw DOM
#: capture. Changing the vocabulary changes no rule of this module, so
#: :data:`PRIVACY_POLICY` stays; each capability versions its own rules.
RedactionRule = Literal[
    "field_name",
    "url_userinfo",
    "url_query",
    "url_fragment",
    "request_header",
    "input_value",
]

#: ``scheme://`` — the only strings :func:`redact_url` treats as URLs.
_ABSOLUTE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


class RedactionEntry(BaseModel):
    """One replacement: where, which name, which rule.

    Attributes:
        location: JSON pointer to the value that held it (``/start_url``;
            ``""`` for the document root). For a URL rule the pointer names
            the string, and :attr:`field` names the parameter inside it.
        field: The mapping key or URL parameter whose value was replaced;
            ``None`` for a URL's userinfo, which has no name.
        rule: Which rule fired.
    """

    model_config = ConfigDict(frozen=True)

    location: str
    field: str | None = None
    rule: RedactionRule


class RedactionLedger(BaseModel):
    """Every replacement one redaction pass made, in traversal order.

    Attributes:
        policy: The rule version that was in force — :data:`PRIVACY_POLICY`
            for this module's rules, a capability's own version (which
            names this one, ``"af-privacy-1+browser-2"``) for a ledger its
            rules wrote.
        entries: The replacements. Empty means nothing matched — or, where
            the owner says so (``TraceConfig.secrets_recorded``), that
            redaction was switched off.
    """

    policy: str = PRIVACY_POLICY
    entries: list[RedactionEntry] = Field(default_factory=list)


def is_sensitive_field(name: str) -> bool:
    """True when *name* contains one of the PRD §5.10 sensitive substrings."""
    return SENSITIVE_FIELD_PATTERN.search(name) is not None


def is_credential_name(name: str) -> bool:
    """True when *name* is credential-shaped by either pattern."""
    return is_sensitive_field(name) or CREDENTIAL_NAME_PATTERN.search(name) is not None


def _escape_pointer(token: str) -> str:
    """One JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _redact_params(
    text: str, *, location: str, rule: RedactionRule
) -> tuple[str, list[RedactionEntry]]:
    """Replace the value of every credential-named ``k=v`` in an ``&`` list.

    Names are matched unquoted (``api%5Fkey`` is ``api_key``); every byte
    that is not a replaced value is kept exactly — no re-encoding. A
    parameter with an empty value has nothing to replace and is left
    alone, so the ledger never claims a value that was not there.
    """
    entries: list[RedactionEntry] = []
    parts = text.split("&")
    for index, part in enumerate(parts):
        name, eq, value = part.partition("=")
        if not eq or not value:
            continue
        decoded = unquote_plus(name)
        if is_credential_name(decoded):
            parts[index] = f"{name}={REDACTED}"
            entries.append(RedactionEntry(location=location, field=decoded, rule=rule))
    return "&".join(parts), entries


def redact_url(url: str, *, location: str = "") -> tuple[str, list[RedactionEntry]]:
    """Replace the credential parts of an absolute URL; keep every other byte.

    Only a string that starts with ``scheme://`` is treated as a URL —
    anything else comes back unchanged with no entries. Three rules:

    - ``url_userinfo`` — everything before the last ``@`` of the authority
      is replaced *whole* by ``<redacted>``, so no ``user:password@`` shape
      survives (``https://<redacted>@host/``).
    - ``url_query`` — the value of every query parameter whose unquoted
      name :func:`is_credential_name`.
    - ``url_fragment`` — the same, for a fragment written as ``k=v`` pairs
      (the OAuth implicit flow's ``#access_token=…``).

    Args:
        url: The string to redact.
        location: JSON pointer recorded in each entry.

    Returns:
        The redacted string and its ledger entries, in URL order.
    """
    head = _ABSOLUTE_URL.match(url)
    if head is None:
        return url, []
    entries: list[RedactionEntry] = []
    scheme = url[: head.end()]
    rest = url[head.end() :]

    end = len(rest)
    for stop in "/?#":
        found = rest.find(stop)
        if found != -1:
            end = min(end, found)
    authority, tail = rest[:end], rest[end:]
    at = authority.rfind("@")
    if at != -1:
        authority = REDACTED + authority[at:]
        entries.append(RedactionEntry(location=location, field=None, rule="url_userinfo"))

    before_fragment, hash_sign, fragment = tail.partition("#")
    path, question, query = before_fragment.partition("?")
    if question:
        query, found_entries = _redact_params(query, location=location, rule="url_query")
        entries.extend(found_entries)
    if hash_sign and "=" in fragment:
        fragment, found_entries = _redact_params(fragment, location=location, rule="url_fragment")
        entries.extend(found_entries)
    return scheme + authority + path + question + query + hash_sign + fragment, entries


def redact_value(value: Any, *, location: str = "") -> tuple[Any, list[RedactionEntry]]:
    """Redact a JSON-shaped value by the shared rules, recursively.

    - A mapping key that :func:`is_credential_name` has its value replaced
      — whole, whatever it holds — by ``<redacted>`` (rule ``field_name``).
      A ``None`` value is kept: there is nothing to withhold, and
      ``null`` must stay distinguishable from a value that was set.
    - Every other mapping value and every list item is walked.
    - A string goes through :func:`redact_url`.
    - Anything else is returned as is.

    The input is never mutated; mappings come back as new ``dict``\\ s,
    lists as new lists.

    Args:
        value: Typically ``encoding.snapshot(...)`` of a context mapping.
        location: JSON pointer of *value* itself (``""`` for the root).

    Returns:
        The redacted copy and the ledger entries, in traversal order.
    """
    entries: list[RedactionEntry] = []
    return _redact(value, location, entries), entries


def _redact(value: Any, location: str, entries: list[RedactionEntry]) -> Any:
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, child in value.items():
            name = str(key)
            child_location = f"{location}/{_escape_pointer(name)}"
            if child is not None and is_credential_name(name):
                out[key] = REDACTED
                entries.append(
                    RedactionEntry(location=child_location, field=name, rule="field_name")
                )
            else:
                out[key] = _redact(child, child_location, entries)
        return out
    if isinstance(value, list | tuple):
        items = [_redact(item, f"{location}/{index}", entries) for index, item in enumerate(value)]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, str):
        text, found = redact_url(value, location=location)
        entries.extend(found)
        return text
    return value
