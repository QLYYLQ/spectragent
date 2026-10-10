"""The one Python sensitive-control rule, held equal to the Skyvern page script's (§4.12).

``dom/sensitivity.py`` and ``dom/js/skyvern_extraction.py`` state the same rule
in two languages. Neither is generated from the other, so this module keeps
them equal: the hint pattern's literal character for character, and a table of
controls judged by the Python side (the live half of the parity — the same
table judged by the page script in a real Chrome — is
``test_browser_archive_live.py::test_sensitivity_parity_in_chrome``).
"""

from __future__ import annotations

import re

import pytest

from spectragent.core.capabilities.browser.dom.js.skyvern_extraction import EXTRACTION_SCRIPT
from spectragent.core.capabilities.browser.dom.sensitivity import (
    SENSITIVE_HINT_PATTERN,
    attribute_map,
    is_sensitive_control,
)

#: (tag, attributes, sensitive) — shared with the live parity test.
CONTROL_TABLE: tuple[tuple[str, dict[str, str], bool], ...] = (
    ("input", {"type": "password"}, True),
    ("input", {"type": "PASSWORD"}, True),
    ("input", {"type": "file"}, True),
    ("input", {"type": "hidden", "name": "csrf"}, True),
    ("input", {"type": "text", "name": "q"}, False),
    ("input", {"type": "email", "name": "email"}, False),
    ("input", {"autocomplete": "cc-number"}, True),
    ("input", {"autocomplete": "one-time-code"}, True),
    ("input", {"name": "otp"}, True),
    ("input", {"id": "verificationCode"}, True),
    ("input", {"name": "two_factor_token"}, True),
    ("input", {"name": "auth-code"}, True),
    ("input", {"name": "zip"}, False),
    ("textarea", {"name": "pin_hint"}, True),
    ("textarea", {"name": "comment"}, False),
    ("select", {"autocomplete": "cc-exp-month"}, True),
    ("select", {"name": "country"}, False),
    ("div", {"name": "password"}, False),
    ("a", {"id": "otp-help"}, False),
)


def test_hint_regex_matches_skyvern_literal() -> None:
    """The hint pattern is the page script's regex literal, verbatim and case-insensitive."""
    match = re.search(r"const sensitiveHint = /(.+?)/i\b", EXTRACTION_SCRIPT)
    assert match is not None, (
        "the page script's sensitiveHint literal moved — update the parity test"
    )
    assert SENSITIVE_HINT_PATTERN.pattern == match.group(1)
    assert SENSITIVE_HINT_PATTERN.flags & re.IGNORECASE


def test_the_page_script_rule_has_the_same_shape() -> None:
    """The other clauses of the rule, as the page script writes them."""
    assert "['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)" in EXTRACTION_SCRIPT
    assert "['password', 'file', 'hidden'].includes(type)" in EXTRACTION_SCRIPT
    assert "autocomplete.startsWith('cc-') || autocomplete === 'one-time-code'" in EXTRACTION_SCRIPT
    assert "[attrs.id, attrs.name, autocomplete].join(' ')" in EXTRACTION_SCRIPT


@pytest.mark.parametrize(("tag", "attrs", "expected"), CONTROL_TABLE)
def test_the_control_table(tag: str, attrs: dict[str, str], expected: bool) -> None:
    assert is_sensitive_control(tag, attrs) is expected
    assert is_sensitive_control(tag.upper(), attrs) is expected


def test_attribute_map_reads_cdp_flat_lists() -> None:
    assert attribute_map(["TYPE", "password", "value", "x"]) == {"type": "password", "value": "x"}
    assert attribute_map(["dangling"]) == {}
