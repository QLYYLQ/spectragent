"""``strict`` — the v1 miss policy (§4.14 "miss（`strict`）").

Documents first (a main-frame document miss fails and marks the replay
diverged, a sub-frame one fails), then benign requests (beacons, pings, CSP
and Reporting reports, analytics hosts, a site rule's ``synthesize_204``)
answered 204, and every other subresource 404 — both with
``x-replay-miss: 1``. Never "continue": a replay sends nothing to the network.

Documents come first on purpose: an ad or analytics *iframe* that missed is
a sub-frame document and fails like any other, rather than rendering an
empty 204 page. A document fails with ``Failed`` because a 404 or
``BlockedByClient`` would cut off Chrome's http fallback (S2a).
"""

from typing import Annotated

from pydantic import Field as PydanticField

from spectragent.core.capabilities.browser.replay.canonical import host_of, media_type
from spectragent.core.capabilities.browser.replay.miss_policy import (
    DOCUMENT_ERROR_REASON,
    REPLAY_MISS_HEADER,
    MissDecision,
    MissPolicyBase,
    ReplayMiss,
)

#: Resource types that are reports or beacons by what Chrome says they are.
BENIGN_RESOURCE_TYPES = frozenset({"Ping", "CSPViolationReport"})
#: Request content types of CSP and Reporting API reports.
REPORT_CONTENT_TYPES = frozenset(
    {
        "application/csp-report",
        "application/reports+json",
        "application/expect-ct-report+json",
    }
)
#: Analytics, telemetry and error-reporting endpoints, matched as a host
#: suffix: a miss there is answered 204 (nothing on the page depends on the
#: answer). ``extra_benign_hosts`` adds to it.
BENIGN_HOSTS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "doubleclick.net",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
    "hotjar.com",
    "hotjar.io",
    "clarity.ms",
    "sentry.io",
    "browser-intake-datadoghq.com",
    "nr-data.net",
    "scorecardresearch.com",
    "quantserve.com",
    "fullstory.com",
    "bat.bing.com",
)


def host_matches(host: str, suffixes: tuple[str, ...] | list[str]) -> bool:
    """Whether *host* is one of *suffixes* or a subdomain of one."""
    host = host.lower().rstrip(".")
    for suffix in suffixes:
        wanted = suffix.lower().strip(".")
        if wanted and (host == wanted or host.endswith("." + wanted)):
            return True
    return False


class StrictMissPolicy(MissPolicyBase):
    """Fail documents, 204 the benign, 404 the rest — never go to the network."""

    __registry_key__ = "strict"

    def __init__(
        self,
        *,
        extra_benign_hosts: Annotated[
            list[str] | None,
            PydanticField(
                description=(
                    "Hosts (matched as a suffix: 'stats.example.com' covers "
                    "'a.stats.example.com') whose misses are answered 204 as "
                    "benign analytics, on top of the built-in list. None (the "
                    "default): the built-in list only."
                ),
            ),
        ] = None,
    ) -> None:
        self.extra_benign_hosts = extra_benign_hosts

    def decide(self, miss: ReplayMiss) -> MissDecision:
        if miss.is_document:
            return MissDecision(
                request_class="document",
                answer="fail",
                error_reason=DOCUMENT_ERROR_REASON,
                diverged=miss.frame == "main",
            )
        if self.benign(miss):
            return MissDecision(
                request_class="benign", answer="fulfill", status=204, headers=(REPLAY_MISS_HEADER,)
            )
        return MissDecision(
            request_class="subresource",
            answer="fulfill",
            status=404,
            headers=(REPLAY_MISS_HEADER,),
        )

    def benign(self, miss: ReplayMiss) -> bool:
        """A beacon, a report, an analytics host, or a site rule's ``synthesize_204``."""
        if miss.site_benign or miss.resource_type in BENIGN_RESOURCE_TYPES:
            return True
        if media_type(miss.content_type)[0] in REPORT_CONTENT_TYPES:
            return True
        hosts = [*BENIGN_HOSTS, *(self.extra_benign_hosts or ())]
        return host_matches(host_of(miss.url), hosts)
