"""Page-URL rules: query parameters a site writes into its own page URL, left out of restoration's URL comparison.

Some pages write a volatile parameter into their own address after loading,
with ``history.replaceState`` or client-side routing and no network request:
a recorded and a restored observation of the same page then differ in that
parameter alone. A replay rule set (``site_rules.py``) holds these
declarations in its ``page_urls`` section, beside its request rules, so they
are frozen, digested and snapshotted with them; request matching never reads
them. Only the browser's restoration verification does
(``restoration.py::measure``, ``docs/architecture_design.md`` §4.14
"恢复：重定位与校验").

A rule names a host (required: there is no rule for every site), an optional
path, and the query parameter names to leave out. :func:`compare_page_urls`
applies every rule whose scope matches both URLs and compares the rest
byte for byte.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import NamedTuple
from urllib.parse import SplitResult, unquote_plus, urlsplit

from pydantic import BaseModel, ConfigDict, Field


class PageScope(BaseModel):
    """Which page URLs a rule covers: both selectors must match.

    ``host`` is an fnmatch glob compared without case or a trailing dot; it
    never implicitly includes subdomains (write ``*.example.com`` for them).
    ``path`` is an fnmatch glob over the URL path (``None``: every path).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    path: str | None = None

    def matches(self, url: str) -> bool:
        parts = _split(url)
        if parts is None:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        return fnmatch.fnmatchcase(host, self.host.lower().rstrip(".")) and (
            self.path is None or fnmatch.fnmatchcase(parts.path or "/", self.path)
        )


class PageUrlRule(BaseModel):
    """Query parameters a site writes into its own page URL that do not select content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    scope: PageScope
    ignore_query: tuple[str, ...] = Field(min_length=1)

    def ignores_query(self, name: str) -> bool:
        """Whether *name* (decoded) is one of this rule's name globs."""
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.ignore_query)


class PageUrlComparison(NamedTuple):
    """The outcome of :func:`compare_page_urls`.

    ``ignored_query`` is every decoded parameter name left out of either URL,
    sorted; ``rule_ids`` the rules that left one out, sorted. Both are empty
    when no rule applied, and the URLs were then compared exactly.
    """

    match: bool
    ignored_query: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()


def compare_page_urls(
    recorded: str, restored: str, rules: Sequence[PageUrlRule]
) -> PageUrlComparison:
    """Whether two page URLs are equal once the declared parameters are left out.

    The rules that apply are those whose scope matches both URLs. Each URL's
    query is split on ``&``; a segment whose decoded name an applicable rule
    ignores is dropped, with any value, on either side, present or absent.
    Everything else is compared byte for byte: scheme, host and port, path,
    the remaining segments in their order and spelling, and the fragment.
    Without an applicable rule the URLs compare exactly, as do URLs that do
    not parse.
    """
    applicable = [
        rule for rule in rules if rule.scope.matches(recorded) and rule.scope.matches(restored)
    ]
    if not applicable:
        return PageUrlComparison(match=recorded == restored)
    a, b = _split(recorded), _split(restored)
    if a is None or b is None:
        return PageUrlComparison(match=recorded == restored)
    ignored: set[str] = set()
    used: set[str] = set()

    def kept(parts: SplitResult) -> list[str]:
        segments: list[str] = []
        for segment in parts.query.split("&") if parts.query else []:
            name = unquote_plus(segment.split("=", 1)[0])
            owners = [rule.id for rule in applicable if rule.ignores_query(name)]
            if owners:
                ignored.add(name)
                used.update(owners)
            else:
                segments.append(segment)
        return segments

    kept_a, kept_b = kept(a), kept(b)
    if not ignored:
        return PageUrlComparison(match=recorded == restored)
    match = (a.scheme, a.netloc, a.path, kept_a, a.fragment) == (
        b.scheme,
        b.netloc,
        b.path,
        kept_b,
        b.fragment,
    )
    return PageUrlComparison(
        match=match, ignored_query=tuple(sorted(ignored)), rule_ids=tuple(sorted(used))
    )


def _split(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return None
    return parts


__all__ = ["PageScope", "PageUrlComparison", "PageUrlRule", "compare_page_urls"]
