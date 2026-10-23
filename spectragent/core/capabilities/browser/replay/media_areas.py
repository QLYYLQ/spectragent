"""Media-area rules: pages whose video areas restoration verification does not compare.

On a video site a recording and its offline replay rarely show the same
playback: stream keys are encrypted per session, autoplay previews start at
another moment, a player may not load offline at all, or two stacked
``<video>`` elements trade places. Restoration then fails on
``media_mismatch`` and on a screenshot distance that sits in the player,
although the rest of the page is the same. A replay rule set
(``site_rules.py``) declares such pages in its ``media_areas`` section,
beside its request and page-URL rules, so the declarations are frozen,
digested and snapshotted with them; request matching never reads them. Only
the browser's restoration verification does (``restoration.py::measure``,
``docs/architecture_design.md`` §4.14 "恢复：重定位与校验"): on a declared
page the media playback state is not judged, and the boxes the two
screenshots' media facts measured are masked out of the screenshot
comparison. Recording and media alignment do not change.

A rule names a host (required: there is no rule for every site) and an
optional path, with the same :class:`~spectragent.core.capabilities.browser.replay.page_urls.PageScope`
a page-URL rule uses. :func:`media_area_rule_ids` lists the rules whose
scope matches both page URLs.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.capabilities.browser.replay.page_urls import PageScope


class MediaAreaRule(BaseModel):
    """A page whose media areas restoration verification leaves out."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    scope: PageScope


def media_area_rule_ids(
    recorded: str | None, restored: str | None, rules: Sequence[MediaAreaRule]
) -> tuple[str, ...]:
    """The ids of the rules whose scope matches both page URLs, sorted.

    Empty when either URL is unknown: without both addresses there is no
    evidence that the two observations show a declared page.
    """
    if recorded is None or restored is None:
        return ()
    return tuple(
        sorted(
            rule.id
            for rule in rules
            if rule.scope.matches(recorded) and rule.scope.matches(restored)
        )
    )


__all__ = ["MediaAreaRule", "media_area_rule_ids"]
