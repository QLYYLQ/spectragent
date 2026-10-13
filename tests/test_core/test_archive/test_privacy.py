"""The shared privacy rules — one vocabulary, pinned.

Three things are held here: what the two name patterns match (and, as
importantly, what they leave alone), that the URL rules replace the
credential parts of a URL and **not one other byte**, and that the rules
cannot change without the policy version changing with them.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from typing import Any

import pytest

from spectragent.core.archive import privacy
from spectragent.core.archive.privacy import (
    CREDENTIAL_NAME_PATTERN,
    MARKER_REDACTED,
    MARKER_SESSION_STATE,
    PRIVACY_POLICY,
    REDACTED,
    SENSITIVE_FIELD_PATTERN,
    RedactionEntry,
    RedactionLedger,
    is_credential_name,
    is_sensitive_field,
    redact_url,
    redact_value,
)
from spectragent.core.trace.secret_patterns import SECRET_PATTERNS

#: The two URL shapes of ``SECRET_PATTERNS``
#: (``spectragent/core/trace/secret_patterns.py``, which the secret guard test
#: and the export's credential validator share): a redacted URL must never
#: look like a credential to the guard, or every redacted trace would fail it.
_GUARD_URL_PATTERNS = tuple(
    pattern
    for label, pattern in SECRET_PATTERNS
    if label in ("url with credentials", "url carrying a token")
)


class TestFieldNamePatterns:
    @pytest.mark.parametrize(
        "name",
        [
            "password",
            "Passwd",
            "user_pwd",
            "otp",
            "token",
            "access_token",
            "client_secret",
            "credit_card",
            "CARD_NUMBER",
            "cvv",
        ],
    )
    def test_sensitive(self, name: str) -> None:
        assert is_sensitive_field(name)
        assert is_credential_name(name)

    @pytest.mark.parametrize(
        "name", ["username", "email", "start_url", "timeout", "viewport_width", "query"]
    )
    def test_not_sensitive(self, name: str) -> None:
        assert not is_sensitive_field(name)

    @pytest.mark.parametrize(
        "name",
        [
            "key",
            "api_key",
            "apikey",
            "OPENAI_API_KEY",
            "x-api-key",
            "sig",
            "X-Amz-Signature",
            "X-Amz-Credential",
            "auth",
            "basic.auth",
        ],
    )
    def test_credential_shaped(self, name: str) -> None:
        assert is_credential_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "keyword",
            "monkey",
            "keys",
            "signal",
            "author",
            "authority",
            "start_url",
            "credentials_ref",
        ],
    )
    def test_not_credential_shaped(self, name: str) -> None:
        """A segment boundary is required: ``keyword`` and ``monkey`` stay readable.

        ``credentials_ref`` is the task schema's pointer into a credential
        store — a reference, never the secret — and is recorded as is.
        """
        assert not is_credential_name(name)


class TestRedactUrl:
    def test_userinfo_is_replaced_whole(self) -> None:
        out, entries = redact_url(
            "http://sweepbot:Hunter2@proxy.example:8080/p?q=1#f", location="/u"
        )
        assert out == f"http://{REDACTED}@proxy.example:8080/p?q=1#f"
        assert entries == [RedactionEntry(location="/u", field=None, rule="url_userinfo")]

    def test_userinfo_without_a_password_too(self) -> None:
        out, _ = redact_url("ftp://anonymous@files.example/")
        assert out == f"ftp://{REDACTED}@files.example/"

    def test_an_at_sign_inside_the_password(self) -> None:
        out, _ = redact_url("https://u:p@ss@host.example/")
        assert out == f"https://{REDACTED}@host.example/"

    def test_a_credential_query_parameter(self) -> None:
        url = "https://shop.example/landing?ref=guard&token=Zk3Lm9QpRt7Vx2BnQ8"
        out, entries = redact_url(url, location="/start_url")
        assert out == f"https://shop.example/landing?ref=guard&token={REDACTED}"
        assert entries == [RedactionEntry(location="/start_url", field="token", rule="url_query")]

    def test_names_are_matched_unquoted_and_kept_as_written(self) -> None:
        out, entries = redact_url("https://a.example/?api%5Fkey=s3cr3tvalue&x=1")
        assert out == f"https://a.example/?api%5Fkey={REDACTED}&x=1"
        assert [e.field for e in entries] == ["api_key"]

    def test_a_credential_fragment_parameter(self) -> None:
        out, entries = redact_url("https://app.example/cb#access_token=abc123def&state=xyz")
        assert out == f"https://app.example/cb#access_token={REDACTED}&state=xyz"
        assert entries == [RedactionEntry(location="", field="access_token", rule="url_fragment")]

    def test_a_plain_fragment_is_not_a_parameter_list(self) -> None:
        url = "https://docs.example/page#token-section"
        assert redact_url(url) == (url, [])

    def test_every_other_byte_is_preserved(self) -> None:
        """No re-encoding: what was not replaced is byte-for-byte the input."""
        url = "https://h.example/a%20b/c?x=%E2%9C%93&flag&token=t0k3n&b=c+d;e#k=v&sig=zz"
        out, entries = redact_url(url)
        assert (
            out
            == f"https://h.example/a%20b/c?x=%E2%9C%93&flag&token={REDACTED}&b=c+d;e#k=v&sig={REDACTED}"
        )
        assert [(e.field, e.rule) for e in entries] == [
            ("token", "url_query"),
            ("sig", "url_fragment"),
        ]

    def test_an_empty_value_has_nothing_to_replace(self) -> None:
        url = "https://h.example/?token=&a=1"
        assert redact_url(url) == (url, [])

    @pytest.mark.parametrize(
        "url",
        [
            "https://shop.example?ref=a@b.com",
            "https://shop.example#u=a@b.com",
            "https://shop.example/p@x?q=1",
        ],
    )
    def test_an_at_sign_after_the_authority_is_not_userinfo(self, url: str) -> None:
        """The authority ends at the first ``/``, ``?`` or ``#``; an ``@`` past it is data.

        Read as userinfo, ``https://shop.example?ref=a@b.com`` would come back
        as ``https://<redacted>@b.com``: another host, recorded in the
        ``task_setup`` a branch task re-creates its environment from.
        """
        assert redact_url(url) == (url, [])

    @pytest.mark.parametrize(
        "text",
        [
            "hello world",
            "/relative/path?token=abc12345",
            "mailto:me@example.com",
            "",
            "not a url://x",
        ],
    )
    def test_only_absolute_urls_are_urls(self, text: str) -> None:
        assert redact_url(text) == (text, [])

    @pytest.mark.parametrize(
        "url",
        [
            "http://sweepbot:Hunter2Hunter2@proxy.internal.example:8080",
            "https://gw.internal.example/v1?access_token=Zk3Lm9QpRt7Vx2Bn",
            "https://shop.example/landing?ref=guard&token=Zk3Lm9QpRt7Vx2BnQ8",
            "https://api.example/v1?key=AIzaSyA1b2C3d4E5f6G7h8",
            "https://user:pw@h.example/?key=abcdefgh123&token=12345678#access_token=abcdefgh",
        ],
    )
    def test_the_output_never_looks_like_a_credential_to_the_guard(self, url: str) -> None:
        assert any(p.search(url) for p in _GUARD_URL_PATTERNS), (
            "the input must be one the guard flags"
        )
        out, entries = redact_url(url)
        assert entries
        for pattern in _GUARD_URL_PATTERNS:
            assert pattern.search(out) is None, (pattern.pattern, out)


class TestRedactValue:
    def test_a_credential_named_key_is_replaced_whole(self) -> None:
        value = {
            "start_url": "https://x.example/",
            "api_key": "sk-123",
            "auth": {"user": "u", "pw": "p"},
        }
        out, entries = redact_value(value)
        assert out == {"start_url": "https://x.example/", "api_key": REDACTED, "auth": REDACTED}
        assert entries == [
            RedactionEntry(location="/api_key", field="api_key", rule="field_name"),
            RedactionEntry(location="/auth", field="auth", rule="field_name"),
        ]

    def test_null_stays_null(self) -> None:
        """``null`` must stay distinguishable from a value that was set."""
        assert redact_value({"token": None}) == ({"token": None}, [])

    def test_nested_values_and_json_pointers(self) -> None:
        value = {
            "pages": [
                {"url": "https://a.example/?token=abcdefgh1"},
                {"url": "https://b.example/"},
            ],
            "odd/key~name": {"password": "hunter2"},
        }
        out, entries = redact_value(value, location="")
        assert out["pages"][0]["url"] == f"https://a.example/?token={REDACTED}"
        assert out["pages"][1]["url"] == "https://b.example/"
        assert out["odd/key~name"] == {"password": REDACTED}
        assert [(e.location, e.field, e.rule) for e in entries] == [
            ("/pages/0/url", "token", "url_query"),
            ("/odd~1key~0name/password", "password", "field_name"),
        ]

    def test_scalars_pass_through(self) -> None:
        for scalar in (1, 2.5, True, None):
            assert redact_value(scalar) == (scalar, [])

    def test_a_top_level_url_is_located_at_the_root(self) -> None:
        out, entries = redact_value("https://u:p@h.example/")
        assert out == f"https://{REDACTED}@h.example/"
        assert entries[0].location == ""

    def test_the_input_is_never_mutated(self) -> None:
        value = {"token": "abc", "nested": {"key": "v"}, "urls": ["https://u:p@h.example/"]}
        before = json.dumps(value, sort_keys=True)
        redact_value(value)
        assert json.dumps(value, sort_keys=True) == before

    def test_the_residual_is_real(self) -> None:
        """Stated, not hidden: a secret under an unrecognised name is kept."""
        value = {"note": "the password is hunter2"}
        assert redact_value(value) == (value, [])


class TestLedger:
    def test_defaults_to_the_current_policy(self) -> None:
        assert RedactionLedger().policy == PRIVACY_POLICY
        assert RedactionLedger().entries == []

    def test_round_trips_through_json(self) -> None:
        ledger = RedactionLedger(
            entries=[RedactionEntry(location="/a", field="a", rule="field_name")]
        )
        assert RedactionLedger.model_validate_json(ledger.model_dump_json()) == ledger

    def test_an_entry_is_frozen(self) -> None:
        entry = RedactionEntry(location="/a", field="a", rule="field_name")
        with pytest.raises(Exception):  # noqa: B017 — pydantic's frozen-instance error
            entry.location = "/b"  # type: ignore[misc]

    def test_unknown_rule_refused(self) -> None:
        with pytest.raises(ValueError):
            RedactionEntry(location="/a", field="a", rule="guess")  # type: ignore[arg-type]

    @pytest.mark.parametrize("rule", ["request_header", "input_value"])
    def test_capability_rules_share_the_vocabulary(self, rule: str) -> None:
        """The browser's rules (M29) write the same ledger; the names widen, nothing else.

        This module's rule functions do not change with them, so
        ``PRIVACY_POLICY`` does not move (the digest below proves it); a
        capability versions its own rules (``BROWSER_REDACTION_POLICY``).
        """
        entry = RedactionEntry(location="/request/headers", field="Cookie", rule=rule)  # type: ignore[arg-type]
        ledger = RedactionLedger(policy="af-privacy-1+browser-2", entries=[entry])
        assert RedactionLedger.model_validate_json(ledger.model_dump_json()) == ledger


class TestPlaceholders:
    def test_redacted_is_not_the_config_omission(self) -> None:
        """Two answers to two questions: replaced by a rule vs set and withheld."""
        from spectragent.core.trace.redaction import OMITTED

        assert REDACTED != OMITTED

    def test_markers(self) -> None:
        assert f"redacted:{PRIVACY_POLICY}" == MARKER_REDACTED
        assert MARKER_SESSION_STATE == "contains_session_state"


#: Policy version → digest of the rule sources it names: the two name
#: patterns, the placeholder, the URL pattern, and the source of every
#: function that decides what is replaced or how the ledger locates it — the
#: URL rules included. A rule change without a new policy version fails
#: ``test_a_rule_change_bumps_the_policy``; the fix is to bump
#: ``PRIVACY_POLICY`` and add a row here. An edit to those functions that
#: changes no behaviour (a comment, a docstring, formatting) moves the digest
#: too: then, and only then, re-pin the current row — and say so in the change.
_KNOWN_POLICIES = {
    "af-privacy-1": "d20c59ef75268a31d5e4ed00a580f1d1b83a4834075587520250acf8319a549e",
}


def _rule_functions() -> tuple[Any, ...]:
    """Read off the module at call time, so a replaced function is seen."""
    return (
        privacy.is_sensitive_field,
        privacy.is_credential_name,
        privacy._escape_pointer,
        privacy._redact_params,
        privacy.redact_url,
        privacy.redact_value,
        privacy._redact,
    )


def _rule_digest() -> str:
    material = "\n".join(
        [
            SENSITIVE_FIELD_PATTERN.pattern,
            str(int(SENSITIVE_FIELD_PATTERN.flags)),
            CREDENTIAL_NAME_PATTERN.pattern,
            str(int(CREDENTIAL_NAME_PATTERN.flags)),
            REDACTED,
            privacy._ABSOLUTE_URL.pattern,
            str(int(privacy._ABSOLUTE_URL.flags)),
            *(inspect.getsource(function) for function in _rule_functions()),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_a_rule_change_bumps_the_policy() -> None:
    """Every ledger records the policy that wrote it; this keeps that true.

    A pattern edited in place would make two traces stamped with the same
    ``PRIVACY_POLICY`` mean two different things.
    """
    assert PRIVACY_POLICY in _KNOWN_POLICIES, (
        f"PRIVACY_POLICY {PRIVACY_POLICY!r} has no pinned digest — add it to _KNOWN_POLICIES"
    )
    assert _KNOWN_POLICIES[PRIVACY_POLICY] == _rule_digest(), (
        "the privacy rules changed but PRIVACY_POLICY did not: bump it and pin the new digest"
    )


def test_the_tripwire_covers_the_url_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL rule changed in place moves the digest, as a name pattern does.

    Two edits the digest once missed: what counts as a URL (scheme-relative
    ``//host`` strings, say) and how ``redact_url`` reads one (a
    ``search`` instead of a ``match`` would redact URLs embedded in prose).
    """
    pinned = _KNOWN_POLICIES[PRIVACY_POLICY]
    monkeypatch.setattr(privacy, "_ABSOLUTE_URL", re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//"))
    assert _rule_digest() != pinned
    monkeypatch.undo()

    def redact_url(url: str, *, location: str = "") -> Any:  # same name, another body
        return url, []

    monkeypatch.setattr(privacy, "redact_url", redact_url)
    assert _rule_digest() != pinned
